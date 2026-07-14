"""Self-edit -> pull request workflow (spec §18).

ARES may propose changes to its own code by opening a GitHub pull request. It can
never merge them — merging is a human action gated by branch protection.

SECURITY (PATCH-3, fixes C1/C2): ALL token-bearing work happens in the daemon
process via the GitHub REST/Git-Data API over httpx. There is NO local git clone
and NO sandbox exec, so:
  - the `GITHUB_TOKEN` never touches disk or the `ares-sbx` sandbox that runs
    arbitrary shells — ARES cannot read its own token (C1); and
  - the daemon only ever creates a NEW branch ref + a PR against `base_branch`;
    it never updates the base ref, and `open_pr` refuses `branch == base_branch`,
    so ARES cannot push to `main` even with a write-scoped token (C2).
The operator merging the PR on GitHub remains the sole path to running code.
"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx

import ares
from ares.core.tool import BaseTool, ToolContext, ToolResult
from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"

# Read cap: the largest source file in the tree is ~43 KiB, so 256 KiB round-trips
# any real source file whole while bounding what a single read can dump into context.
READ_SOURCE_MAX_BYTES = 256 * 1024
# Build/cache noise omitted from directory listings (not secrets — just clutter).
_LISTING_NOISE = frozenset(
    {"__pycache__", ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"}
)


def _source_root() -> Path:
    """The root of ARES's own source tree, derived from the running `ares` package.

    `<root>/ares/__init__.py` -> parents[1] == `<root>`: `/opt/ares/app` in prod
    (following the release symlink), the repo checkout in dev. Never configured, so
    the model cannot point self-inspection at an arbitrary tree.
    """
    return Path(ares.__file__).resolve().parents[1]


def _is_secret_name(name: str) -> bool:
    """True for the secrets dotenv and its variants; the `.env.example` template is not.

    Secrets come only from the environment (§14.2); `read_source` never exposes them.
    """
    return name.startswith(".env") and name != ".env.example"


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


def _validate_path(raw_path: str) -> str | None:
    """Return an error string if `raw_path` is not a safe repo-relative path."""
    if not raw_path or raw_path.strip() == "":
        return "error: empty file path"
    if raw_path.startswith("/"):
        return f"error: path '{raw_path}' must be repo-relative, not absolute"
    parts = raw_path.split("/")
    if ".." in parts or "." in parts:
        return f"error: path '{raw_path}' must not contain '.' or '..' segments"
    return None


class ReadSource(BaseTool):
    """Read ARES's own source (read-only) so it can reason about a change (§18).

    Daemon-side and read-only: it surfaces the `ares` user's existing RO access to
    `/opt/ares/app` (§14.2) to the model. Scoped to the source root, refuses to
    leave it (even via symlink), and never reads the secrets file.
    """

    name = "read_source"
    description = (
        "Read ARES's own source code (read-only self-inspection) so it can reason "
        "about what to change before proposing an edit with open_pr. Takes a "
        "repo-relative path: a file returns its contents, a directory returns a "
        "listing. Cannot read outside the source tree and never exposes secrets."
    )
    keywords = ("read", "source", "code", "inspect", "view", "file", "list", "self", "own")
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "repo-relative path; empty or '.' lists the source root",
            }
        },
    }
    core = False

    def __init__(self, root: Path | None = None, max_bytes: int = READ_SOURCE_MAX_BYTES) -> None:
        """Pin the source root (default: derived from the running `ares` package)."""
        self._root = (root if root is not None else _source_root()).resolve()
        self._max_bytes = max_bytes

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Read a file or list a directory within the source tree (§18)."""
        raw = (kwargs.get("path") or "").strip()
        rel = raw.strip("/")
        if rel == ".":
            rel = ""

        if rel:
            parts = rel.split("/")
            if ".." in parts or "." in parts:
                return ToolResult(
                    False, f"error: path '{raw}' must not contain '.' or '..' segments"
                )
            if any(_is_secret_name(p) for p in parts):
                return ToolResult(
                    False, f"error: path '{raw}' is not readable (secrets are never exposed)"
                )

        target = (self._root / rel).resolve() if rel else self._root
        # Defense in depth: after following symlinks the target must stay in-root.
        if target != self._root and self._root not in target.parents:
            return ToolResult(False, f"error: path '{raw}' escapes the source tree")
        if not target.exists():
            return ToolResult(False, f"error: no such source path '{raw or '.'}'")

        if target.is_dir():
            return self._list_dir(target, rel)
        return self._read_file(target, raw)

    def _list_dir(self, target: Path, rel: str) -> ToolResult:
        """Return a sorted listing, dirs flagged with '/', noise and secrets omitted."""
        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if child.name in _LISTING_NOISE or _is_secret_name(child.name):
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        label = rel or "."
        if not entries:
            return ToolResult(True, f"{label}/ (empty)")
        return ToolResult(True, f"{label}/\n" + "\n".join(f"  {e}" for e in entries))

    def _read_file(self, target: Path, raw: str) -> ToolResult:
        """Return file contents (UTF-8, capped), marking truncation explicitly."""
        try:
            data = target.read_bytes()
        except OSError as e:
            return ToolResult(False, f"error: cannot read '{raw}': {e}")
        text = data[: self._max_bytes].decode("utf-8", errors="replace")
        if len(data) > self._max_bytes:
            text += (
                f"\n\n[... truncated at {self._max_bytes} of {len(data)} bytes; "
                "content is INCOMPLETE — do not feed this back into open_pr]"
            )
        return ToolResult(True, text)


