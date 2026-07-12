"""Tests for the live Home Assistant WebSocket transport, per spec §7.3."""
from __future__ import annotations

import json

import websockets

from ares.core.event import EventBus
from ares.core.session import SessionManager
from ares.plugins.sources.home_assistant import HomeAssistantSource


class FakeHAService:
    """Minimal mock HAService with snapshot_summary (used by process_state_changed)."""

    async def snapshot_summary(self) -> str:
        """Return a canned snapshot string."""
        return "snap"


class FakeWS:
    """Fake websocket connection: recv() pops scripted JSON strings; send() records."""

    def __init__(self, script: list[str]) -> None:
        """Initialize with a list of raw messages to yield from recv(), in order."""
        self._script = list(script)
        self.sent: list[str] = []

    async def recv(self) -> str:
        """Pop and return the next scripted message, or raise ConnectionClosed."""
        if not self._script:
            raise websockets.exceptions.ConnectionClosed(None, None)
        return self._script.pop(0)

    async def send(self, data: str) -> None:
        """Record a sent message."""
        self.sent.append(data)


def make_source(config: dict | None = None) -> HomeAssistantSource:
    """Build a HomeAssistantSource with real EventBus/SessionManager and a fake HAService."""
    if config is None:
        config = {
            "ws_url": "ws://ha.local:8123/api/websocket",
            "token": "test-token",
            "allowed_domains": ["binary_sensor", "person", "alarm_control_panel"],
            "debounce_seconds": 5,
            "priority_rules": [],
        }
    bus = EventBus()
    sessions = SessionManager()
    service = FakeHAService()
    return HomeAssistantSource(bus, config, service, sessions)


async def test_authenticate_ok() -> None:
    """auth_required -> auth -> auth_ok returns True and sends the configured token."""
    src = make_source()
    fake = FakeWS(
        [
            json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}),
            json.dumps({"type": "auth_ok", "ha_version": "2024.1.0"}),
        ]
    )

    result = await src._authenticate(fake)

    assert result is True
    sent_auth = [json.loads(m) for m in fake.sent if json.loads(m).get("type") == "auth"]
    assert len(sent_auth) == 1
    assert sent_auth[0]["access_token"] == "test-token"


async def test_authenticate_invalid() -> None:
    """auth_required -> auth -> auth_invalid returns False without raising."""
    src = make_source()
    fake = FakeWS(
        [
            json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}),
            json.dumps({"type": "auth_invalid", "message": "Invalid password"}),
        ]
    )

    result = await src._authenticate(fake)

    assert result is False


async def test_receive_loop_dispatches_state_changed() -> None:
    """A state_changed event message is dispatched to process_state_changed."""
    src = make_source()
    recorded: list[dict] = []

    async def fake_process_state_changed(data: dict) -> bool:
        recorded.append(data)
        src._stopping = True
        return True

    src.process_state_changed = fake_process_state_changed

    event_msg = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": "binary_sensor.x",
                    "old_state": {"state": "off"},
                    "new_state": {"state": "on"},
                },
            },
        }
    )
    fake = FakeWS([event_msg])

    await src._receive_loop(fake)

    assert len(recorded) == 1
    assert recorded[0]["entity_id"] == "binary_sensor.x"


async def test_receive_loop_dispatches_ares_event() -> None:
    """An ares_event event message is dispatched to process_ares_event."""
    src = make_source()
    recorded: list[dict] = []

    async def fake_process_ares_event(data: dict) -> None:
        recorded.append(data)
        src._stopping = True

    src.process_ares_event = fake_process_ares_event

    event_msg = json.dumps(
        {
            "type": "event",
            "event": {
                "event_type": "ares_event",
                "data": {
                    "event": "face_recognised",
                    "who": "John",
                    "location": "front_door",
                    "priority": "NORMAL",
                },
            },
        }
    )
    fake = FakeWS([event_msg])

    await src._receive_loop(fake)

    assert len(recorded) == 1
    assert recorded[0]["who"] == "John"


async def test_subscribe_sends_both_event_types() -> None:
    """_subscribe sends subscribe_events messages for both state_changed and ares_event."""
    src = make_source()
    fake = FakeWS([])

    await src._subscribe(fake)

    sent = [json.loads(m) for m in fake.sent]
    assert len(sent) == 2
    event_types = {m["event_type"] for m in sent}
    assert event_types == {"state_changed", "ares_event"}
    assert all(m["type"] == "subscribe_events" for m in sent)
