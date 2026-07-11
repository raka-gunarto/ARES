"""Test cases for ResponseRouter delivery logic.

Covers delivery-time channel read, fallback on deliver False, and notify path.
See spec §4.5 for router behavior and fallback order.
"""
from __future__ import annotations

import asyncio

import pytest

from ares.core.channel import BaseChannel, ChannelType
from ares.core.router import ResponseRouter
from ares.core.session import Session, SessionManager


class RecordingChannel(BaseChannel):
    """Test fixture channel that records all delivery attempts and can be configured to fail."""

    def __init__(self, ctype: ChannelType, succeed: bool = True) -> None:
        """
        Initialize a recording channel.

        Args:
            ctype: The channel type (overrides class attribute for testing).
            succeed: Whether deliver() returns True (success) or False (failure).
        """
        self.type = ctype  # Instance attribute overrides class attribute
        self.succeed = succeed
        self.delivered: list[tuple[str, str]] = []  # List of (user_id, message) tuples
        self.delivery_attempts: int = 0  # Total times deliver() was called

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Record the delivery attempt and return success/failure as configured."""
        self.delivery_attempts += 1
        self.delivered.append((user_id, message))
        return self.succeed


class FailingChannel(BaseChannel):
    """Test fixture channel that raises an exception during deliver."""

    def __init__(self, ctype: ChannelType) -> None:
        """Initialize a channel that always raises."""
        self.type = ctype
        self.delivery_attempts: int = 0

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Record the attempt and raise an exception."""
        self.delivery_attempts += 1
        raise RuntimeError(f"Simulated delivery failure for {self.type}")


@pytest.fixture
def sessions():
    """Create a fresh SessionManager for each test."""
    return SessionManager()


@pytest.fixture
def router(sessions):
    """Create a fresh ResponseRouter with the given sessions."""
    return ResponseRouter(sessions)


@pytest.mark.asyncio
async def test_delivery_time_channel_read(sessions, router):
    """Test that router reads active_channel AT DELIVERY TIME, not at registration.

    Scenario:
    - Register CONSOLE and VOICE channels.
    - With default active_channel=CONSOLE, speak should deliver via CONSOLE.
    - Change active_channel to VOICE using touch().
    - Next speak should deliver via VOICE, proving the router reads the session
      at delivery time, not at registration.
    """
    console = RecordingChannel(ChannelType.CONSOLE)
    voice = RecordingChannel(ChannelType.VOICE)
    router.register(console)
    router.register(voice)

    # First speak: default active_channel is CONSOLE
    await router.speak("primary", "hi")
    assert len(console.delivered) == 1
    assert console.delivered[0] == ("primary", "hi")
    assert len(voice.delivered) == 0

    # Change active_channel to VOICE
    sessions.touch("primary", ChannelType.VOICE, None)

    # Second speak: should now use VOICE (read at delivery time)
    await router.speak("primary", "again")
    assert len(console.delivered) == 1  # No additional delivery
    assert len(voice.delivered) == 1
    assert voice.delivered[0] == ("primary", "again")


@pytest.mark.asyncio
async def test_push_fallback_on_deliver_false(sessions, router):
    """Test that speak falls back to PUSH when primary channel returns False.

    Scenario:
    - VOICE channel fails (returns False).
    - PUSH channel succeeds (returns True).
    - CONSOLE channel succeeds.
    - Set active_channel=VOICE.
    - speak should try VOICE, see it failed, fall back to PUSH, and stop.
    - CONSOLE should NOT receive the message (router stops after PUSH succeeds).
    """
    voice_fail = RecordingChannel(ChannelType.VOICE, succeed=False)
    push_succeed = RecordingChannel(ChannelType.PUSH, succeed=True)
    console_succeed = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(voice_fail)
    router.register(push_succeed)
    router.register(console_succeed)

    # Set active channel to VOICE (which will fail)
    sessions.touch("primary", ChannelType.VOICE, None)

    # Speak should fall back to PUSH
    await router.speak("primary", "msg")

    # VOICE was tried but failed
    assert len(voice_fail.delivered) == 1
    assert voice_fail.delivered[0] == ("primary", "msg")

    # PUSH succeeded and was used
    assert len(push_succeed.delivered) == 1
    assert push_succeed.delivered[0] == ("primary", "msg")

    # CONSOLE should NOT receive the message (router stops after PUSH)
    assert len(console_succeed.delivered) == 0


@pytest.mark.asyncio
async def test_push_also_fails_fallback_to_console(sessions, router):
    """Test fallback chain: VOICE (fail) → PUSH (fail) → CONSOLE (success).

    Scenario:
    - VOICE, PUSH, and CONSOLE channels are registered.
    - VOICE and PUSH return False; CONSOLE returns True.
    - Set active_channel=VOICE.
    - speak should try VOICE (fail), PUSH (fail), then CONSOLE (succeed).
    """
    voice_fail = RecordingChannel(ChannelType.VOICE, succeed=False)
    push_fail = RecordingChannel(ChannelType.PUSH, succeed=False)
    console_succeed = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(voice_fail)
    router.register(push_fail)
    router.register(console_succeed)

    sessions.touch("primary", ChannelType.VOICE, None)

    await router.speak("primary", "msg")

    # All three should have been tried in order
    assert len(voice_fail.delivered) == 1
    assert len(push_fail.delivered) == 1
    assert len(console_succeed.delivered) == 1
    assert console_succeed.delivered[0] == ("primary", "msg")


