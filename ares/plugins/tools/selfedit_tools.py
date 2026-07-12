"""Self-edit -> pull request workflow (spec §18).

ARES may propose changes to its own code by writing files into a scratch
git clone and opening a GitHub pull request. It never writes its own live
code (`live_code_path`) and never merges its own PR — merging is a human
action gated by GitHub branch protection.

All git operations run inside `scratch_repo` only, executed as the
unprivileged sandbox user when one is configured (mirrors
`shell_tools.RunShell`'s exec pattern), never as the daemon user.
"""
from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path

import httpx

from ares.core.tool import BaseTool, ToolContext, ToolResult
from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

DEV_WARNING = (
    "[warning: sandbox_user not configured; running git as the daemon user — DEV ONLY]\n"
)

GITHUB_API = "https://api.github.com"


class PRCache:
    """Small in-memory cache of opened PRs, for /api/prs (§18)."""

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self._entries: list[dict] = []

    def add(self, entry: dict) -> None:
        """Add an entry, replacing any existing entry with the same 'number'."""
        number = entry.get("number")
        if number is not None:
            for i, existing in enumerate(self._entries):
                if existing.get("number") == number:
                    self._entries[i] = entry
                    return
        self._entries.append(entry)

    def all(self) -> list[dict]:
        """Return a shallow copy of all cached entries."""
        return list(self._entries)


async def _run_git(
    args: list[str], sandbox_user: str, cwd: str, secret: str = ""
) -> tuple[int, str]:
    """Run a git command, as the sandbox user when configured (mirrors RunShell).

    `secret` (the GitHub token) is scrubbed from the returned output so a git
    error that echoes the token-embedded remote URL can never leak it into a
    ToolResult or log.
    """

    def _scrub(text: str) -> str:
        return text.replace(secret, "***") if secret else text

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": cwd or "/tmp",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }

    warning = ""
    me = getpass.getuser()
    env_mode = os.environ.get("ARES_ENV", "dev")
    if env_mode == "prod" and (not sandbox_user or sandbox_user == me):
        logger.error("selfedit git op refused: would run as the daemon user in prod")
        return 1, "error: git execution refused (no sandbox user separation in prod)"

    if sandbox_user:
        argv = ["sudo", "-n", "-u", sandbox_user, "git"] + args
    else:
        argv = ["git"] + args
        warning = DEV_WARNING

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=(cwd or None),
            env=env,
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        return 1, _scrub(f"error: failed to run git: {e}")

    stdout, _ = await proc.communicate()
    output = warning + _scrub(stdout.decode(errors="replace"))
    return proc.returncode, output


