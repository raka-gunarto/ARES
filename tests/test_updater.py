from __future__ import annotations

import ast
import hashlib
import hmac
import importlib.util
import pathlib
import re

import pytest

# Load updater module from disk (not on package path)
_p = pathlib.Path(__file__).resolve().parent.parent / "updater" / "aresupdater.py"
_spec = importlib.util.spec_from_file_location("aresupdater", str(_p))
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


# ============================================================================
# Test 1: No ares import + no shell=True (AST check)
# ============================================================================


def test_updater_no_ares_import():
    """Verify updater.py does not import any ares modules (§14 security boundary)."""
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

    assert not ares_imports, f"Updater must not import ares: {ares_imports}"


def test_updater_stdlib_only():
    """Verify updater.py only imports from the stdlib allowlist."""
    with open(_p, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    imported_modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Get top-level module name
                top_level = alias.name.split(".")[0]
                imported_modules.add(top_level)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level = node.module.split(".")[0]
                imported_modules.add(top_level)

    # Stdlib allowlist per spec
    stdlib_allowlist = {
        "json",
        "hmac",
        "hashlib",
        "os",
        "sys",
        "subprocess",
        "threading",
        "time",
        "logging",
        "http",
        "socketserver",
        "urllib",
        "pathlib",
        "__future__",
        "typing",
    }

    non_stdlib = imported_modules - stdlib_allowlist
    assert not non_stdlib, f"Updater must only import stdlib, found: {non_stdlib}"


def test_updater_no_shell_true():
    """Verify updater.py does not use shell=True in subprocess calls.

    Pragmatic check: shell=True should not appear as an actual parameter
    (followed by ) or , on the same line), skipping docstrings.
    """
    with open(_p, "r", encoding="utf-8") as f:
        source = f.read()

    # Simple heuristic: remove triple-quoted strings (docstrings)
    cleaned = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    cleaned = re.sub(r"'''.*?'''", "", cleaned, flags=re.DOTALL)

    # Check for shell=True as an actual parameter
    if "shell=True)" in cleaned or "shell=True," in cleaned:
        pytest.fail("Updater must not use shell=True")


# ============================================================================
# Test 2: HMAC verify (signature validation)
# ============================================================================


def test_verify_signature_accepts_valid():
    """verify_signature accepts a correctly-signed body."""
    secret = b"topsecret"
    body = b'{"ref":"refs/heads/main"}'
    good = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert updater.verify_signature(secret, body, good) is True


def test_verify_signature_rejects_wrong_secret():
    """verify_signature rejects a body signed with a different secret."""
    secret = b"topsecret"
    body = b'{"ref":"refs/heads/main"}'
    good = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert updater.verify_signature(b"other", body, good) is False


def test_verify_signature_rejects_tampered_body():
    """verify_signature rejects a tampered body."""
    secret = b"topsecret"
    body = b'{"ref":"refs/heads/main"}'
    good = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert updater.verify_signature(secret, b'{"ref":"x"}', good) is False


def test_verify_signature_rejects_empty():
    """verify_signature rejects empty signature."""
    secret = b"topsecret"
    body = b'{"ref":"refs/heads/main"}'

    assert updater.verify_signature(secret, body, "") is False


def test_verify_signature_rejects_malformed():
    """verify_signature rejects malformed signatures."""
    secret = b"topsecret"
    body = b'{"ref":"refs/heads/main"}'

    assert updater.verify_signature(secret, body, "deadbeef") is False
    assert updater.verify_signature(secret, body, "sha1=abc") is False


# ============================================================================
# Test 3: perform_update abort on smoke failure (safety gate)
# ============================================================================


def test_perform_update_aborts_on_smoke_failure(monkeypatch):
    """perform_update aborts (no swap/restart/write) if smoke import fails.

    This is the key safety test: a broken checkout must not swap or restart.
    """
    # Track calls in order
    call_order = []

    def fake_checkout_release(config, sha):
        call_order.append("checkout")
        return "/tmp/rel"

    def fake_smoke_import(venv_python, cwd):
        call_order.append("smoke")
        return False

    def fake_swap_symlink(app_dir, release_path):
        call_order.append("swap")

    def fake_run_restart(config):
        call_order.append("restart")
        return True

    def fake_write_released_sha(config, new_sha):
        call_order.append("write")

    monkeypatch.setattr(updater, "checkout_release", fake_checkout_release)
    monkeypatch.setattr(updater, "smoke_import", fake_smoke_import)
    monkeypatch.setattr(updater, "swap_symlink", fake_swap_symlink)
    monkeypatch.setattr(updater, "run_restart", fake_run_restart)
    monkeypatch.setattr(updater, "write_released_sha", fake_write_released_sha)

    result = updater.perform_update(
        {"app_dir": "/tmp/app", "venv_python": "/x"}, "abc123"
    )

    assert result is False
    # Only checkout and smoke should have been called
    assert call_order == ["checkout", "smoke"]
    # swap, restart, write should NOT have been called
    assert "swap" not in call_order
    assert "restart" not in call_order
    assert "write" not in call_order


# ============================================================================
# Test 4: perform_update succeeds and swaps when smoke passes
# ============================================================================


def test_perform_update_swaps_on_smoke_success(monkeypatch):
    """perform_update swaps and restarts if smoke import succeeds.

    Sequence: checkout -> smoke -> swap -> restart -> write.
    """
    # Track calls in order
    call_order = []

    def fake_checkout_release(config, sha):
        call_order.append("checkout")
        return "/tmp/rel"

    def fake_smoke_import(venv_python, cwd):
        call_order.append("smoke")
        return True

    def fake_swap_symlink(app_dir, release_path):
        call_order.append("swap")

    def fake_run_restart(config):
        call_order.append("restart")
        return True

    def fake_write_released_sha(config, new_sha):
        call_order.append("write")

    monkeypatch.setattr(updater, "checkout_release", fake_checkout_release)
    monkeypatch.setattr(updater, "smoke_import", fake_smoke_import)
    monkeypatch.setattr(updater, "swap_symlink", fake_swap_symlink)
    monkeypatch.setattr(updater, "run_restart", fake_run_restart)
    monkeypatch.setattr(updater, "write_released_sha", fake_write_released_sha)

    result = updater.perform_update(
        {"app_dir": "/tmp/app", "venv_python": "/x"}, "abc123"
    )

    assert result is True
    # Check order: checkout, smoke, swap, restart, write
    assert call_order == ["checkout", "smoke", "swap", "restart", "write"]
