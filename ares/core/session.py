"""Session management for user conversations."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from ares.core.channel import ChannelType


@dataclasses.dataclass
class Session:
    """Represents a user's active conversation session."""

    user_id: str
    active_channel: ChannelType = ChannelType.CONSOLE
    current_room: str | None = None
    history: list[dict] = dataclasses.field(default_factory=list)
    last_activity: datetime = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class SessionManager:
    """Manages in-memory user sessions."""

    def __init__(self, history_limit: int = 30, timeout_minutes: int = 45) -> None:
        """
        Initialize the session manager.

        Args:
            history_limit: Maximum number of messages to retain in history.
            timeout_minutes: Minutes of inactivity before history is cleared.
        """
        self.history_limit = history_limit
        self.timeout_minutes = timeout_minutes
        self._sessions: dict[str, Session] = {}

    def get(self, user_id: str) -> Session:
        """
        Get or create a session for a user.

        If the session exists and has timed out (last_activity older than
        timeout_minutes), the history is cleared but active_channel and
        current_room are preserved.

        Args:
            user_id: The user ID.

        Returns:
            The user's session.
        """
        if user_id not in self._sessions:
            self._sessions[user_id] = Session(user_id=user_id)
            return self._sessions[user_id]

        session = self._sessions[user_id]
        elapsed = datetime.now(timezone.utc) - session.last_activity
        if elapsed > timedelta(minutes=self.timeout_minutes):
            session.history = []

        return session

    def touch(
        self, user_id: str, channel: ChannelType | None, room: str | None
    ) -> Session:
        """
        Get or create a session and update its channel, room, and last_activity.

        Args:
            user_id: The user ID.
            channel: If not None, set the active channel.
            room: If not None, set the current room.

        Returns:
            The updated session.
        """
        session = self.get(user_id)
        if channel is not None:
            session.active_channel = channel
        if room is not None:
            session.current_room = room
        session.last_activity = datetime.now(timezone.utc)
        return session

    def append_history(self, user_id: str, role: str, content: str) -> None:
        """
        Append a message to a user's conversation history.

        The history is trimmed to retain only the last history_limit messages.

        Args:
            user_id: The user ID.
            role: The message role (e.g., "user", "assistant").
            content: The message content.
        """
        session = self.get(user_id)
        session.history.append({"role": role, "content": content})
        session.history = session.history[-self.history_limit :]
