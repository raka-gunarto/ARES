"""Dashboard auth watch (§17.4): log and alert on requests without a valid token."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from ares.plugins.channels.push_ntfy import NtfyChannel
from ares.plugins.dashboard import auth_watch
from ares.plugins.dashboard.api import build_app
from ares.plugins.dashboard.auth_watch import AuthWatch
from ares.plugins.dashboard.channel import WebChannel

TOKEN = "secret-token-xyz"
STATIC = Path(__file__).parent.parent / "ares" / "plugins" / "dashboard" / "static"


class Tasks:
    async def list_open(self, user_id):
        return []


def _client(alerts: list) -> httpx.AsyncClient:
    async def alert(title, message):
        alerts.append((title, message))

    async def emit_chat(text):
        pass

    app = build_app(
        token=TOKEN, emit_chat=emit_chat, web_channel=WebChannel(), memory=None,
        tasks=Tasks(), priv_store=None, prs_provider=lambda: [],
        health_provider=lambda: {"ok": True}, static_dir=STATIC, auth_alert=alert,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_valid_token_and_lock_screen_are_not_reported(caplog):
    alerts: list = []
    caplog.set_level(logging.WARNING, logger=auth_watch.__name__)
    async with _client(alerts) as c:
        assert (await c.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 200
        await c.get("/")
        assert (await c.get("/api/version")).status_code == 200
    await _settle()
    assert alerts == [] and "dashboard auth" not in caplog.text


async def test_bad_token_is_logged_and_alerted_without_the_token(caplog):
    alerts: list = []
    caplog.set_level(logging.WARNING, logger=auth_watch.__name__)
    async with _client(alerts) as c:
        r = await c.get("/api/health", headers={
            "Authorization": "Bearer hunter2-typo", "CF-Connecting-IP": "203.0.113.9",
            "User-Agent": "evil\nbot",
        })
        assert r.status_code == 401
    await _settle()
    assert "bad token: GET '/api/health' from ip=203.0.113.9" in caplog.text
    assert "'evil\\nbot'" in caplog.text  # control chars can't forge log lines
    assert alerts and alerts[0][0] == "ARES dashboard: bad token"
    assert "203.0.113.9" in alerts[0][1]
    assert "hunter2" not in caplog.text and "hunter2" not in alerts[0][1]


async def test_wrong_token_on_the_lock_screen_is_still_a_bad_token():
    alerts: list = []
    async with _client(alerts) as c:
        await c.get("/api/version", headers={"Authorization": "Bearer nope"})
    await _settle()
    assert [a[0] for a in alerts] == ["ARES dashboard: bad token"]


async def test_unauthenticated_protected_and_unknown_paths_are_reported(caplog):
    alerts: list = []
    caplog.set_level(logging.WARNING, logger=auth_watch.__name__)
    async with _client(alerts) as c:
        assert (await c.post("/api/chat", json={"text": "hi"})).status_code == 401
        await c.get("/.env", headers={"CF-Connecting-IP": "198.51.100.7"})
    await _settle()
    assert "unauthenticated: POST '/api/chat'" in caplog.text
    assert "unauthenticated: GET '/.env' from ip=198.51.100.7" in caplog.text
    assert len(alerts) == 2  # two different clients


async def test_alerts_are_deduplicated_per_client_but_every_request_is_logged(caplog):
    now = [1000.0]
    sent: list = []

    async def alert(title, message):
        sent.append(message)

    caplog.set_level(logging.WARNING, logger=auth_watch.__name__)
    watch = AuthWatch(TOKEN, alert, clock=lambda: now[0])
    scope = {"type": "http", "method": "GET", "path": "/api/tasks", "client": ("10.16.0.1", 5),
             "headers": [(b"cf-connecting-ip", b"203.0.113.9")]}
    for _ in range(4):
        watch.observe(scope)
    await _settle()
    assert len(sent) == 1
    assert caplog.text.count("unauthenticated: GET") == 4
    now[0] += auth_watch.NOTIFY_COOLDOWN_S
    watch.observe(scope)
    await _settle()
    assert len(sent) == 2 and "+3 more" in sent[1]


async def test_global_alert_cap_and_bounded_memory(monkeypatch):
    monkeypatch.setattr(auth_watch, "MAX_TRACKED", 8)
    sent: list = []

    async def alert(title, message):
        sent.append(message)

    watch = AuthWatch(TOKEN, alert, clock=lambda: 0.0)
    for i in range(50):
        watch.observe({"type": "http", "method": "GET", "path": "/x", "client": (f"192.0.2.{i}", 1),
                       "headers": []})
    await _settle()
    assert len(sent) == auth_watch.MAX_NOTIFY_PER_HOUR
    assert len(watch._seen) <= 8


async def test_a_failing_alert_never_breaks_the_request():
    async def alert(title, message):
        raise RuntimeError("ntfy down")

    watch = AuthWatch(TOKEN, alert)
    watch.observe({"type": "http", "method": "GET", "path": "/x", "client": None, "headers": []})
    await _settle()


async def test_ntfy_notify_sends_title_and_tags(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            seen.update(url=url, content=content, headers=headers)
            return Resp()

    monkeypatch.setattr("ares.plugins.channels.push_ntfy.httpx.AsyncClient", FakeClient)
    ch = NtfyChannel("https://ntfy.test/", "tok", {"primary": "topic"})
    assert await ch.notify("primary", "body", title="ARES dashboard: bad token", tags="warning")
    assert seen["url"] == "https://ntfy.test/topic"
    assert seen["headers"] == {"Authorization": "Bearer tok", "Title": "ARES dashboard: bad token",
                               "Tags": "warning"}
    assert await ch.deliver("primary", "plain", None)
    assert "Title" not in seen["headers"]
