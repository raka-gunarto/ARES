"""Web channel for dashboard message delivery via async queues."""
from __future__ import annotations

import asyncio
import typing

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session


logger = get_logger(__name__)


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
            True on success, False if the queue is full (error logged).
        """
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
