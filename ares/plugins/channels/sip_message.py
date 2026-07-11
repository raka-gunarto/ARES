"""SIP MESSAGE channel for text delivery."""
from __future__ import annotations

import logging
import typing

from ares.core.channel import BaseChannel, ChannelType

if typing.TYPE_CHECKING:
    from ares.core.session import Session

logger = logging.getLogger(__name__)


class SIPMessageChannel(BaseChannel):
    """Message delivery channel via SIP MESSAGE protocol."""

    type = ChannelType.SIP_MESSAGE

    def __init__(self, service) -> None:
        """
        Initialize SIP MESSAGE channel.

        Args:
            service: SIPService instance for sending messages.
        """
        self.service = service

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Deliver a message via SIP MESSAGE.

        Args:
            user_id: The user ID to look up in service.user_uris.
            message: The message text to send.
            session: The user's current session (unused).

        Returns:
            False if no URI found for user_id or delivery fails, True on success.
        """
        try:
            uri = self.service.user_uris.get(user_id)
            if not uri:
                logger.warning(f"No SIP URI configured for user {user_id}")
                return False

            return await self.service.send_message(uri, message)
        except Exception as e:
            logger.error(f"Failed to deliver SIP MESSAGE to {user_id}: {e}")
            return False
