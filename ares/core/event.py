from __future__ import annotations

import asyncio
import dataclasses
import enum
import itertools
from datetime import datetime, timezone

from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class Priority(enum.IntEnum):
    """Event priority levels, lower value = higher priority."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclasses.dataclass(frozen=True, slots=True)
class Event:
    """An immutable event published to the EventBus."""

    id: str
    source: str
    type: str
    payload: dict
    priority: Priority
    user_id: str = "primary"
    room: str | None = None
    timestamp: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class EventBus:
    """Async event bus with priority and FIFO ordering within priority."""

    def __init__(self) -> None:
        """Initialize an empty event bus."""
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._counter = itertools.count()

    async def publish(self, event: Event) -> None:
        """
        Publish an event to the bus.

        Events are queued by priority (lower value first), with FIFO ordering
        within the same priority level. A strictly increasing counter ensures
        events with identical priority do not require Event comparison.
        """
        count = next(self._counter)
        await self._queue.put((event.priority, count, event))
        log.debug(
            "event published: id=%s source=%s type=%s priority=%s",
            event.id,
            event.source,
            event.type,
            event.priority.name,
        )

    async def get(self) -> Event:
        """
        Retrieve the next event from the bus.

        Returns the highest-priority event (lowest priority value). Within
        the same priority, returns events in FIFO order.
        """
        _, _, event = await self._queue.get()
        return event

    def qsize(self) -> int:
        """Return the number of events currently queued."""
        return self._queue.qsize()