class OpenPR(BaseTool):
    """Propose a change to ARES's own code by opening a GitHub PR."""

    name = "open_pr"
    description = (
        "Propose a change to ARES's own source code by opening a GitHub pull "
        "request via the GitHub API. ARES can never merge this PR — an operator "
        "reviews and merges it on GitHub. Never targets the base branch."
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
        github_repo: str,
        github_token: str,
        base_branch: str = "main",
        cache: PRCache | None = None,
    ) -> None:
        """Store self-edit configuration. No scratch/sandbox — all API-driven."""
        self.github_repo = github_repo
        self.github_token = github_token
        self.base_branch = base_branch or "main"
        self._cache = cache if cache is not None else PRCache()

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Create a branch + commit + PR via the GitHub API (§18)."""
        branch = (kwargs.get("branch") or "").strip()
        title = kwargs.get("title") or ""
        body = kwargs.get("body") or ""
        files = kwargs.get("files") or []

        if not branch or not title:
            return ToolResult(False, "error: branch and title are required")
        if not files:
            return ToolResult(False, "error: files must be a non-empty list")

        # SECURITY (C2): never target the base branch — only ever a NEW branch.
        if branch == self.base_branch:
            return ToolResult(
                False,
                f"error: refusing to target the base branch '{self.base_branch}'; "
                "open_pr proposes changes on a NEW branch for the operator to merge",
            )

        # SECURITY: validate every path before doing any API work.
        for f in files:
            err = _validate_path(f.get("path", ""))
            if err:
                return ToolResult(False, err)

        try:
            # 1. base commit + tree of the base branch.
            st, ref = await self._github("GET", f"git/ref/heads/{self.base_branch}")
            if st != 200:
                return ToolResult(False, f"error: cannot read base branch (HTTP {st})")
            base_sha = ref["object"]["sha"]
            st, commit = await self._github("GET", f"git/commits/{base_sha}")
            if st != 200:
                return ToolResult(False, f"error: cannot read base commit (HTTP {st})")
            base_tree = commit["tree"]["sha"]

            # 2. a blob per file.
            tree_items: list[dict] = []
            for f in files:
                content_b64 = base64.b64encode(
                    (f.get("content") or "").encode("utf-8")
                ).decode("ascii")
                st, blob = await self._github(
                    "POST", "git/blobs", {"content": content_b64, "encoding": "base64"}
                )
                if st != 201:
                    return ToolResult(False, f"error: blob create failed (HTTP {st})")
                tree_items.append(
                    {"path": f["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]}
                )

            # 3. tree, 4. commit (author ARES), 5. new branch ref.
            st, tree = await self._github(
                "POST", "git/trees", {"base_tree": base_tree, "tree": tree_items}
            )
            if st != 201:
                return ToolResult(False, f"error: tree create failed (HTTP {st})")
            st, new_commit = await self._github(
                "POST",
                "git/commits",
                {
                    "message": title,
                    "tree": tree["sha"],
                    "parents": [base_sha],
                    "author": {"name": "ARES", "email": "ares@localhost"},
                },
            )
            if st != 201:
                return ToolResult(False, f"error: commit create failed (HTTP {st})")
            commit_sha = new_commit["sha"]

            st, _ = await self._github(
                "POST",
                "git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
            if st == 422:  # ref exists -> fast-forward it to the new commit
                st, _ = await self._github(
                    "PATCH", f"git/refs/heads/{branch}", {"sha": commit_sha, "force": True}
                )
            if st not in (200, 201):
                return ToolResult(False, f"error: branch ref update failed (HTTP {st})")

            # 6. the PR.
            st, pr = await self._github(
                "POST",
                "pulls",
                {"title": title, "head": branch, "base": self.base_branch, "body": body},
            )
            if st != 201:
                return ToolResult(
                    False,
                    f"error: PR create failed (HTTP {st}); branch pushed as '{branch}'",
                )
        except httpx.HTTPError as e:
            return ToolResult(False, f"error: GitHub API request failed: {e}")

        entry = {
            "number": pr["number"],
            "url": pr["html_url"],
            "title": title,
            "branch": branch,
            "state": "open",
        }
        self._cache.add(entry)
        return ToolResult(True, f"opened PR #{entry['number']}: {entry['url']}")

    async def _github(
        self, method: str, path: str, json: dict | None = None
    ) -> tuple[int, dict]:
        """One authenticated GitHub API call. Isolated seam (monkeypatched in tests).

        The token lives only in the Authorization header of this daemon-side
        request — never on disk, never in a ToolResult or log.
        """
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"{GITHUB_API}/repos/{self.github_repo}/{path}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(method, url, headers=headers, json=json)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data


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
        "properties": {"number": {"type": "integer"}},
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
                f"{GITHUB_API}/repos/{self.github_repo}/pulls/{number}", headers=headers
            )
        if response.status_code != 200:
            raise RuntimeError(f"GitHub PR fetch failed: HTTP {response.status_code}")
        return response.json()


def build_selfedit_tools(config: dict, cache: PRCache) -> list[BaseTool]:
    """Factory for the self-edit tool plugin, used by main.py."""
    github_repo = config.get("github_repo", "")
    github_token = config.get("github_token", "")
    base_branch = config.get("base_branch", "main")
    return [
        ReadSource(),
        OpenPR(github_repo, github_token, base_branch, cache),
        GetPRStatus(github_repo, github_token, cache),
    ]
