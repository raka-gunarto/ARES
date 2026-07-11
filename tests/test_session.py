"""Test cases for Session and SessionManager."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ares.core.channel import ChannelType
from ares.core.session import Session, SessionManager


def test_timeout_clears_history_but_preserves_channel_and_room():
    """Test that timeout clears history while preserving channel and room."""
    mgr = SessionManager(history_limit=30, timeout_minutes=45)

    # Create a session by touching with a channel and room
    session = mgr.touch("u", ChannelType.WEB, "kitchen")
    assert session.active_channel == ChannelType.WEB
    assert session.current_room == "kitchen"

    # Append a message to history
    mgr.append_history("u", "user", "hi")
    assert len(session.history) == 1
    assert session.history[0]["content"] == "hi"

    # Artificially age the session to trigger timeout (60 minutes ago)
    session.last_activity = datetime.now(timezone.utc) - timedelta(minutes=60)

    # Retrieve the session again — history should be cleared
    retrieved = mgr.get("u")
    assert retrieved.history == []
    assert retrieved.active_channel == ChannelType.WEB
    assert retrieved.current_room == "kitchen"

    # Verify that without timeout (recent activity), history is NOT cleared
    mgr2 = SessionManager(history_limit=30, timeout_minutes=45)
    mgr2.touch("u2", ChannelType.CONSOLE, "room1")
    mgr2.append_history("u2", "user", "message1")

    # Keep activity recent (don't age it)
    retrieved2 = mgr2.get("u2")
    assert len(retrieved2.history) == 1
    assert retrieved2.history[0]["content"] == "message1"


def test_history_trim():
    """Test that history is trimmed to the last history_limit messages."""
    mgr = SessionManager(history_limit=5)

    # Append 8 messages
    messages = [f"msg{i}" for i in range(1, 9)]
    for i, msg in enumerate(messages):
        mgr.append_history("u", "user" if i % 2 == 0 else "assistant", msg)

    session = mgr.get("u")

    # Assert that only the last 5 messages are retained
    assert len(session.history) == 5

    # Verify the retained messages are the LAST 5 in order
    retained_contents = [h["content"] for h in session.history]
    assert retained_contents == ["msg4", "msg5", "msg6", "msg7", "msg8"]

    # Check the content of the first and last retained items
    assert session.history[0]["content"] == "msg4"
    assert session.history[-1]["content"] == "msg8"


def test_touch_updates_channel_room_and_last_activity():
    """Test that touch updates channel, room, and last_activity appropriately."""
    mgr = SessionManager()

    # Test 1: touch with channel and room sets them
    session = mgr.touch("u", ChannelType.SIP_MESSAGE, "hall")
    assert session.active_channel == ChannelType.SIP_MESSAGE
    assert session.current_room == "hall"
    initial_activity = session.last_activity

    # Small sleep equivalent: capture the current time after a tiny delay
    # to ensure last_activity advances
    import time
    time.sleep(0.01)

    # Test 2: touch with None, None preserves channel and room but advances last_activity
    session = mgr.touch("u", None, None)
    assert session.active_channel == ChannelType.SIP_MESSAGE
    assert session.current_room == "hall"
    assert session.last_activity >= initial_activity
    assert session.last_activity > initial_activity  # should be strictly newer

    # Capture this activity timestamp
    second_activity = session.last_activity

    # Test 3: touch with channel only changes channel but preserves room
    time.sleep(0.01)
    session = mgr.touch("u", ChannelType.VOICE, None)
    assert session.active_channel == ChannelType.VOICE
    assert session.current_room == "hall"
    assert session.last_activity >= second_activity
