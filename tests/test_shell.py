from __future__ import annotations

import asyncio
import getpass
import os
import pathlib
import pytest
import shutil
import subprocess

from ares.plugins.tools.shell_tools import RunShell


@pytest.mark.asyncio
async def test_basic_exec_with_dev_warning():
    """Test basic command execution with dev warning (empty sandbox_user)."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="echo hello")

    assert result.ok is True
    assert "hello" in result.content
    assert "DEV ONLY" in result.content


@pytest.mark.asyncio
async def test_non_zero_exit_is_ok_true():
    """Test that non-zero exit codes return ok=True (not infra failure)."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="exit 7")

    assert result.ok is True
    assert "exit code 7" in result.content


@pytest.mark.asyncio
async def test_empty_command_rejected():
    """Test that empty or whitespace-only commands are rejected."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="   ")

    assert result.ok is False
    assert "empty" in result.content.lower()


@pytest.mark.asyncio
async def test_timeout_cap_rejected_no_execution():
    """Test that timeout_s exceeding max is rejected before execution."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="echo hi", timeout_s=999)

    assert result.ok is False
    assert "120" in result.content  # mentions the max
    # Ensure it's a rejection message, not command output
    assert "echo" not in result.content.lower() or "timeout" in result.content.lower()


@pytest.mark.asyncio
async def test_timeout_kills_long_command():
    """Test that commands exceeding timeout are killed and reported."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="sleep 5", timeout_s=1)

    assert result.ok is False
    assert "timed out" in result.content.lower()


@pytest.mark.asyncio
async def test_secret_isolation():
    """Test that host env vars are not passed to the sandboxed command."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)

    # Set a test secret in the host env
    os.environ["ARES_TEST_SECRET"] = "topsecret"
    try:
        result = await sh.run(None, command="echo val=$ARES_TEST_SECRET")

        # The restricted env should NOT include ARES_TEST_SECRET
        assert result.ok is True
        assert "topsecret" not in result.content
    finally:
        # Clean up
        del os.environ["ARES_TEST_SECRET"]


@pytest.mark.asyncio
async def test_refuses_own_uid_in_prod_empty_sandbox_user(monkeypatch):
    """Test that empty sandbox_user is refused in prod mode."""
    monkeypatch.setenv("ARES_ENV", "prod")

    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="echo hi")

    assert result.ok is False
    assert "refused" in result.content.lower() or "error" in result.content.lower()


@pytest.mark.asyncio
async def test_refuses_own_uid_in_prod_current_user(monkeypatch):
    """Test that sandbox_user == current user is refused in prod mode."""
    monkeypatch.setenv("ARES_ENV", "prod")

    current_user = getpass.getuser()
    sh = RunShell(current_user, "/tmp", timeout_default_s=30, timeout_max_s=120)
    result = await sh.run(None, command="echo hi")

    assert result.ok is False
    assert "refused" in result.content.lower() or "error" in result.content.lower()


@pytest.mark.asyncio
async def test_timeout_default_applies():
    """Test that timeout_default_s is used when timeout_s is not provided."""
    sh = RunShell("", "/tmp", timeout_default_s=30, timeout_max_s=120)
    # A quick echo should succeed with the default timeout
    result = await sh.run(None, command="echo quick")

    assert result.ok is True
    assert "quick" in result.content


@pytest.mark.asyncio
async def test_prod_builds_runner_argv_command_single_element(monkeypatch):
    """Test that prod mode builds runner argv with command as single element."""
    monkeypatch.setenv("ARES_ENV", "prod")
    monkeypatch.setattr("ares.plugins.tools.shell_tools.getpass.getuser", lambda: "ares")

    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            returncode = 0
            pid = 12345

            async def communicate(self):
                return (b"ok-output", None)

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    sh = RunShell("ares-sbx", "/home/ares-sbx")
    res = await sh.run(None, command="echo hello")

    assert res.ok is True
    assert list(captured["argv"]) == [
        "sudo",
        "-n",
        "-u",
        "ares-sbx",
        "/usr/local/sbin/ares-sbx-runner",
        "echo hello",
    ]
    assert len(captured["argv"]) == 6
    assert captured["argv"][-1] == "echo hello"
    assert captured["kwargs"].get("env") is None
    assert captured["kwargs"].get("cwd") is None
    assert "shell" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_prod_shell_metacharacters_single_argv_element(monkeypatch):
    """Test that shell metacharacters are passed as single argv element."""
    monkeypatch.setenv("ARES_ENV", "prod")
    monkeypatch.setattr("ares.plugins.tools.shell_tools.getpass.getuser", lambda: "ares")

    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs

        class _P:
            returncode = 0
            pid = 12345

            async def communicate(self):
                return (b"ok-output", None)

        return _P()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    sh = RunShell("ares-sbx", "/home/ares-sbx")

    dangerous_commands = [
        "; rm -rf /",
        "$(cat /etc/passwd)",
        "`id`",
        "a && b | c > /tmp/x",
    ]

    for cmd in dangerous_commands:
        captured.clear()
        res = await sh.run(None, command=cmd)
        assert res.ok is True
        assert list(captured["argv"])[-1] == cmd
        assert len(captured["argv"]) == 6
        assert captured["argv"][4] == "/usr/local/sbin/ares-sbx-runner"


def test_sbx_runner_syntax_and_lint():
    """Test sbx-runner script syntax and lint."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sbx_runner_path = repo_root / "deploy" / "sbx-runner"

    assert sbx_runner_path.exists(), f"sbx-runner not found at {sbx_runner_path}"

    # Check syntax with sh -n
    result = subprocess.run(
        ["sh", "-n", str(sbx_runner_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"sbx-runner syntax error: {result.stderr}"

    # Check with shellcheck if available
    if shutil.which("shellcheck"):
        result = subprocess.run(
            ["shellcheck", str(sbx_runner_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"sbx-runner shellcheck errors: {result.stderr}"
    else:
        pytest.skip("shellcheck not installed")
