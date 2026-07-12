"""Tests for self-edit pull request workflow (ares/plugins/tools/selfedit_tools.py)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ares.plugins.tools.selfedit_tools import (
    PRCache,
    build_selfedit_tools,
)


def _git(*args, cwd=None):
    """Run a git command, capturing output."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def git_setup(tmp_path):
    """Set up a local git origin and scratch clone for testing."""
    # Create bare origin repo
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )

    # Create seed clone to populate origin
    seed = tmp_path / "seed"
    _git("clone", str(origin), str(seed))

    # Add initial commit to seed
    (seed / "README.md").write_text("seed\n")
    _git(
        "-c", "user.email=seed@x", "-c", "user.name=seed",
        "add", "-A",
        cwd=seed,
    )
    _git(
        "-c", "user.email=seed@x", "-c", "user.name=seed",
        "commit", "-m", "init",
        cwd=seed,
    )
    _git("push", "origin", "main", cwd=seed)

    # Verify main branch exists on origin
    result = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", "main"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "Failed to verify main branch on origin"

    # Create scratch clone (tool reuses this)
    scratch = tmp_path / "scratch"
    _git("clone", str(origin), str(scratch))

    # Create live directory (must stay empty)
    live = tmp_path / "live"
    live.mkdir()

    # Build config and tools
    config = {
        "scratch_repo": str(scratch),
        "live_code_path": str(live),
        "github_repo": "owner/repo",
        "github_token": "test-token",
        "sandbox_user": "",  # dev mode
    }
    cache = PRCache()
    open_pr_tool, get_status_tool = build_selfedit_tools(config, cache)

    return {
        "origin": origin,
        "seed": seed,
        "scratch": scratch,
        "live": live,
        "config": config,
        "cache": cache,
        "open_pr_tool": open_pr_tool,
        "get_status_tool": get_status_tool,
    }


@pytest.mark.asyncio
async def test_absolute_path_rejected(git_setup):
    """Absolute paths should be rejected without any git/network operations."""
    open_pr_tool = git_setup["open_pr_tool"]
    live = git_setup["live"]
    scratch = git_setup["scratch"]

    result = await open_pr_tool.run(
        None,
        branch="ares/patch-1",
        title="test",
        files=[{"path": "/etc/pwned", "content": "x"}],
    )

    assert result.ok is False
    assert "escapes" in result.content
    assert list(live.rglob("*")) == []  # live still empty
    assert not (scratch / "etc" / "pwned").exists()


@pytest.mark.asyncio
async def test_parent_escape_rejected(git_setup):
    """Parent directory escape sequences should be rejected."""
    open_pr_tool = git_setup["open_pr_tool"]
    live = git_setup["live"]

    result = await open_pr_tool.run(
        None,
        branch="ares/patch-1",
        title="test",
        files=[{"path": "../../pwned.py", "content": "x"}],
    )

    assert result.ok is False
    assert "escapes" in result.content
    assert list(live.rglob("*")) == []  # live still empty


@pytest.mark.asyncio
async def test_success_writes_scratch_pushes_origin_never_live(git_setup):
    """Successful PR creation should write to scratch, push to origin, never touch live."""
    open_pr_tool = git_setup["open_pr_tool"]
    scratch = git_setup["scratch"]
    live = git_setup["live"]
    origin = git_setup["origin"]
    cache = git_setup["cache"]

    # Monkeypatch _create_pr with a fake
    async def fake_create(branch, title, body):
        return {
            "number": 7,
            "url": "https://github.com/owner/repo/pull/7",
            "title": title,
            "branch": branch,
            "state": "open",
        }

    open_pr_tool._create_pr = fake_create

    # Call the tool
    result = await open_pr_tool.run(
        None,
        branch="ares/patch-1",
        title="tweak",
        files=[{"path": "ares/newmod.py", "content": "# hi\n"}],
    )

    # Should succeed
    assert result.ok is True
    assert "7" in result.content
    assert "https://github.com/owner/repo/pull/7" in result.content

    # File should exist in scratch with correct content
    new_file = scratch / "ares" / "newmod.py"
    assert new_file.exists()
    assert new_file.read_text() == "# hi\n"

    # Origin should have the branch
    verify_result = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", "ares/patch-1"],
        capture_output=True,
        text=True,
    )
    assert verify_result.returncode == 0, "Branch not found on origin"

    # Live directory must stay empty
    assert list(live.rglob("*")) == []

    # Cache should have the entry
    all_entries = cache.all()
    assert len(all_entries) == 1
    assert all_entries[0]["number"] == 7


@pytest.mark.asyncio
async def test_get_pr_status_reports_merged(git_setup):
    """GetPRStatus should report merged state when PR is merged."""
    get_status_tool = git_setup["get_status_tool"]
    cache = git_setup["cache"]

    # Seed the cache with an entry
    cache.add({
        "number": 7,
        "url": "https://github.com/owner/repo/pull/7",
        "title": "test",
        "branch": "ares/patch-1",
        "state": "open",
    })

    # Monkeypatch _fetch to return merged status
    async def fake_fetch(number):
        return {"state": "closed", "merged": True}

    get_status_tool._fetch = fake_fetch

    # Call the tool
    result = await get_status_tool.run(None, number=7)

    assert result.ok is True
    assert "merged" in result.content
    assert "7" in result.content

    # Cache entry should be updated
    all_entries = cache.all()
    assert len(all_entries) == 1
    assert all_entries[0]["state"] == "merged"


@pytest.mark.asyncio
async def test_get_pr_status_reports_open(git_setup):
    """GetPRStatus should report open state when PR is open."""
    get_status_tool = git_setup["get_status_tool"]
    cache = git_setup["cache"]

    # Seed the cache with an entry
    cache.add({
        "number": 7,
        "url": "https://github.com/owner/repo/pull/7",
        "title": "test",
        "branch": "ares/patch-1",
        "state": "closed",
    })

    # Monkeypatch _fetch to return open status
    async def fake_fetch(number):
        return {"state": "open", "merged": False}

    get_status_tool._fetch = fake_fetch

    # Call the tool
    result = await get_status_tool.run(None, number=7)

    assert result.ok is True
    assert "open" in result.content
    assert "7" in result.content

    # Cache entry should be updated
    all_entries = cache.all()
    assert len(all_entries) == 1
    assert all_entries[0]["state"] == "open"
