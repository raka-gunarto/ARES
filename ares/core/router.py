"""Response routing for message delivery across channels."""
from __future__ import annotations

from ares.core.channel import BaseChannel, ChannelType
from ares.core.session import SessionManager
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class ResponseRouter:
    """Routes messages to appropriate delivery channels with fallback logic."""

    def __init__(self, sessions: SessionManager) -> None:
        """
        Initialize the response router.

        Args:
            sessions: The session manager for retrieving user sessions.
        """
        self.sessions = sessions
        self._channels: dict[ChannelType, BaseChannel] = {}

    def register(self, channel: BaseChannel) -> None:
        """
        Register a delivery channel.

        Args:
            channel: The channel to register (must have a type attribute).
        """
        self._channels[channel.type] = channel

    async def speak(self, user_id: str, message: str) -> None:
        """
        Deliver a message via the user's active channel with fallback logic.

        Attempts delivery in order: active_channel → PUSH → CONSOLE.
        On channel failure or missing channel, continues to next fallback.
        Logs an error if all channels fail.

        Args:
            user_id: The user ID.
            message: The message text to deliver.
        """
        session = self.sessions.get(user_id)
        primary_channel_type = session.active_channel

        # Try primary channel
        if primary_channel_type in self._channels:
            channel = self._channels[primary_channel_type]
            try:
                ok = await channel.deliver(user_id, message, session)
                if ok:
                    return
            except Exception as e:
                log.exception(
                    "Channel %s.deliver failed for user %s: %s",
                    primary_channel_type,
                    user_id,
                    e,
                )

        # Fallback order: PUSH, then CONSOLE
        for fallback_type in [ChannelType.PUSH, ChannelType.CONSOLE]:
            # Skip if it's the channel we already tried
            if fallback_type == primary_channel_type:
                continue

            if fallback_type in self._channels:
                channel = self._channels[fallback_type]
                try:
                    ok = await channel.deliver(user_id, message, session)
                    if ok:
                        return
                except Exception as e:
                    log.exception(
                        "Channel %s.deliver failed for user %s: %s",
                        fallback_type,
                        user_id,
                        e,
                    )

        # No channel delivered the message
        log.error("Message not delivered for user %s: no channel succeeded", user_id)

    async def notify(self, user_id: str, message: str) -> None:
        """
        Deliver a notification via PUSH channel with CONSOLE fallback.

        Attempts delivery in order: PUSH → CONSOLE.
        Logs an error if all channels fail.

        Args:
            user_id: The user ID.
            message: The notification text to deliver.
        """
        session = self.sessions.get(user_id)

        # Try PUSH first
        if ChannelType.PUSH in self._channels:
            channel = self._channels[ChannelType.PUSH]
            try:
                ok = await channel.deliver(user_id, message, session)
                if ok:
                    return
            except Exception as e:
                log.exception(
                    "Channel %s.deliver failed for user %s: %s",
                    ChannelType.PUSH,
                    user_id,
                    e,
                )

        # Fall back to CONSOLE
        if ChannelType.CONSOLE in self._channels:
            channel = self._channels[ChannelType.CONSOLE]
            try:
                ok = await channel.deliver(user_id, message, session)
                if ok:
                    return
            except Exception as e:
                log.exception(
                    "Channel %s.deliver failed for user %s: %s",
                    ChannelType.CONSOLE,
                    user_id,
                    e,
                )

        # No channel delivered the message
        log.error("Notification not delivered for user %s: no channel succeeded", user_id)
