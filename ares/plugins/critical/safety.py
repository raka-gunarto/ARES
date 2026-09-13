"""Deterministic fire and intrusion handlers (spec §7.7).

These bypass the LLM entirely: a smoke alarm must reach the household even if
the model is slow, down, or has been talked out of it. Every action is a direct
channel/service call, and each one is attempted independently — a failing
speaker must never stop the phone call.

Collaborators arrive through the `services` dict (plugins never import each
other), all optional:

* ``services["announcers"]`` — async callables ``(phrase) -> bool`` that say the
  phrase out loud in the house (every voice room, the HA speaker);
* ``services["presence"]`` — async ``() -> bool``, whether someone is home;
* ``services["sip"]`` — the SIP service, for calling the user when away.
"""
from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from ares.core.critical import BaseCriticalHandler
from ares.core.event import Event
from ares.core.router import ResponseRouter
from ares.core.utils.logging import get_logger

if TYPE_CHECKING:
    from ares.core.tasks.store import TaskStore

log = get_logger(__name__)


class SafetyHandler(BaseCriticalHandler):
    """Match alarm entities by glob; alert on every available path."""

    phrase = ""
    title = ""

    def __init__(self, entities: list[str], tasks: TaskStore, services: dict) -> None:
        """Store the entity globs, the task store and the injected services."""
        self.globs = list(entities)
        self.tasks = tasks
        self.services = services

    def matches(self, event: Event) -> bool:
        """A `state_change` of a matching entity to `on`/`triggered`."""
        if event.type != "state_change":
            return False
        entity_id = event.payload.get("entity_id")
        if not entity_id or not any(fnmatch.fnmatch(entity_id, g) for g in self.globs):
            return False
        return (event.payload.get("new") or {}).get("state") in ("on", "triggered")

    async def handle(self, event: Event, router: ResponseRouter) -> None:
        """Push, announce in the house, call if away, open a monitoring task."""
        name = type(self).__name__
        entity_id = event.payload.get("entity_id")
        log.warning("%s: %s triggered for %s", name, entity_id, event.user_id)

        try:
            await router.notify(event.user_id, self.phrase)
        except Exception:
            log.exception("%s: notify failed", name)

        for announce in self.services.get("announcers") or []:
            try:
                await announce(self.phrase)
            except Exception:
                log.exception("%s: announcement failed", name)

        await self._call_if_away(event.user_id, name)

        try:
            await self.tasks.create(
                event.user_id, "monitoring",
                title=f"{self.title}: {entity_id}", detail=self.phrase,
            )
        except Exception:
            log.exception("%s: task creation failed", name)

    async def _call_if_away(self, user_id: str, name: str) -> None:
        sip = self.services.get("sip")
        uri = getattr(sip, "user_uris", {}).get(user_id) if sip is not None else None
        if not uri:
            return
        presence = self.services.get("presence")
        if presence is not None:
            try:
                if await presence():
                    return  # someone is home and has heard the announcement
            except Exception:
                log.exception("%s: presence check failed; calling anyway", name)
        try:
            await sip.call_and_speak(uri, self.phrase, False)
        except Exception:
            log.exception("%s: SIP call failed", name)


class FireHandler(SafetyHandler):
    """Smoke or fire detected."""

    phrase = "Attention. Smoke or fire has been detected in the house."
    title = "Fire/smoke detected"


class IntruderHandler(SafetyHandler):
    """Alarm panel triggered."""

    phrase = "Attention. The alarm has been triggered."
    title = "Alarm triggered"
