"""Self-edit tests (PATCH-3): fully API-driven, token never leaves the daemon.

open_pr uses the GitHub Git-Data API (mocked here via the `_github` seam) — there
is no local git clone and no sandbox exec, so there is no token-on-disk path and
no `sudo` path. Covers: path-escape rejection, base-branch guard (can't push to
main), the happy path (branch ref + PR created, cached), and get_pr_status.
"""
from __future__ import annotations

from pathlib import Path

from ares.plugins.tools.selfedit_tools import (
    PRCache,
    ReadSource,
    build_selfedit_tools,
)


def _tools(cache: PRCache):
    config = {"github_repo": "owner/repo", "github_token": "ghp-secret", "base_branch": "main"}
    _read_source, open_pr, get_status = build_selfedit_tools(config, cache)
    return open_pr, get_status


def _fake_github():
    """A scripted GitHub Git-Data API. Records calls; returns plausible responses."""
    calls: list[tuple[str, str, dict | None]] = []

    async def _github(method: str, path: str, json=None):
        calls.append((method, path, json))
        if method == "GET" and path.startswith("git/ref/heads/"):
            return 200, {"object": {"sha": "basesha"}}
        if method == "GET" and path.startswith("git/commits/"):
            return 200, {"tree": {"sha": "basetree"}}
        if method == "POST" and path == "git/blobs":
            return 201, {"sha": f"blob{len(calls)}"}
        if method == "POST" and path == "git/trees":
            return 201, {"sha": "newtree"}
        if method == "POST" and path == "git/commits":
            return 201, {"sha": "newcommit"}
        if method == "POST" and path == "git/refs":
            return 201, {}
        if method == "POST" and path == "pulls":
            return 201, {"number": 11, "html_url": "https://github.com/owner/repo/pull/11"}
        return 500, {}

    return _github, calls


async def test_absolute_path_rejected():
    open_pr, _ = _tools(PRCache())
    called = {"hit": False}

    async def _github(*a, **k):
        called["hit"] = True
        return 200, {}

    open_pr._github = _github
    r = await open_pr.run(None, branch="x", title="t", files=[{"path": "/etc/pwned", "content": "x"}])
    assert r.ok is False and "absolute" in r.content.lower()
    assert called["hit"] is False, "no API call should happen for a rejected path"


async def test_parent_escape_rejected():
    open_pr, _ = _tools(PRCache())
    r = await open_pr.run(None, branch="x", title="t", files=[{"path": "../../etc/x", "content": "y"}])
    assert r.ok is False and ".." in r.content


async def test_refuses_base_branch():
    """C2: open_pr must never target the base branch (would be a push to main)."""
    open_pr, _ = _tools(PRCache())
    called = {"hit": False}

    async def _github(*a, **k):
        called["hit"] = True
        return 200, {}

    open_pr._github = _github
    r = await open_pr.run(None, branch="main", title="t", files=[{"path": "a.py", "content": "x"}])
    assert r.ok is False and "base branch" in r.content.lower()
    assert called["hit"] is False, "refused before any API call"


async def test_happy_path_creates_branch_and_pr_and_caches():
    cache = PRCache()
    open_pr, _ = _tools(cache)
    github, calls = _fake_github()
    open_pr._github = github

    r = await open_pr.run(
        None,
        branch="ares/patch-1",
        title="tidy logging",
        body="small cleanup",
        files=[{"path": "ares/newmod.py", "content": "# hi\n"}],
    )
    assert r.ok is True and "11" in r.content

    methods = [(m, p) for (m, p, _) in calls]
    # A new branch ref is created and a PR opened; the base ref is NEVER updated.
    assert ("POST", "git/refs") in methods
    assert ("POST", "pulls") in methods
    assert not any(p.startswith("git/refs/heads/main") for (_, p) in methods)
    # The PR head is the new branch, base is main.
    pr_body = next(j for (m, p, j) in calls if p == "pulls")
    assert pr_body["head"] == "ares/patch-1" and pr_body["base"] == "main"
    # Cached for /api/prs.
    assert cache.all()[0]["number"] == 11