@pytest.mark.asyncio
async def test_notify_path_push_first(sessions, router):
    """Test that notify prefers PUSH channel over CONSOLE.

    Scenario:
    - PUSH and CONSOLE channels are registered (both succeed).
    - notify should use PUSH, not CONSOLE.
    """
    push = RecordingChannel(ChannelType.PUSH, succeed=True)
    console = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(push)
    router.register(console)

    await router.notify("primary", "ping")

    # PUSH should receive the notification
    assert len(push.delivered) == 1
    assert push.delivered[0] == ("primary", "ping")

    # CONSOLE should NOT receive it (notify prefers PUSH)
    assert len(console.delivered) == 0


@pytest.mark.asyncio
async def test_notify_fallback_to_console(sessions, router):
    """Test that notify falls back to CONSOLE when PUSH fails.

    Scenario:
    - PUSH channel fails (returns False).
    - CONSOLE channel succeeds.
    - notify should try PUSH, see it failed, fall back to CONSOLE.
    """
    push_fail = RecordingChannel(ChannelType.PUSH, succeed=False)
    console = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(push_fail)
    router.register(console)

    await router.notify("primary", "ping")

    # PUSH was tried but failed
    assert len(push_fail.delivered) == 1

    # CONSOLE was used as fallback
    assert len(console.delivered) == 1
    assert console.delivered[0] == ("primary", "ping")


@pytest.mark.asyncio
async def test_missing_channel_fallback(sessions, router):
    """Test that speak doesn't crash when active_channel is not registered.

    Scenario:
    - Only CONSOLE channel is registered.
    - Set active_channel=VOICE (which is NOT registered).
    - speak should skip the missing VOICE channel, try PUSH (also missing),
      and fall back to CONSOLE (which succeeds).
    - No exception should be raised.
    """
    console = RecordingChannel(ChannelType.CONSOLE, succeed=True)
    router.register(console)

    # Set active_channel to VOICE, which is not registered
    sessions.touch("primary", ChannelType.VOICE, None)

    # This should not raise an exception
    await router.speak("primary", "fallback_test")

    # Should end up at CONSOLE
    assert len(console.delivered) == 1
    assert console.delivered[0] == ("primary", "fallback_test")


@pytest.mark.asyncio
async def test_exception_isolation(sessions, router):
    """Test that channel exceptions don't crash speak; router logs and falls back.

    Scenario:
    - A channel whose deliver() raises an exception.
    - CONSOLE channel that succeeds.
    - Set active_channel to the failing channel.
    - speak should catch the exception, log it, and fall back to CONSOLE.
    """
    failing = FailingChannel(ChannelType.VOICE)
    console = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(failing)
    router.register(console)

    sessions.touch("primary", ChannelType.VOICE, None)

    # This should not raise an exception despite the failing channel
    await router.speak("primary", "exception_test")

    # Failing channel was attempted
    assert failing.delivery_attempts == 1

    # CONSOLE was used as fallback
    assert len(console.delivered) == 1
    assert console.delivered[0] == ("primary", "exception_test")


@pytest.mark.asyncio
async def test_multiple_users_independent_sessions(sessions, router):
    """Test that router correctly routes messages to multiple users independently.

    Scenario:
    - Two users with different active_channels.
    - Each should get their message via their active_channel.
    """
    console = RecordingChannel(ChannelType.CONSOLE)
    voice = RecordingChannel(ChannelType.VOICE)

    router.register(console)
    router.register(voice)

    # Set user1 to CONSOLE, user2 to VOICE
    sessions.touch("user1", ChannelType.CONSOLE, None)
    sessions.touch("user2", ChannelType.VOICE, None)

    await router.speak("user1", "hello")
    await router.speak("user2", "world")

    # user1 should have gone to CONSOLE
    console_user1 = [entry for entry in console.delivered if entry[0] == "user1"]
    assert len(console_user1) == 1
    assert console_user1[0][1] == "hello"

    # user2 should have gone to VOICE
    voice_user2 = [entry for entry in voice.delivered if entry[0] == "user2"]
    assert len(voice_user2) == 1
    assert voice_user2[0][1] == "world"


@pytest.mark.asyncio
async def test_notify_no_active_channel_needed(sessions, router):
    """Test that notify works without setting active_channel (uses only PUSH/CONSOLE).

    Scenario:
    - Create a session for a user without touching it (active_channel defaults to CONSOLE).
    - notify should try PUSH first (if available), then CONSOLE.
    - notify does NOT use active_channel.
    """
    push = RecordingChannel(ChannelType.PUSH, succeed=True)
    console = RecordingChannel(ChannelType.CONSOLE, succeed=True)

    router.register(push)
    router.register(console)

    # Create a session without explicit touch (defaults to CONSOLE active_channel)
    sessions.get("primary")

    await router.notify("primary", "notification")

    # PUSH should receive it (notify ignores active_channel)
    assert len(push.delivered) == 1
    assert push.delivered[0] == ("primary", "notification")

    # CONSOLE should not
    assert len(console.delivered) == 0