class OpenPR(BaseTool):
    """Propose a change to ARES's own code by opening a GitHub PR."""

    name = "open_pr"
    description = (
        "Propose a change to ARES's own source code by writing files into a "
        "scratch git clone, committing, and opening a GitHub pull request. "
        "ARES can never merge this PR — an operator reviews and merges it on "
        "GitHub."
    )
    keywords = (
        "code",
        "edit",
        "self",
        "pr",
        "pull",
        "request",
        "patch",
        "improve",
        "fix",
        "propose",
    )
    parameters = {
        "type": "object",
        "properties": {
            "branch": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["branch", "title", "files"],
    }
    core = False

    def __init__(
        self,
        scratch_repo: str,
        live_code_path: str,
        github_repo: str,
        github_token: str,
        sandbox_user: str = "",
        cache: PRCache | None = None,
    ) -> None:
        """Store self-edit configuration; live_code_path is stored only to never be written to."""
        self.scratch_repo = scratch_repo
        self.live_code_path = live_code_path
        self.github_repo = github_repo
        self.github_token = github_token
        self.sandbox_user = sandbox_user
        self._cache = cache if cache is not None else PRCache()

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Write files into the scratch clone, commit, push, and open a PR (§18)."""
        branch = kwargs.get("branch", "")
        title = kwargs.get("title", "")
        body = kwargs.get("body", "")
        files = kwargs.get("files") or []

        if not branch or not title:
            return ToolResult(False, "error: branch and title are required")
        if not files:
            return ToolResult(False, "error: files must be a non-empty list")

        # SECURITY: validate every path BEFORE any git/network work. Never
        # derive a write path from live_code_path.
        scratch_root = Path(self.scratch_repo).resolve()
        safe_paths: list[tuple[Path, str]] = []
        for f in files:
            raw_path = f.get("path", "")
            content = f.get("content", "")
            if os.path.isabs(raw_path):
                return ToolResult(
                    False, f"error: path '{raw_path}' escapes the scratch repo"
                )
            resolved = (scratch_root / raw_path).resolve()
            try:
                resolved.relative_to(scratch_root)
            except ValueError:
                return ToolResult(
                    False, f"error: path '{raw_path}' escapes the scratch repo"
                )
            safe_paths.append((resolved, content))

        # Ensure the scratch clone exists.
        if not (scratch_root / ".git").exists():
            clone_url = (
                f"https://x-access-token:{self.github_token}@github.com/"
                f"{self.github_repo}.git"
            )
            scratch_root.parent.mkdir(parents=True, exist_ok=True)
            rc, out = await _run_git(
                ["clone", clone_url, str(scratch_root)],
                self.sandbox_user,
                str(scratch_root.parent),
                self.github_token,
            )
            if rc != 0:
                return ToolResult(False, f"clone failed: {out}")

        rc, out = await _run_git(
            ["fetch", "origin"], self.sandbox_user, str(scratch_root), self.github_token
        )
        if rc != 0:
            return ToolResult(False, f"fetch failed: {out}")

        rc, out = await _run_git(
            ["checkout", "-B", branch, "origin/main"], self.sandbox_user, str(scratch_root)
        )
        if rc != 0:
            return ToolResult(False, f"checkout failed: {out}")

        rc, out = await _run_git(
            ["reset", "--hard", "origin/main"], self.sandbox_user, str(scratch_root)
        )
        if rc != 0:
            return ToolResult(False, f"reset failed: {out}")

        # Write each file's content — the only writes performed.
        try:
            for resolved, content in safe_paths:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(False, f"error: failed to write files: {e}")

        rc, out = await _run_git(["add", "-A"], self.sandbox_user, str(scratch_root))
        if rc != 0:
            return ToolResult(False, f"add failed: {out}")

        rc, out = await _run_git(
            [
                "-c",
                "user.name=ARES",
                "-c",
                "user.email=ares@localhost",
                "commit",
                "-m",
                title,
            ],
            self.sandbox_user,
            str(scratch_root),
        )
        if rc != 0:
            return ToolResult(False, f"commit failed (nothing to commit?): {out}")

        rc, out = await _run_git(
            ["push", "--force-with-lease", "origin", branch],
            self.sandbox_user,
            str(scratch_root),
            self.github_token,
        )
        if rc != 0:
            return ToolResult(False, f"push failed: {out}")

        try:
            entry = await self._create_pr(branch, title, body)
        except (RuntimeError, httpx.HTTPError) as e:
            return ToolResult(False, f"error: failed to open PR: {e}")

        self._cache.add(entry)
        return ToolResult(True, f"opened PR #{entry['number']}: {entry['url']}")

    async def _create_pr(self, branch: str, title: str, body: str) -> dict:
        """Create the PR via GitHub REST. Isolated for testability (monkeypatch target)."""
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{GITHUB_API}/repos/{self.github_repo}/pulls",
                headers=headers,
                json={"title": title, "head": branch, "base": "main", "body": body},
            )
        if response.status_code != 201:
            raise RuntimeError(f"GitHub PR create failed: HTTP {response.status_code}")

        data = response.json()
        return {
            "number": data["number"],
            "url": data["html_url"],
            "title": title,
            "branch": branch,
            "state": "open",
        }


class GetPRStatus(BaseTool):
    """Report whether a self-edit PR is open, closed, or merged."""

    name = "get_pr_status"
    description = (
        "Check the status of a previously opened self-edit pull request "
        "(open, closed, or merged) so ARES knows if the operator accepted "
        "its change."
    )
    keywords = ("pr", "pull", "request", "status", "merged", "review", "code")
    parameters = {
        "type": "object",
        "properties": {
            "number": {"type": "integer"},
        },
        "required": ["number"],
    }
    core = False

    def __init__(
        self, github_repo: str, github_token: str, cache: PRCache | None = None
    ) -> None:
        """Store GitHub repo/token and the shared PR cache."""
        self.github_repo = github_repo
        self.github_token = github_token
        self._cache = cache if cache is not None else PRCache()

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Fetch a PR's status from GitHub and update the shared cache."""
        number = kwargs.get("number")
        if number is None:
            return ToolResult(False, "error: number is required")

        try:
            data = await self._fetch(number)
        except (RuntimeError, httpx.HTTPError) as e:
            return ToolResult(False, f"error: failed to fetch PR status: {e}")

        state = "merged" if data.get("merged") else data.get("state", "unknown")

        for entry in self._cache.all():
            if entry.get("number") == number:
                entry["state"] = state
                self._cache.add(entry)
                break

        return ToolResult(True, f"PR #{number}: {state}")

    async def _fetch(self, number: int) -> dict:
        """GET the PR from GitHub REST. Isolated for testability (monkeypatch target)."""
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{GITHUB_API}/repos/{self.github_repo}/pulls/{number}",
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub PR fetch failed: HTTP {response.status_code}")
        return response.json()


def build_selfedit_tools(config: dict, cache: PRCache) -> list[BaseTool]:
    """Factory for the self-edit tool plugin, used by main.py."""
    scratch_repo = config.get("scratch_repo", "")
    live_code_path = config.get("live_code_path", "")
    github_repo = config.get("github_repo", "")
    github_token = config.get("github_token", "")
    sandbox_user = config.get("sandbox_user", "")

    return [
        OpenPR(
            scratch_repo,
            live_code_path,
            github_repo,
            github_token,
            sandbox_user,
            cache,
        ),
        GetPRStatus(github_repo, github_token, cache),
    ]
