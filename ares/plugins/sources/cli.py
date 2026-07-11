from __future__ import annotations

import asyncio
import sys

from ares.core.event import Priority
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class CLISource(BaseSource):
    """Event source that reads lines from stdin."""

    name = "cli"

    def __init__(self, bus, config: dict) -> None:
        """Initialize CLISource.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary for this source.
        """
        super().__init__(bus, config)
        self.shutdown_event: asyncio.Event | None = None

    async def start(self) -> None:
        """Read lines from stdin and emit events.

        Each non-empty line becomes an event.
        - Lines starting with "!high " emit with HIGH priority.
        - The command "!quit" stops the source.
        - Other lines emit with NORMAL priority.
        """
        loop = asyncio.get_event_loop()

        while not self._stopping:
            try:
                # Read a line without blocking the event loop
                line = await loop.run_in_executor(None, sys.stdin.readline)

                # EOF is an empty string
                if not line:
                    log.debug("cli: EOF reached")
                    break

                # Strip trailing newline
                line = line.rstrip("\n\r")

                # Skip empty or whitespace-only lines
                if not line or not line.strip():
                    continue

                # Check for !quit command
                if line == "!quit":
                    log.debug("cli: quit command received")
                    self._stopping = True
                    if self.shutdown_event is not None:
                        self.shutdown_event.set()
                    break

                # Check for !high priority prefix
                if line.startswith("!high "):
                    text = line[6:]  # Remove "!high " prefix
                    await self.emit(
                        type="cli_input",
                        payload={"text": text},
                        priority=Priority.HIGH,
                    )
                else:
                    # Regular line with NORMAL priority
                    await self.emit(
                        type="cli_input",
                        payload={"text": line},
                        priority=Priority.NORMAL,
                    )

            except Exception as e:
                # Log the exception and continue, unless it's EOF
                log.error("cli: error reading input: %s", e)
                continue
