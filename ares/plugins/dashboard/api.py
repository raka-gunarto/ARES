"""FastAPI app factory for the ARES web dashboard (spec §17).

All routes are thin wrappers over core objects passed in at construction;
no business logic lives in this module.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from ares.core.utils.logging import get_logger

logger = get_logger(__name__)

# The updater records the deployed commit here (see updater/aresupdater.py
# write_released_sha). Overridable for dev via ARES_RELEASED_SHA_FILE.
RELEASED_SHA_FILE = os.environ.get("ARES_RELEASED_SHA_FILE", "/opt/ares/RELEASED_SHA")


def read_released_sha() -> str | None:
    """Return the deployed commit SHA, or None if it isn't recorded/readable."""
    try:
        with open(RELEASED_SHA_FILE, "r", encoding="utf-8") as f:
            sha = f.read().strip()
    except OSError:
        return None
    return sha or None


def task_to_dict(task: Any) -> dict:
    """Convert a Task dataclass to a plain dict."""
    return dataclasses.asdict(task)


def req_to_dict(req: Any) -> dict:
    """Convert a PrivRequest dataclass to a plain dict."""
    return dataclasses.asdict(req)


def build_app(
    *,
    token: str,
    emit_chat: Callable[[str], Awaitable[None]],
    web_channel: Any,
    memory: Any,
    tasks: Any,
    priv_store: Any,
    prs_provider: Callable[[], list[dict]],
    health_provider: Callable[[], dict],
    static_dir: Path,
) -> FastAPI:
    """Build the FastAPI dashboard app.

    Args:
        token: Shared bearer token required on all /api/... routes.
        emit_chat: async callable(text) that emits a web_message event for
            user "primary".
        web_channel: WebChannel with .outbox(user_id) -> asyncio.Queue.
        memory: BaseMemory instance (list/read used here).
        tasks: TaskStore instance (list_open used here).
        priv_store: PrivStore instance, or None if privileges are disabled.
        prs_provider: sync callable () -> list[dict] of open self-edit PRs.
        health_provider: sync callable () -> dict of health/status info.
        static_dir: path to the dashboard static/ directory (index.html lives
            here).

    Returns:
        A configured FastAPI app.
    """
    app = FastAPI(title="ARES Dashboard")

    def require_token(request: Request) -> None:
        """Require a valid `Authorization: Bearer <token>` header.

        Uses a constant-time comparison to avoid timing side-channels.
        """
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(auth, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    api_auth = Depends(require_token)

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the dashboard's static index.html. No auth required."""
        return FileResponse(static_dir / "index.html")

    @app.get("/api/version")
    async def get_version() -> JSONResponse:
        """Deployed commit SHA. No auth: a commit SHA is not sensitive, and the
        lock screen shows it before a token is entered."""
        sha = read_released_sha()
        return JSONResponse({"sha": sha, "short": sha[:8] if sha else None})

    @app.post("/api/chat", dependencies=[api_auth])
    async def post_chat(request: Request) -> JSONResponse:
        """Accept a chat message from the operator and emit a web_message."""
        body = await request.json()
        text = body["text"]
        await emit_chat(text)
        return JSONResponse(status_code=202, content={"status": "accepted"})

    @app.get("/api/chat/poll", dependencies=[api_auth])
    async def poll_chat(since: str | None = None) -> dict:
        """Long-poll (<=25s) for new outbox messages for user "primary".

        `since` is accepted for future cursor-based use but ignored in v1 --
        the outbox is a plain queue, drained in FIFO order.
        """
        q: asyncio.Queue = web_channel.outbox("primary")
        try:
            msg = await asyncio.wait_for(q.get(), timeout=25)
            msgs = [msg]
        except asyncio.TimeoutError:
            msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        return {"messages": msgs}

    @app.get("/api/memory/list", dependencies=[api_auth])
    async def memory_list() -> PlainTextResponse:
        """List all memory files (delegates to BaseMemory.list)."""
        return PlainTextResponse(await memory.list())

    @app.get("/api/memory/file", dependencies=[api_auth])
    async def memory_file(path: str) -> PlainTextResponse:
        """Read a memory file (delegates to BaseMemory.read; read-only).

        memory.read() is itself path-safe: it returns an error string for
        path escapes or non-.md files and never reads outside the memory
        root, so passing `path` straight through is safe.
        """
        return PlainTextResponse(await memory.read(path))

    @app.get("/api/tasks", dependencies=[api_auth])
    async def get_tasks(status: str = "open") -> JSONResponse:
        """List open tasks for user "primary"."""
        result = await tasks.list_open("primary")
        return JSONResponse([task_to_dict(t) for t in result])

    @app.get("/api/privileges", dependencies=[api_auth])
    async def get_privileges(status: str = "pending") -> JSONResponse:
        """List privilege requests filtered by status."""
        if priv_store is None:
            return JSONResponse([])
        result = await priv_store.list(status)
        return JSONResponse([req_to_dict(r) for r in result])

    @app.post("/api/privileges/{req_id}/approve", dependencies=[api_auth])
    async def approve_privilege(req_id: str) -> JSONResponse:
        """Approve a pending privilege request. Operator gate (spec §16/§17)."""
        if priv_store is None:
            raise HTTPException(status_code=404, detail="privileges disabled")
        r = await priv_store.approve(req_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not pending")
        return JSONResponse(req_to_dict(r))

    @app.post("/api/privileges/{req_id}/deny", dependencies=[api_auth])
    async def deny_privilege(req_id: str) -> JSONResponse:
        """Deny a pending privilege request. Operator gate (spec §16/§17)."""
        if priv_store is None:
            raise HTTPException(status_code=404, detail="privileges disabled")
        r = await priv_store.deny(req_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not pending")
        return JSONResponse(req_to_dict(r))

    @app.get("/api/prs", dependencies=[api_auth])
    async def get_prs() -> JSONResponse:
        """List open self-edit PRs."""
        return JSONResponse(prs_provider())

    @app.get("/api/health", dependencies=[api_auth])
    async def get_health() -> JSONResponse:
        """Return health/status info."""
        return JSONResponse(health_provider())

    return app
