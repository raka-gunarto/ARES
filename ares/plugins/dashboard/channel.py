"""Web channel for dashboard message delivery via async queues."""
from __future__ import annotations

import asyncio
import time
import typing

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session


logger = get_logger(__name__)

# A present browser re-issues the <=25s long-poll immediately, so it marks
# itself at least every ~25s. If the last poll is older than this, treat the
# web session as abandoned: deliver() declines so the router can reach the
# user where they actually are (speaker / push) instead of piling messages
# into an outbox no one is draining.
PRESENCE_WINDOW_S = 60.0


class WebChannel(BaseChannel):
    """Message delivery channel for web dashboard via async queues.

    Maintains per-user outbox queues that are polled by the browser's
    long-poll endpoint. This allows the agent's speak messages to reach
    the web dashboard when WEB is the active channel.
    """

    type = ChannelType.WEB

    def __init__(self) -> None:
        """Initialize the web channel with empty outbox queues."""
        self._outboxes: dict[str, asyncio.Queue] = {}
        self._last_poll: dict[str, float] = {}

    def mark_poll(self, user_id: str) -> None:
        """Record that the browser just issued a long-poll for this user.

        Called by the dashboard's poll endpoint on every request. It is the
        only presence signal we have for the web channel: an open dashboard
        polls continuously, a closed one stops.
        """
        self._last_poll[user_id] = time.monotonic()

    def is_present(self, user_id: str) -> bool:
        """True if the browser has long-polled within PRESENCE_WINDOW_S."""
        last = self._last_poll.get(user_id)
        return last is not None and (time.monotonic() - last) <= PRESENCE_WINDOW_S

    def outbox(self, user_id: str) -> asyncio.Queue:
        """Get or create the outbox queue for a user.

        Args:
            user_id: The user ID to get the outbox for.

        Returns:
            The asyncio.Queue for this user's outbox.
        """
        if user_id not in self._outboxes:
            self._outboxes[user_id] = asyncio.Queue()
        return self._outboxes[user_id]

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Deliver a message to a user's web dashboard outbox.

        Pushes the message onto the user's outbox queue without blocking.
        The browser's long-poll endpoint drains this queue to receive messages.

        Args:
            user_id: The user ID to deliver to.
            message: The message text to deliver.
            session: The user's current session.

        Returns:
            True on success, False if no browser is currently long-polling
            (session abandoned) or the queue is full (error logged).
        """
        if not self.is_present(user_id):
            # No live browser: decline so the router falls through to a channel
            # that reaches the user. Don't queue — it would surface as a stale
            # duplicate of what they already got by speaker/push next time they
            # open the dashboard.
            logger.debug("web channel: no active poll for %s; declining", user_id)
            return False
        try:
            self.outbox(user_id).put_nowait(message)
            return True
        except asyncio.QueueFull:
            logger.error(
                f"Outbox queue full for user {user_id}; message dropped: {message[:100]}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to deliver message to web outbox for {user_id}: {e}")
            return False
