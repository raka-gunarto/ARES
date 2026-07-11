from __future__ import annotations

import asyncio
import getpass
import os
import pytest

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
