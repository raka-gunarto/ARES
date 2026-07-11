"""Home Assistant source and REST service, per spec §7.3.

BLOCKER (recorded in PROGRESS.md): §12 provides no WebSocket client (httpx
has no WS support) and §0 forbids adding dependencies. The live WS transport
(`ws_url`, subscribe_events, reconnect/backoff) is therefore BLOCKED. HAService
implements the full REST surface and HomeAssistantSource implements the full
filter/emit pipeline as directly-callable methods; `start()` logs the blocker
and idles rather than opening a WebSocket.
"""
from __future__ import annotations

import asyncio
import fnmatch
import tempfile
import time
from pathlib import Path

import httpx

from ares.core.event import EventBus, Priority
from ares.core.session import SessionManager
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

_PRIORITY_MAP: dict[str, Priority] = {
    "CRITICAL": Priority.CRITICAL,
    "HIGH": Priority.HIGH,
    "NORMAL": Priority.NORMAL,
    "LOW": Priority.LOW,
}

_SNAPSHOT_TTL = 30.0


class HAService:
    """REST client for Home Assistant, placed in services["home_assistant"]."""

    def __init__(self, rest_url: str, token: str, allowed_domains: list[str]) -> None:
        """Initialize the service.

        Args:
            rest_url: Base URL of the Home Assistant REST API.
            token: Long-lived access token.
            allowed_domains: Domains fetched by snapshot_summary().
        """
        self.rest_url = rest_url.rstrip("/")
        self.token = token
        self.allowed_domains = allowed_domains
        self._client = httpx.AsyncClient(timeout=10.0)
        self._snap_cache: str | None = None
        self._snap_ts: float = 0.0

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
        """Return a cached "entity: state" summary, rebuilt at most every 30s."""
        now = time.monotonic()
        if self._snap_cache is not None and (now - self._snap_ts) < _SNAPSHOT_TTL:
            return self._snap_cache

        lines: list[str] = []
        for domain in self.allowed_domains:
            states = await self.get_states(domain)
            for s in states:
                lines.append(f"{s.get('entity_id')}: {s.get('state')}")
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

    async def start(self) -> None:
        """Idle: the live WS transport is blocked (see module docstring)."""
        log.warning(
            "home_assistant: live WebSocket transport unavailable (no WS "
            "dependency in spec §12); source idle — events must be injected "
            "via process_state_changed. See PROGRESS.md Blockers."
        )
        while not self._stopping:
            await asyncio.sleep(1)
