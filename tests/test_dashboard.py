from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from ares.plugins.dashboard.api import build_app
from ares.plugins.dashboard.channel import WebChannel
from ares.core.memory.filesystem import FilesystemMemory
from ares.plugins.privileges.store import PrivStore


TOKEN = "secret-token-xyz"


@pytest.fixture
async def web_channel() -> WebChannel:
    """Create a WebChannel for testing."""
    return WebChannel()


@pytest.fixture
async def memory(tmp_path: Path) -> FilesystemMemory:
    """Create a FilesystemMemory with a test file."""
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    memory = FilesystemMemory(mem_root)
    # Write a known test file
    await memory.write("note.md", "hello-memory", "overwrite")
    return memory


@pytest.fixture
async def priv_store(tmp_path: Path) -> PrivStore:
    """Create and initialize a PrivStore for testing."""
    store = PrivStore(tmp_path / "privq.db")
    await store.init()
    yield store
    await store.aclose()


@pytest.fixture
async def tasks_stub() -> Any:
    """Stub object with list_open method."""

    class TasksStub:
        async def list_open(self, user_id: str) -> list:
            return []

    return TasksStub()


@pytest.fixture
async def app(
    web_channel: WebChannel,
    memory: FilesystemMemory,
    tasks_stub: Any,
    priv_store: PrivStore,
) -> Any:
    """Build the FastAPI app for testing."""
    chat_messages: list[str] = []

    async def emit_chat(text: str) -> None:
        """Record chat messages."""
        chat_messages.append(text)

    static_dir = Path(__file__).parent.parent / "ares" / "plugins" / "dashboard" / "static"

    return build_app(
        token=TOKEN,
        emit_chat=emit_chat,
        web_channel=web_channel,
        memory=memory,
        tasks=tasks_stub,
        priv_store=priv_store,
        prs_provider=lambda: [],
        health_provider=lambda: {"ok": True, "uptime": 0.0, "queue_depths": {}},
        static_dir=static_dir,
    )


@pytest.fixture
async def client(app: Any) -> httpx.AsyncClient:
    """Create an async HTTP client for the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestTokenAuth:
    """Test token authentication on /api routes."""

    async def test_health_without_auth_returns_401(self, client: httpx.AsyncClient) -> None:
        """GET /api/health with no Authorization header should return 401."""
        resp = await client.get("/api/health")
        assert resp.status_code == 401

    async def test_health_with_wrong_token_returns_401(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /api/health with wrong Bearer token should return 401."""
        resp = await client.get(
            "/api/health", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 401

    async def test_health_with_correct_token_returns_200(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /api/health with correct Bearer token should return 200 with ok=True."""
        resp = await client.get(
            "/api/health", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "uptime" in data
        assert "queue_depths" in data

    async def test_index_requires_no_auth(self, client: httpx.AsyncClient) -> None:
        """GET / should work without Authorization header."""
        resp = await client.get("/")
        assert resp.status_code == 200
        # The index.html file should be served
        assert "<!DOCTYPE" in resp.text or "<html" in resp.text.lower()


class TestMemoryPathSafety:
    """Test memory file read safety and path validation."""

    async def test_memory_file_escapes_root_returns_error(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /api/memory/file?path=../../etc/passwd should return error, not file content."""
        resp = await client.get(
            "/api/memory/file?path=../../etc/passwd",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Should contain error message
        assert "error" in body.lower()
        assert "invalid path" in body
        # Should NOT contain passwd-like content
        assert "root:" not in body

    async def test_memory_file_non_md_returns_error(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /api/memory/file?path=../setup.py should return error for non-.md file."""
        resp = await client.get(
            "/api/memory/file?path=../setup.py",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Should contain error message
        assert "error" in body.lower()
        assert "invalid path" in body

    async def test_memory_file_valid_path_returns_content(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /api/memory/file?path=note.md should return the known test content."""
        resp = await client.get(
            "/api/memory/file?path=note.md",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.text == "hello-memory"


class TestPrivilegeApproval:
    """Test privilege request approval workflow."""

    async def test_approve_flips_db_status(
        self, client: httpx.AsyncClient, priv_store: PrivStore
    ) -> None:
        """Test that approving a privilege request changes its status from pending to approved."""
        # Create a pending privilege request
        req = await priv_store.create("primary", "package_install", "htop", "monitoring")
        req_id = req.id

        # Verify it starts as pending
        pending = await priv_store.list("pending")
        assert len(pending) == 1
        assert pending[0].id == req_id
        assert pending[0].status == "pending"

        # Approve via the API
        resp = await client.post(
            f"/api/privileges/{req_id}/approve",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert data["id"] == req_id

        # Re-approve should fail with 404 (no longer pending)
        resp = await client.post(
            f"/api/privileges/{req_id}/approve",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 404

        # Verify the DB was flipped
        pending_after = await priv_store.list("pending")
        assert len(pending_after) == 0

        approved = await priv_store.list("approved")
        assert len(approved) == 1
        assert approved[0].id == req_id
        assert approved[0].status == "approved"


class TestChatSubmission:
    """Test chat message submission."""

    async def test_post_chat_accepted_and_recorded(
        self, app: Any, client: httpx.AsyncClient
    ) -> None:
        """Test that POST /api/chat with token submits message and records it."""
        # We need to extract the chat_messages list from the app's emit_chat closure
        # The app fixture builds the app with emit_chat that records to a list
        # We'll make a new client and app to capture the messages

        chat_messages: list[str] = []

        async def emit_chat(text: str) -> None:
            chat_messages.append(text)

        static_dir = Path(__file__).parent.parent / "ares" / "plugins" / "dashboard" / "static"

        test_app = build_app(
            token=TOKEN,
            emit_chat=emit_chat,
            web_channel=WebChannel(),
            memory=FilesystemMemory(Path(__file__).parent / "tmp_chat_test"),
            tasks=tasks_stub_instance(),
            priv_store=None,
            prs_provider=lambda: [],
            health_provider=lambda: {"ok": True, "uptime": 0.0, "queue_depths": {}},
            static_dir=static_dir,
        )

        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/chat",
                json={"text": "hi"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "accepted"

            # Verify the message was recorded
            assert len(chat_messages) == 1
            assert chat_messages[0] == "hi"


def tasks_stub_instance() -> Any:
    """Create a stub tasks object with list_open method."""

    class TasksStub:
        async def list_open(self, user_id: str) -> list:
            return []

    return TasksStub()
