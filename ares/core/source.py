from __future__ import annotations

import abc

from ares.core.event import Event, EventBus, Priority
from ares.core.utils.ids import new_id


class BaseSource(abc.ABC):
    """Abstract base class for event sources.

    Each source publishes events to the EventBus. Subclasses must implement
    the start() method to define the event generation logic.
    """

    name: str  # class attribute, unique per subclass

    def __init__(self, bus: EventBus, config: dict) -> None:
        """Initialize a source with an event bus and configuration.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary for this source.
        """
        self.bus = bus
        self.config = config
        self._stopping = False

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the source.

        This is a long-running method that returns only when stop() is called.
        Subclasses must implement this method to define event generation logic.
        """
        pass

    async def stop(self) -> None:
        """Stop the source.

        Sets the _stopping flag which should be checked by start() to exit
        gracefully.
        """
        self._stopping = True

    async def emit(
        self,
        type: str,
        payload: dict,
        priority: Priority,
        user_id: str = "primary",
        room: str | None = None,
    ) -> None:
        """Emit an event to the bus.

        Builds an Event with id, timestamp, and source=self.name, then
        publishes it to the bus.

        Args:
            type: The event type.
            payload: The event payload dictionary.
            priority: The event priority level.
            user_id: The user ID (default: "primary").
            room: Optional room identifier.
        """
        event = Event(
            id=new_id(),
            source=self.name,
            type=type,
            payload=payload,
            priority=priority,
            user_id=user_id,
            room=room,
        )
        await self.bus.publish(event)
