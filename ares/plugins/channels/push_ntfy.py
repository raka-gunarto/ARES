"""Push notification channel via ntfy.sh."""
from __future__ import annotations

import typing

import httpx

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session


logger = get_logger(__name__)


class NtfyChannel(BaseChannel):
    """Push notification channel using ntfy.sh HTTP API."""

    type = ChannelType.PUSH

    def __init__(self, server: str, token: str | None, topics: dict[str, str]) -> None:
        """
        Initialize the ntfy channel.

        Args:
            server: Base URL of the ntfy server (trailing slash will be removed).
            token: Optional authentication token for the ntfy server.
            topics: Mapping of user_id to ntfy topic string.
        """
        self.server = server.rstrip("/")
        self.token = token or ""
        self.topics = topics

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """
        Deliver a message to a user via ntfy.sh.

        Args:
            user_id: The user ID to deliver to.
            message: The message text to deliver.
            session: The user's current session.

        Returns:
            True on success (2xx status), False otherwise.
        """
        topic = self.topics.get(user_id)
        if not topic:
            logger.warning(f"No ntfy topic configured for user {user_id}")
            return False

        url = f"{self.server}/{topic}"
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    content=message.encode("utf-8"),
                    headers=headers,
                )
            return 200 <= resp.status_code < 300
        except Exception as e:
            logger.error(f"Failed to deliver message via ntfy to {user_id}: {e}")
            return False
