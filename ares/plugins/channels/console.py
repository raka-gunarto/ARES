from __future__ import annotations

import typing

from ares.core.channel import BaseChannel, ChannelType

if typing.TYPE_CHECKING:
    from ares.core.session import Session


class ConsoleChannel(BaseChannel):
    """Message delivery channel that prints to console."""

    type = ChannelType.CONSOLE

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Deliver a message to console.

        Args:
            user_id: The user ID (unused for console delivery).
            message: The message text to print.
            session: The user's current session (unused for console delivery).

        Returns:
            True (console delivery always succeeds).
        """
        print(f"ARES> {message}")
        return True
