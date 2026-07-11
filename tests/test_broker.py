from __future__ import annotations

import ast
import importlib.util
import pathlib
import sqlite3
import tempfile
from typing import Any

import pytest

# Load broker module from disk (not on package path)
_p = pathlib.Path(__file__).resolve().parent.parent / "broker" / "aresbrokerd.py"
_spec = importlib.util.spec_from_file_location("aresbrokerd", str(_p))
broker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(broker)

# Allow config mirroring broker.example.json
ALLOW = {
    "package_install": [r"^[a-z0-9][a-z0-9+.\-]*$"],
    "service_action": [r"^(restart|status) (ares|ares-updater)$"],
    "command": [],
}


# ============================================================================
# Test 1: No ares import + no shell=True (AST check)
# ============================================================================


def test_broker_no_ares_import():
    """Verify broker.py does not import any ares modules (§10 security boundary)."""
    with open(_p, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    ares_imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ares"):
                    ares_imports.append(f"Import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("ares"):
                ares_imports.append(f"ImportFrom: {node.module}")

    assert not ares_imports, f"Broker must not import ares: {ares_imports}"


def test_broker_no_shell_true():
    """Verify broker.py does not use shell=True (§16.3 security requirement)."""
    with open(_p, "r", encoding="utf-8") as f:
        source = f.read()

    assert "shell=True" not in source, "Broker must not use shell=True"


# ============================================================================
# Test 2: Allowlist validation (broker.validate)
# ============================================================================


def test_validate_package_install_accept():
    """Valid package name passes package_install allowlist."""
    assert broker.validate("package_install", "htop", ALLOW) is True


def test_validate_package_install_injection_reject():
    """Injection attempt is rejected."""
    assert broker.validate("package_install", "htop; rm -rf /", ALLOW) is False


def test_validate_package_install_empty_reject():
    """Empty command is rejected."""
    assert broker.validate("package_install", "", ALLOW) is False


def test_validate_service_action_restart_accept():
    """Valid restart command passes."""
    assert broker.validate("service_action", "restart ares", ALLOW) is True


def test_validate_service_action_status_updater_accept():
    """Valid status command for updater passes."""
    assert broker.validate("service_action", "status ares-updater", ALLOW) is True


def test_validate_service_action_wrong_unit_reject():
    """Unknown unit is rejected."""
    assert broker.validate("service_action", "restart nginx", ALLOW) is False


def test_validate_service_action_wrong_action_reject():
    """Unknown action is rejected."""
    assert broker.validate("service_action", "reboot ares", ALLOW) is False


def test_validate_command_always_reject():
    """Command kind is always rejected (empty allowlist in v1)."""
    assert broker.validate("command", "anything", ALLOW) is False


# ============================================================================
# Test 3: argv construction (broker.build_argv)
# ============================================================================


def test_build_argv_package_install():
    """build_argv for valid package_install returns fixed argv list."""
    result = broker.build_argv("package_install", "htop")
    assert result == ["apt-get", "install", "-y", "htop"]
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)


def test_build_argv_service_restart_ares():
    """build_argv for restart ares extracts action and unit correctly."""
    result = broker.build_argv("service_action", "restart ares")
    assert result == ["systemctl", "restart", "ares"]
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)


def test_build_argv_service_status_updater():
    """build_argv for status ares-updater extracts correctly."""
    result = broker.build_argv("service_action", "status ares-updater")
    assert result == ["systemctl", "status", "ares-updater"]
    assert isinstance(result, list)
    assert all(isinstance(x, str) for x in result)


def test_build_argv_service_invalid_returns_none():
    """build_argv returns None for unvalidated service_action."""
    result = broker.build_argv("service_action", "restart nginx")
    assert result is None


def test_build_argv_command_always_none():
    """build_argv returns None for command kind (no allowlist in v1)."""
    result = broker.build_argv("command", "x")
    assert result is None


def test_build_argv_bogus_kind_returns_none():
    """build_argv returns None for unknown kind."""
    result = broker.build_argv("bogus", "x")
    assert result is None


# ============================================================================
# Test 4: process_once end-to-end with temp DB and monkeypatch
# ============================================================================


@pytest.fixture
def temp_db():
    """Create a temp DB with priv_requests schema and clean up after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS priv_requests (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('package_install','service_action','command')),
          command TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','approved','denied','executing','done','failed')),
          exit_code INTEGER,
          output TEXT,
          created_at TEXT NOT NULL,
          decided_at TEXT,
          executed_at TEXT
        )
    """)
    conn.commit()
    conn.close()

    yield path

    import os
    os.unlink(path)