async def test_token_never_in_result():
    """The token must never appear in tool output (it lives only in the header)."""
    open_pr, _ = _tools(PRCache())
    github, _ = _fake_github()
    open_pr._github = github
    r = await open_pr.run(None, branch="b", title="t", files=[{"path": "a.py", "content": "x"}])
    assert "ghp-secret" not in r.content


async def test_get_pr_status_merged():
    cache = PRCache()
    cache.add({"number": 11, "url": "u", "title": "t", "branch": "b", "state": "open"})
    _, get_status = _tools(cache)

    async def _fetch(number):
        return {"state": "closed", "merged": True}

    get_status._fetch = _fetch
    r = await get_status.run(None, number=11)
    assert r.ok is True and "merged" in r.content
    assert cache.all()[0]["state"] == "merged"


# --- read_source (v1.3): read-only, scoped self-inspection ---


def _src_tree(tmp_path: Path) -> Path:
    """A small fake source tree rooted at tmp_path (stand-in for /opt/ares/app)."""
    (tmp_path / "ares" / "core").mkdir(parents=True)
    (tmp_path / "ares" / "core" / "agent.py").write_text("# agent\nX = 1\n")
    (tmp_path / "ares" / "__pycache__").mkdir()
    (tmp_path / "ares" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (tmp_path / "instance").mkdir()
    (tmp_path / "instance" / ".env").write_text("GITHUB_TOKEN=supersecret\n")
    (tmp_path / "instance" / ".env.example").write_text("GITHUB_TOKEN=\n")
    return tmp_path


async def test_read_source_reads_a_file(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="ares/core/agent.py")
    assert r.ok is True and r.content == "# agent\nX = 1\n"


async def test_read_source_lists_a_directory_skipping_noise(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="ares")
    assert r.ok is True
    assert "core/" in r.content  # dirs flagged with a trailing slash
    assert "__pycache__" not in r.content  # build noise omitted


async def test_read_source_root_listing_on_empty_path(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="")
    assert r.ok is True and "ares/" in r.content and "instance/" in r.content


async def test_read_source_never_reads_secrets(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="instance/.env")
    assert r.ok is False and "supersecret" not in r.content
    # ...and the secret is omitted from a directory listing, but the template stays.
    listing = await rs.run(None, path="instance")
    assert ".env.example" in listing.content and "\n  .env\n" not in listing.content + "\n"


async def test_read_source_env_example_is_readable(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="instance/.env.example")
    assert r.ok is True and r.content == "GITHUB_TOKEN=\n"


async def test_read_source_rejects_absolute_and_traversal(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    assert (await rs.run(None, path="/etc/passwd")).ok is False
    r = await rs.run(None, path="../../etc/passwd")
    assert r.ok is False and ".." in r.content


async def test_read_source_rejects_symlink_escape(tmp_path):
    root = _src_tree(tmp_path)
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("leaked\n")
    (root / "ares" / "link.txt").symlink_to(outside)
    r = await ReadSource(root=root).run(None, path="ares/link.txt")
    assert r.ok is False and "leaked" not in r.content


async def test_read_source_missing_path(tmp_path):
    rs = ReadSource(root=_src_tree(tmp_path))
    r = await rs.run(None, path="ares/nope.py")
    assert r.ok is False and "no such" in r.content.lower()


async def test_read_source_truncates_large_file(tmp_path):
    root = _src_tree(tmp_path)
    (root / "big.txt").write_text("a" * 5000)
    r = await ReadSource(root=root, max_bytes=1000).run(None, path="big.txt")
    assert r.ok is True and "truncated" in r.content and "INCOMPLETE" in r.content
    assert r.content.startswith("a" * 1000) and not r.content.startswith("a" * 1001)
