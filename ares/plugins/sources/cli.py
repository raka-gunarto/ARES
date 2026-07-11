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
        - EOF also stops the source (and signals daemon shutdown), so piped
          input that ends without an explicit "!quit" still shuts down cleanly.
        - Other lines emit with NORMAL priority.

        Uses a cancellable `asyncio.StreamReader` connected to stdin (per
        spec §0 rule 4: all I/O is async) instead of a blocking
        `run_in_executor(sys.stdin.readline)` call, so this coroutine can be
        cancelled promptly during shutdown.
        """
        loop = asyncio.get_event_loop()

        try:
            reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception as e:
            log.warning(
                "cli: could not attach async reader to stdin (%s); idling", e
            )
            while not self._stopping:
                await asyncio.sleep(0.2)
            return

        while not self._stopping:
            try:
                line_bytes = await reader.readline()

                # EOF is an empty bytes string
                if not line_bytes:
                    log.debug("cli: EOF reached")
                    self._stopping = True
                    if self.shutdown_event is not None:
                        self.shutdown_event.set()
                    return

                line = line_bytes.decode(errors="replace").rstrip("\n\r")

                # Skip empty or whitespace-only lines
                if not line or not line.strip():
                    continue

                # Check for !quit command
                if line == "!quit":
                    log.debug("cli: quit command received")
                    self._stopping = True
                    if self.shutdown_event is not None:
                        self.shutdown_event.set()
                    return

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

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Log the exception and continue on any other per-line error
                log.error("cli: error reading input: %s", e)
                continue
