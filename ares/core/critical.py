"""Critical event handler registry and base classes."""
from __future__ import annotations

import abc

from ares.core.event import Event
from ares.core.router import ResponseRouter
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class BaseCriticalHandler(abc.ABC):
    """Abstract base class for critical event handlers."""

    @abc.abstractmethod
    def matches(self, event: Event) -> bool:
        """
        Check if this handler should handle the event.

        Args:
            event: The event to check.

        Returns:
            True if this handler should handle the event, False otherwise.
        """

    @abc.abstractmethod
    async def handle(self, event: Event, router: ResponseRouter) -> None:
        """
        Handle the critical event.

        Args:
            event: The event to handle.
            router: The response router for sending responses.
        """


class CriticalHandlerRegistry:
    """Registry for critical event handlers with first-match-wins dispatch."""

    def __init__(self, router: ResponseRouter) -> None:
        """
        Initialize the critical handler registry.

        Args:
            router: The response router for passing to handlers.
        """
        self.router = router
        self._handlers: list[BaseCriticalHandler] = []

    def register(self, handler: BaseCriticalHandler) -> None:
        """
        Register a critical event handler.

        Args:
            handler: The handler to register.
        """
        self._handlers.append(handler)

    async def handle(self, event: Event) -> None:
        """
        Handle a critical event using the first matching handler.

        Iterates through registered handlers in order. The first handler
        whose matches() returns True will handle the event. If a handler
        raises an exception, it is logged but does not propagate.
        If no handler matches, an ERROR is logged.

        Args:
            event: The event to handle.
        """
        for handler in self._handlers:
            if handler.matches(event):
                try:
                    await handler.handle(event, self.router)
                except Exception as e:
                    log.exception(
                        "Critical handler %s raised exception for event %s: %s",
                        handler.__class__.__name__,
                        event.id,
                        e,
                    )
                return

        log.error("no critical handler matched event %s", event.id)