@pytest.fixture
def fake_run_command():
    """Fixture: monkeypatch broker.run_command to track calls and return fixed output."""
    class FakeRunCommand:
        def __init__(self):
            self.calls = []

        def __call__(self, argv):
            self.calls.append(argv)
            return (0, "installed")

        def reset(self):
            self.calls = []

    return FakeRunCommand()


def test_process_once_happy_path(temp_db, monkeypatch, fake_run_command):
    """process_once: approved row is executed, status becomes done."""
    # Insert an approved package_install request
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-1", "user1", "package_install", "htop", "install htop", "approved", "2026-07-11T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Monkeypatch run_command
    monkeypatch.setattr(broker, "run_command", fake_run_command)

    # Process once
    broker.process_once({"db_path": temp_db, "allow": ALLOW})

    # Check DB state
    conn = sqlite3.connect(temp_db)
    cur = conn.execute("SELECT status, exit_code, output FROM priv_requests WHERE id='req-1'")
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "done"
    assert row[1] == 0
    assert row[2] == "installed"

    # Verify run_command was called with exact argv
    assert len(fake_run_command.calls) == 1
    assert fake_run_command.calls[0] == ["apt-get", "install", "-y", "htop"]


# ============================================================================
# Test 5: Defence in depth (rejected non-allowlisted commands)
# ============================================================================


def test_process_once_defence_in_depth_command_rejected(temp_db, monkeypatch, fake_run_command):
    """process_once: approved but non-allowlisted command is rejected, run_command not called."""
    # Insert an approved command request (kind='command' has empty allowlist)
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-2", "user1", "command", "rm -rf /", "malicious", "approved", "2026-07-11T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Monkeypatch run_command
    fake_run_command.reset()
    monkeypatch.setattr(broker, "run_command", fake_run_command)

    # Process once
    broker.process_once({"db_path": temp_db, "allow": ALLOW})

    # Check DB state
    conn = sqlite3.connect(temp_db)
    cur = conn.execute("SELECT status, output FROM priv_requests WHERE id='req-2'")
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "failed"
    assert "rejected: not allowlisted" in row[1]

    # Verify run_command was never called
    assert len(fake_run_command.calls) == 0


# ============================================================================
# Test 6: Operator mis-approval still rejected
# ============================================================================


def test_process_once_operator_misapproval_rejected(temp_db, monkeypatch, fake_run_command):
    """process_once: approved request with non-regex-matching command is rejected."""
    # Insert an approved package_install request with injection attempt
    # (bypasses CHECK constraint because command is free text, but fails regex)
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-3", "user1", "package_install", "evil; rm -rf /", "operator mistake", "approved", "2026-07-11T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Monkeypatch run_command
    fake_run_command.reset()
    monkeypatch.setattr(broker, "run_command", fake_run_command)

    # Process once
    broker.process_once({"db_path": temp_db, "allow": ALLOW})

    # Check DB state
    conn = sqlite3.connect(temp_db)
    cur = conn.execute("SELECT status, output FROM priv_requests WHERE id='req-3'")
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "failed"
    assert "rejected: not allowlisted" in row[1]

    # Verify run_command was never called
    assert len(fake_run_command.calls) == 0


# ============================================================================
# Test 7: Multiple requests in one cycle
# ============================================================================


def test_process_once_multiple_requests(temp_db, monkeypatch, fake_run_command):
    """process_once processes multiple approved requests in one cycle."""
    conn = sqlite3.connect(temp_db)

    # Insert multiple requests
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-a", "user1", "package_install", "git", "install git", "approved", "2026-07-11T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-b", "user1", "service_action", "restart ares", "restart service", "approved", "2026-07-11T00:00:00+00:00"),
    )
    # Insert a pending one (should be ignored)
    conn.execute(
        "INSERT INTO priv_requests "
        "(id, user_id, kind, command, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("req-c", "user1", "package_install", "vim", "pending", "pending", "2026-07-11T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Monkeypatch run_command
    fake_run_command.reset()
    monkeypatch.setattr(broker, "run_command", fake_run_command)

    # Process once
    broker.process_once({"db_path": temp_db, "allow": ALLOW})

    # Check that both approved were processed
    conn = sqlite3.connect(temp_db)
    cur = conn.execute("SELECT id, status FROM priv_requests ORDER BY id")
    rows = cur.fetchall()
    conn.close()

    status_map = {row[0]: row[1] for row in rows}
    assert status_map["req-a"] == "done"
    assert status_map["req-b"] == "done"
    assert status_map["req-c"] == "pending"  # unchanged

    # Verify both were executed
    assert len(fake_run_command.calls) == 2
    assert fake_run_command.calls[0] == ["apt-get", "install", "-y", "git"]
    assert fake_run_command.calls[1] == ["systemctl", "restart", "ares"]
