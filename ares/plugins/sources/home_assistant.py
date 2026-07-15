"""Home Assistant source and REST service, per spec §7.3.

The live WS transport is implemented: connect to `ws_url` -> authenticate
with the long-lived token -> subscribe_events (`state_changed` and
`ares_event`) -> dispatch incoming events into the existing filter methods
(`process_state_changed` / `process_ares_event`) -> reconnect forever with
exponential backoff (2s -> 60s cap) on any connection error. Requires the
optional `websockets` extra (`pip install -e ".[home_assistant]"`); without
it, `start()` raises RuntimeError.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import tempfile
import time
from pathlib import Path

import httpx

from ares.core.event import EventBus, Priority
from ares.core.session import SessionManager
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

# Guarded import of the optional websockets extra (spec §12).
try:
    import websockets

    _HAVE_WS = True
except ImportError:
    _HAVE_WS = False

_PRIORITY_MAP: dict[str, Priority] = {
    "CRITICAL": Priority.CRITICAL,
    "HIGH": Priority.HIGH,
    "NORMAL": Priority.NORMAL,
    "LOW": Priority.LOW,
}

_SNAPSHOT_TTL = 30.0

# SECURITY (§14, PATCH-3): outbound control is NOT gated by the inbound event
# allow-list. Without this, the LLM could unlock doors / disarm the alarm from a
# poisoned memory note or crafted message. These `domain.service` pairs are
# DEFAULT-DENIED for LLM-initiated `control_device`; the operator can override
# `home_assistant.blocked_controls` in config (empty list = allow all).
DEFAULT_BLOCKED_CONTROLS = (
    "lock.unlock",
    "lock.open",
    "alarm_control_panel.alarm_disarm",
)

# Default attribute keys included in snapshot and tool output.
_DEFAULT_SNAPSHOT_ATTRS = (
    "friendly_name",
    "temperature",
    "humidity",
    "brightness",
    "current_temperature",
    "battery_level",
    "state_class",
)


def _format_entity_line(
    entity_id: str, state: str, attrs: dict, attr_keys: tuple[str, ...]
) -> str:
    """Format one entity line as 'entity_id: state [k=v, ...]' with matching attrs."""
    parts = []
    for k in attr_keys:
        if k in attrs:
            v = attrs[k]
            parts.append(f"{k}={v}")
    suffix = f" [{', '.join(parts)}]" if parts else ""
    return f"{entity_id}: {state}{suffix}"


class HAService:
    """REST client for Home Assistant, placed in services["home_assistant"]."""

    def __init__(
        self,
        rest_url: str,
        token: str,
        allowed_domains: list[str],
        blocked_controls: list[str] | None = None,
        snapshot_attrs: tuple[str, ...] = _DEFAULT_SNAPSHOT_ATTRS,
    ) -> None:
        """Initialize the service.

        Args:
            rest_url: Base URL of the Home Assistant REST API.
            token: Long-lived access token.
            allowed_domains: Domains fetched by snapshot_summary().
            blocked_controls: `domain.service` pairs that LLM-initiated
                control_device must refuse (default: doors/alarm disarm).
            snapshot_attrs: Attribute keys to include in snapshot and tool output.
        """
        self.rest_url = rest_url.rstrip("/")
        self.token = token
        self.allowed_domains = allowed_domains
        self.blocked_controls = frozenset(
            blocked_controls if blocked_controls is not None else DEFAULT_BLOCKED_CONTROLS
        )
        self.snapshot_attrs = snapshot_attrs
        self._client = httpx.AsyncClient(timeout=10.0)
        self._snap_cache: str | None = None
        self._snap_ts: float = 0.0

    def control_allowed(self, domain: str, action: str) -> bool:
        """True unless `domain.action` is on the outbound safety denylist (§14)."""
        return f"{domain}.{action}" not in self.blocked_controls

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def get_state(self, entity_id: str) -> dict:
        """Fetch the current state of a single entity."""
        resp = await self._client.get(
            f"{self.rest_url}/api/states/{entity_id}", headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def get_states(self, domain: str | None) -> list[dict]:
        """Fetch all entity states, optionally filtered to one domain."""
        resp = await self._client.get(
            f"{self.rest_url}/api/states", headers=self._headers()
        )
        resp.raise_for_status()
        states: list[dict] = resp.json()
        if domain:
            prefix = f"{domain}."
            states = [s for s in states if s.get("entity_id", "").startswith(prefix)]
        return states

    async def call_service(
        self, domain: str, service: str, entity_id: str, data: dict
    ) -> dict:
        """Call a Home Assistant service against a single entity."""
        body = {"entity_id": entity_id, **(data or {})}
        resp = await self._client.post(
            f"{self.rest_url}/api/services/{domain}/{service}",
            json=body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def snapshot_summary(self) -> str:
        """Return a cached summary with key attributes, rebuilt at most every 30s."""
        now = time.monotonic()
        if self._snap_cache is not None and (now - self._snap_ts) < _SNAPSHOT_TTL:
            return self._snap_cache

        lines: list[str] = []
        for domain in self.allowed_domains:
            states = await self.get_states(domain)
            for s in states:
                eid = s.get("entity_id", "")
                state = s.get("state", "unknown")
                attrs = s.get("attributes", {})
                lines.append(_format_entity_line(eid, state, attrs, self.snapshot_attrs))
        summary = "\n".join(lines)
        self._snap_cache = summary
        self._snap_ts = now
        return summary

    async def camera_snapshot(self, entity_id: str) -> Path:
        """Fetch a camera still and write it to a temp file, returning its path."""
        resp = await self._client.get(
            f"{self.rest_url}/api/camera_proxy/{entity_id}", headers=self._headers()
        )
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write(resp.content)
            path = Path(f.name)
        return path

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()


class HomeAssistantSource(BaseSource):
    """Filters and emits Home Assistant state_changed / ares_event events."""

    name = "home_assistant"

    def __init__(
        self,
        bus: EventBus,
        config: dict,
        service: HAService,
        sessions: SessionManager,
    ) -> None:
        """Initialize the source.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dict (spec §8 home_assistant block).
            service: HAService used for snapshot summaries.
            sessions: SessionManager updated on person.* changes.
        """
        super().__init__(bus, config)
        self.service = service
        self.sessions = sessions
        self.allowed_domains: list[str] = config.get(
            "allowed_domains", ["binary_sensor", "person", "alarm_control_panel"]
        )
        self.allowed_entities: set[str] = set(config.get("allowed_entities", []))
        self.debounce_seconds: float = float(config.get("debounce_seconds", 5))
        self.priority_rules: list[dict] = config.get("priority_rules", [])
        self.entity_rooms: dict[str, str] = config.get("entity_rooms", {})
        self._last_emit: dict[str, float] = {}
        self._ws_url: str | None = config.get("ws_url")
        self._token: str | None = config.get("token")

    def _resolve_priority(self, entity_id: str) -> Priority:
        """First matching priority_rules glob wins; default NORMAL."""
        for rule in self.priority_rules:
            if fnmatch.fnmatch(entity_id, rule["match"]):
                return _PRIORITY_MAP.get(rule["priority"], Priority.NORMAL)
        return Priority.NORMAL

    async def process_state_changed(self, data: dict) -> bool:
        """Run the filter pipeline for a single HA state_changed event.

        Returns True if an event was emitted, False if dropped (or on error).
        """
        try:
            entity_id = data["entity_id"]
            new_state = data.get("new_state")
            if new_state is None:
                return False
            domain = entity_id.split(".", 1)[0]

            # allow-list
            if domain not in self.allowed_domains and entity_id not in self.allowed_entities:
                return False

            # same-state drop
            old_state = data.get("old_state")
            if old_state and old_state.get("state") == new_state.get("state"):
                return False

            # debounce
            now = time.monotonic()
            last = self._last_emit.get(entity_id)
            if last is not None and (now - last) < self.debounce_seconds:
                return False

            priority = self._resolve_priority(entity_id)

            # person -> session
            if domain == "person":
                away = new_state.get("state") != "home"
                room = None if away else self.entity_rooms.get(entity_id)
                self.sessions.touch("primary", None, room)

            friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)
            snapshot = await self.service.snapshot_summary()
            room = self.entity_rooms.get(entity_id)
            await self.emit(
                type="state_change",
                payload={
                    "entity_id": entity_id,
                    "old": old_state,
                    "new": new_state,
                    "friendly_name": friendly,
                    "snapshot": snapshot,
                },
                priority=priority,
                room=room,
            )
            self._last_emit[entity_id] = now
            return True
        except Exception:
            log.exception("home_assistant: failed to process state_changed event")
            return False

    async def process_ares_event(self, data: dict) -> None:
        """Pass through a custom HA ares_event verbatim with its declared priority."""
        priority = _PRIORITY_MAP.get(data.get("priority", "NORMAL"), Priority.NORMAL)
        await self.emit(
            type="ares_event",
            payload=data,
            priority=priority,
            room=data.get("location"),
        )

    async def _authenticate(self, ws) -> bool:
        """Perform the HA WS auth handshake.

        Args:
            ws: The open websocket connection.

        Returns:
            True on `auth_ok`, False on `auth_invalid` (logged, not raised).
        """
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") != "auth_required":
            log.error(
                "home_assistant: expected auth_required, got %r", msg.get("type")
            )

        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))

        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("type") == "auth_ok":
            return True
        if msg.get("type") == "auth_invalid":
            log.error(
                "home_assistant: authentication failed: %s", msg.get("message")
            )
            return False
        log.error("home_assistant: unexpected auth reply type %r", msg.get("type"))
        return False

    async def _subscribe(self, ws) -> None:
        """Subscribe to state_changed and ares_event over the WS connection."""
        await ws.send(
            json.dumps(
                {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            )
        )
        await ws.send(
            json.dumps(
                {"id": 2, "type": "subscribe_events", "event_type": "ares_event"}
            )
        )

    async def _receive_loop(self, ws) -> None:
        """Read events from the WS connection and dispatch into the filter pipeline.

        Loops until `_stopping` is set or `ws.recv()` raises (e.g. connection
        closed), in which case the exception propagates to `start()` for
        reconnect handling.
        """
        while not self._stopping:
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("type") != "event":
                continue
            event = msg.get("event", {})
            event_type = event.get("event_type")
            data = event.get("data", {})
            if event_type == "state_changed":
                await self.process_state_changed(data)
            elif event_type == "ares_event":
                await self.process_ares_event(data)
            # else: ignore (e.g. "result", "pong")

    async def start(self) -> None:
        """Connect to Home Assistant's WS API and dispatch events, per spec §7.3.

        Reconnects forever with exponential backoff (2s -> 60s cap) on any
        connection error, until `stop()` is called.

        Raises:
            RuntimeError: If the `websockets` extra is not installed.
        """
        if not _HAVE_WS:
            raise RuntimeError(
                "home_assistant: the 'websockets' extra is required for the "
                "live transport; pip install -e '.[home_assistant]'"
            )

        delay = 2
        while not self._stopping:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    if await self._authenticate(ws):
                        delay = 2  # reset ONLY after a successful auth
                        await self._subscribe(ws)
                        await self._receive_loop(ws)
                    else:
                        # Bad token/creds: do NOT hammer HA every 2s — back off.
                        log.error(
                            "home_assistant: authentication rejected; backing off %ss "
                            "(check HA_TOKEN)", delay
                        )
            except websockets.exceptions.ConnectionClosed:
                # A dropped HA connection is routine; reconnect quietly (no traceback).
                log.info("home_assistant: WS connection closed; reconnecting")
            except Exception:
                log.exception("home_assistant: WS connection error")

            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
