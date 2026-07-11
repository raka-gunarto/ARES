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


class FireHandler(BaseCriticalHandler):
    """Handler for fire/smoke detection events."""

    def __init__(
        self, fire_entities: list[str], tasks: TaskStore, services: dict
    ) -> None:
        """
        Initialize the fire handler.

        Args:
            fire_entities: List of entity glob patterns to match (e.g. "binary_sensor.smoke_*").
            tasks: Task store for creating monitoring tasks.
            services: Dict of available services (voice, sip, etc.).
        """
        self.globs = fire_entities
        self.tasks = tasks
        self.services = services

    def matches(self, event: Event) -> bool:
        """
        Check if this event is a fire/smoke detection.

        Matches state_change events where the entity matches the configured
        globs and the new state is 'on' or 'triggered'.

        Args:
            event: The event to check.

        Returns:
            True if this is a fire/smoke event, False otherwise.
        """
        if event.type != "state_change":
            return False

        entity_id = event.payload.get("entity_id")
        if not entity_id:
            return False

        # Check if entity matches any of the configured globs
        if not any(fnmatch.fnmatch(entity_id, glob) for glob in self.globs):
            return False

        # Check if new state is on/triggered
        new_state = event.payload.get("new", {}).get("state")
        return new_state in ("on", "triggered")

    async def handle(self, event: Event, router: ResponseRouter) -> None:
        """
        Handle a fire/smoke detection event deterministically.

        Actions taken:
        - Push notification to user
        - Voice broadcast to all rooms (if voice service available)
        - SIP call attempt (if sip service available)
        - Create monitoring task

        Args:
            event: The fire/smoke detection event.
            router: The response router for sending notifications.
        """
        entity_id = event.payload.get("entity_id")
        phrase = "Attention. Smoke or fire has been detected in the house."

        log.warning(
            "FireHandler: fire/smoke detected on %s (CRITICAL, no LLM)", entity_id
        )

        # Push notification
        try:
            await router.notify(event.user_id, phrase)
        except Exception as e:
            log.exception("FireHandler: notify failed: %s", e)

        # Voice broadcast to all rooms
        voice = self.services.get("voice")
        if voice and hasattr(voice, "broadcast"):
            try:
                await voice.broadcast(phrase)
            except Exception as e:
                log.exception("FireHandler: voice broadcast failed: %s", e)

        # SIP call attempt
        sip = self.services.get("sip")
        if sip and hasattr(sip, "place_call"):
            try:
                await sip.place_call(phrase)
            except Exception as e:
                log.exception("FireHandler: SIP call failed: %s", e)

        # Create monitoring task
        try:
            await self.tasks.create(
                event.user_id,
                "monitoring",
                title=f"Fire/smoke detected: {entity_id}",
                detail=phrase,
            )
        except Exception as e:
            log.exception("FireHandler: task creation failed: %s", e)


class IntruderHandler(BaseCriticalHandler):
    """Handler for alarm/intrusion detection events."""

    def __init__(
        self, alarm_entities: list[str], tasks: TaskStore, services: dict
    ) -> None:
        """
        Initialize the intrusion handler.

        Args:
            alarm_entities: List of entity glob patterns to match (e.g. "alarm_control_panel.*").
            tasks: Task store for creating monitoring tasks.
            services: Dict of available services (voice, sip, etc.).
        """
        self.globs = alarm_entities
        self.tasks = tasks
        self.services = services

    def matches(self, event: Event) -> bool:
        """
        Check if this event is an alarm/intrusion detection.

        Matches state_change events where the entity matches the configured
        globs and the new state is 'on' or 'triggered'.

        Args:
            event: The event to check.

        Returns:
            True if this is an alarm event, False otherwise.
        """
        if event.type != "state_change":
            return False

        entity_id = event.payload.get("entity_id")
        if not entity_id:
            return False

        # Check if entity matches any of the configured globs
        if not any(fnmatch.fnmatch(entity_id, glob) for glob in self.globs):
            return False

        # Check if new state is on/triggered
        new_state = event.payload.get("new", {}).get("state")
        return new_state in ("on", "triggered")

    async def handle(self, event: Event, router: ResponseRouter) -> None:
        """
        Handle an alarm/intrusion detection event deterministically.

        Actions taken:
        - Push notification to user
        - Voice broadcast to all rooms (if voice service available)
        - SIP call attempt (if sip service available)
        - Create monitoring task

        Args:
            event: The alarm detection event.
            router: The response router for sending notifications.
        """
        entity_id = event.payload.get("entity_id")
        phrase = "Attention. The alarm has been triggered."

        log.warning(
            "IntruderHandler: alarm triggered on %s (CRITICAL, no LLM)", entity_id
        )

        # Push notification
        try:
            await router.notify(event.user_id, phrase)
        except Exception as e:
            log.exception("IntruderHandler: notify failed: %s", e)

        # Voice broadcast to all rooms
        voice = self.services.get("voice")
        if voice and hasattr(voice, "broadcast"):
            try:
                await voice.broadcast(phrase)
            except Exception as e:
                log.exception("IntruderHandler: voice broadcast failed: %s", e)

        # SIP call attempt
        sip = self.services.get("sip")
        if sip and hasattr(sip, "place_call"):
            try:
                await sip.place_call(phrase)
            except Exception as e:
                log.exception("IntruderHandler: SIP call failed: %s", e)

        # Create monitoring task
        try:
            await self.tasks.create(
                event.user_id,
                "monitoring",
                title=f"Alarm triggered: {entity_id}",
                detail=phrase,
            )
        except Exception as e:
            log.exception("IntruderHandler: task creation failed: %s", e)
