"""Event dispatcher: routes events to the critical path or per-user agent
worker queues, per spec §4.3.
"""
from __future__ import annotations

import asyncio
import collections
import typing

from ares.core.agent import USER_INITIATED_TYPES
from ares.core.channel import ChannelType
from ares.core.critical import CriticalHandlerRegistry
from ares.core.event import Event, EventBus, Priority
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.agent import Agent

log = get_logger(__name__)

# (source, type) -> channel for exact matches; source alone also checked below.
_CHANNEL_MAP: dict[tuple[str, str], ChannelType] = {
    ("voice", "speech"): ChannelType.VOICE,
    ("sip", "sip_message"): ChannelType.SIP_MESSAGE,
    ("sip", "call_speech"): ChannelType.SIP_CALL,
    ("dashboard", "web_message"): ChannelType.WEB,
}

# Wall-clock cap on one agent turn (§4.3). The worker is a FIFO: a turn wedged on
# a hung await would silently swallow every later message from the household.
# Generous, because tools (a call, a browser flow) legitimately take minutes.
DEFAULT_TURN_TIMEOUT_S = 900.0

# Sources whose events never change the active channel (room-only updates).
_ROOM_ONLY_SOURCES = {"home_assistant", "scheduler", "privileges", "subagents"}


def _channel_for(event: Event) -> ChannelType | None:
    """
    Determine the channel to set for an inbound event, per spec §4.6.

    Returns None when the event should not change the active channel (either
    because it is a room-only source or because it is otherwise unmapped).
    """
    if event.source == "cli":
        return ChannelType.CONSOLE
    if event.source in _ROOM_ONLY_SOURCES:
        return None
    return _CHANNEL_MAP.get((event.source, event.type))


class Dispatcher:
    """
    Consumes events from the EventBus and routes them to the appropriate
    handler: critical events go straight to the CriticalHandlerRegistry;
    everything else is serialized per user_id through a dedicated worker
    task backed by a FIFO deque, honoring LOW-drop and HIGH-jump policies.
    """

    def __init__(
        self,
        bus: EventBus,
        agent: Agent,
        critical: CriticalHandlerRegistry,
        turn_timeout_s: float = DEFAULT_TURN_TIMEOUT_S,
    ) -> None:
        """
        Initialize the dispatcher.

        Args:
            bus: The event bus to consume events from.
            agent: The agent used to handle non-critical events (must expose
                `.sessions` and an async `.handle(event)`).
            critical: The registry used to handle CRITICAL-priority events.
            turn_timeout_s: Wall-clock cap on one `agent.handle` call.
        """
        self.bus = bus
        self.turn_timeout_s = turn_timeout_s
        self.agent = agent
        self.critical = critical
        self._queues: dict[str, collections.deque] = {}
        self._busy: dict[str, bool] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._workers: dict[str, asyncio.Task] = {}

    def _ensure_worker(self, user_id: str) -> None:
        """Lazily create the queue/wakeup/task for a user_id if not present."""
        if user_id in self._workers:
            return
        self._queues[user_id] = collections.deque()
        self._busy[user_id] = False
        self._wakeups[user_id] = asyncio.Event()
        self._workers[user_id] = asyncio.create_task(self._worker(user_id))

    async def _worker(self, user_id: str) -> None:
        """
        Drain a single user's FIFO queue one event at a time, calling
        `agent.handle` for each, never letting one bad event kill the loop.
        """
        wakeup = self._wakeups[user_id]
        queue = self._queues[user_id]
        while True:
            await wakeup.wait()
            wakeup.clear()
            while queue:
                event = queue.popleft()
                self._busy[user_id] = True
                try:
                    channel = _channel_for(event)
                    self.agent.sessions.touch(event.user_id, channel, event.room)
                    await asyncio.wait_for(
                        self.agent.handle(event), timeout=self.turn_timeout_s
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "agent turn for event %s (user=%s) exceeded %ss; abandoned",
                        event.id,
                        user_id,
                        self.turn_timeout_s,
                    )
                    await self._say_timed_out(event)
                except Exception:
                    log.exception(
                        "agent.handle raised for event %s (user=%s)",
                        event.id,
                        user_id,
                    )
                finally:
                    self._busy[user_id] = False

    async def _say_timed_out(self, event: Event) -> None:
        """Tell the person their request was dropped, if they asked for it."""
        router = getattr(self.agent, "router", None)
        if router is None or event.type not in USER_INITIATED_TYPES:
            return
        try:
            await router.speak(event.user_id, "Sorry, that took too long and I had to stop.")
        except Exception:
            log.exception("could not report a timed-out turn")

    def _enqueue(self, event: Event) -> None:
        """Apply LOW/HIGH/NORMAL enqueue policy for a non-critical event."""
        user_id = event.user_id
        self._ensure_worker(user_id)
        queue = self._queues[user_id]

        if event.priority == Priority.LOW:
            if len(queue) > 0 or self._busy.get(user_id, False):
                log.info(
                    "dropping LOW event %s for user %s: queue busy or non-empty",
                    event.id,
                    user_id,
                )
                return
            queue.append(event)
        elif event.priority == Priority.HIGH:
            queue.appendleft(event)
        else:
            queue.append(event)

        self._wakeups[user_id].set()

    async def run(self) -> None:
        """
        Main dispatch loop: forever pull events from the bus and route them
        either to the critical handler (inline) or to the per-user queue.

        On cancellation, cancels all spawned per-user worker tasks before
        re-raising, so callers can shut the dispatcher down cleanly.
        """
        try:
            while True:
                event = await self.bus.get()
                try:
                    if event.priority == Priority.CRITICAL:
                        await self.critical.handle(event)
                    else:
                        self._enqueue(event)
                except Exception:
                    log.exception("dispatcher failed to process event %s", event.id)
        except asyncio.CancelledError:
            for task in self._workers.values():
                task.cancel()
            raise
