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

# Presence for the web channel means "a long-poll is actually connected right
# now," not "polled recently." A backgrounded/closed browser (common on mobile,
# where the tab is frozen while you're out) can look recently-active for many
# seconds yet be unable to receive anything — so a 'recent poll' window let the
# router commit a reply to an outbox no live connection would drain, and the
# message was lost instead of falling through to push. So presence is primarily
# the count of in-flight polls; a short grace bridges the sub-second gap between
# one long-poll returning and the browser re-issuing the next.
PRESENCE_GRACE_S = 10.0


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
        self._waiters: dict[str, int] = {}
        self._last_poll: dict[str, float] = {}

    def poll_started(self, user_id: str) -> None:
        """Record that a long-poll connection just opened for this user."""
        self._waiters[user_id] = self._waiters.get(user_id, 0) + 1

    def poll_finished(self, user_id: str) -> None:
        """Record that a long-poll connection just closed for this user.

        The close time starts the grace window that covers the brief gap until
        the browser re-issues its next poll.
        """
        self._waiters[user_id] = max(0, self._waiters.get(user_id, 0) - 1)
        self._last_poll[user_id] = time.monotonic()

    def is_present(self, user_id: str) -> bool:
        """True if a long-poll is connected now, or one closed within the grace.

        Waiter count is the real signal: a live poll receives a delivered
        message immediately. The grace only bridges the sub-second gap between
        consecutive polls of a continuously-polling browser.
        """
        if self._waiters.get(user_id, 0) > 0:
            return True
        last = self._last_poll.get(user_id)
        return last is not None and (time.monotonic() - last) <= PRESENCE_GRACE_S

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
