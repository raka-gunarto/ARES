from __future__ import annotations

import asyncio

from ares.core.event import Priority
from ares.core.source import BaseSource
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class PrivilegeSource(BaseSource):
    """Polls PrivStore for completed privilege requests and emits updates."""

    name = "privileges"

    def __init__(self, bus, config: dict, store) -> None:
        """Initialize the privilege source.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary.
            store: The PrivStore instance.
        """
        super().__init__(bus, config)
        self.store = store
        self._seen: set[str] = set()
        self.poll_seconds = int(config.get("poll_seconds", 15))

    async def start(self) -> None:
        """Start polling for completed privilege requests."""
        while not self._stopping:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Error polling privilege store: %s", e)

            try:
                await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise

    async def _poll_once(self) -> None:
        """Poll the store once and emit updates for completed requests."""
        reqs = await self.store.list()

        for r in reqs:
            if r.status in {"done", "failed", "denied"} and r.id not in self._seen:
                await self.emit(
                    type="privilege_update",
                    payload={
                        "id": r.id,
                        "kind": r.kind,
                        "status": r.status,
                        "command": r.command,
                        "exit_code": r.exit_code,
                        "output": r.output,
                    },
                    priority=Priority.NORMAL,
                    user_id=r.user_id,
                )
                self._seen.add(r.id)
