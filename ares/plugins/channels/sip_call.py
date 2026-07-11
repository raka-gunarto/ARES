"""SIP CALL channel for speaking into active calls."""
from __future__ import annotations

import logging
import typing

from ares.core.channel import BaseChannel, ChannelType

if typing.TYPE_CHECKING:
    from ares.core.session import Session

logger = logging.getLogger(__name__)


class SIPCallChannel(BaseChannel):
    """Message delivery channel via active SIP call."""

    type = ChannelType.SIP_CALL

    def __init__(self, service) -> None:
        """
        Initialize SIP CALL channel.

        Args:
            service: SIPService instance with active call management.
        """
        self.service = service

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Deliver a message into the active call.

        Args:
            user_id: The user ID (unused; delivery goes into active call only).
            message: The message text to speak.
            session: The user's current session (unused).

        Returns:
            False if no active call or delivery fails, True on success.

        Note: If no active call, returns False and ResponseRouter falls back
        to the next channel (typically PUSH, per spec §7.5).
        """
        try:
            return await self.service.speak_into_call(message)
        except Exception as e:
            logger.error(f"Failed to deliver SIP CALL message for user {user_id}: {e}")
            return False
