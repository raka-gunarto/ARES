"""Channel abstraction for message delivery."""
from __future__ import annotations

import abc
import enum
import typing

if typing.TYPE_CHECKING:
    from ares.core.session import Session


class ChannelType(enum.StrEnum):
    """Enumeration of available message delivery channels."""

    VOICE = "voice"
    SIP_CALL = "sip_call"
    SIP_MESSAGE = "sip_message"
    PUSH = "push"
    CONSOLE = "console"
    WEB = "web"


class BaseChannel(abc.ABC):
    """Abstract base class for message delivery channels."""

    type: ChannelType

    @abc.abstractmethod
    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Deliver a message to a user via this channel.

        Args:
            user_id: The user ID to deliver to.
            message: The message text to deliver.
            session: The user's current session.

        Returns:
            False if delivery failed, True on success.
        """
        ...
