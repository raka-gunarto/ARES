"""Web channel for dashboard message delivery via a cursor-replayable buffer."""
from __future__ import annotations

import asyncio
import time
import typing

from ares.core.channel import BaseChannel, ChannelType
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.session import Session


logger = get_logger(__name__)

# Presence for the web channel means "a long-poll is actually connected right
# now," not "polled recently." A backgrounded/closed browser (common on mobile,
# where the tab is frozen while you're out) can look recently-active for many
# seconds yet be unable to receive anything — so a 'recent poll' window let the
# router commit a reply to an outbox no live connection would drain, and the
# message was lost instead of falling through to push. So presence is primarily
# the count of in-flight polls; a short grace bridges the sub-second gap between
# one long-poll returning and the browser re-issuing the next.
PRESENCE_GRACE_S = 10.0

# The recent tail of the conversation is kept so a browser that reconnects can
# replay everything it missed while suspended (cursor-based catch-up). This is
# what closes the residual v1.14 race: a reply committed to a poll that never
# reached a frozen mobile tab is no longer lost — the tab re-polls from its last
# cursor on wake and gets it. Bounded so an always-open dashboard can't grow it
# without limit.
MAX_BUFFER = 100


class WebChannel(BaseChannel):
    """Message delivery for the web dashboard via a replayable message buffer.

    Every delivered message is appended to a per-user ring buffer with a
    monotonic sequence number. The browser long-polls with the cursor it last
    saw; the endpoint returns everything after it plus a new cursor. Because the
    record lives in the buffer rather than a single poll's response, a poll that
    is lost (a suspended tab) costs nothing — the browser replays on reconnect.
    """

    type = ChannelType.WEB

    def __init__(self) -> None:
        """Initialize empty per-user buffers, cursors, and wakeup events."""
        self._buffers: dict[str, list[tuple[int, str]]] = {}
        self._seq: dict[str, int] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._waiters: dict[str, int] = {}
        self._last_poll: dict[str, float] = {}

    def poll_started(self, user_id: str) -> None:
        """Record that a long-poll connection just opened for this user."""
        self._waiters[user_id] = self._waiters.get(user_id, 0) + 1

    def poll_finished(self, user_id: str) -> None:
        """Record that a long-poll connection just closed for this user.

        The close time starts the grace window that covers the brief gap until
        the browser re-issues its next poll.
        """
        self._waiters[user_id] = max(0, self._waiters.get(user_id, 0) - 1)
        self._last_poll[user_id] = time.monotonic()

    def is_present(self, user_id: str) -> bool:
        """True if a long-poll is connected now, or one closed within the grace.

        Waiter count is the real signal: a live poll receives a delivered
        message immediately. The grace only bridges the sub-second gap between
        consecutive polls of a continuously-polling browser.
        """
        if self._waiters.get(user_id, 0) > 0:
            return True
        last = self._last_poll.get(user_id)
        return last is not None and (time.monotonic() - last) <= PRESENCE_GRACE_S

    def _event(self, user_id: str) -> asyncio.Event:
        """Get or create the wakeup event a waiting poll blocks on."""
        ev = self._events.get(user_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[user_id] = ev
        return ev

    def cursor(self, user_id: str) -> int:
        """The current high-water sequence number for a user."""
        return self._seq.get(user_id, 0)

    def pending_count(self, user_id: str) -> int:
        """How many messages are held in the replay buffer (for health)."""
        return len(self._buffers.get(user_id, ()))

    def messages_since(
        self, user_id: str, since: int | None
    ) -> tuple[list[str], int]:
        """Messages buffered after `since`, plus the new cursor.

        `since is None` is a fresh page load: return nothing and the current
        cursor, so the browser starts from "now" instead of replaying old
        history. Otherwise return every buffered message with a higher sequence
        number (best-effort: anything trimmed from the buffer is simply skipped).

        A `since` greater than the current high-water mark means the browser is
        ahead of us — the daemon restarted and the counter reset. Treat that as
        a resync: replay the whole buffer rather than silently skipping the
        messages produced since the restart.
        """
        high = self._seq.get(user_id, 0)
        if since is None:
            return [], high
        if since > high:
            since = 0
        buf = self._buffers.get(user_id, [])
        msgs = [msg for seq, msg in buf if seq > since]
        return msgs, high

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Buffer a message for the web dashboard and wake any waiting poll.

        The message is *always* recorded (with a fresh sequence number) so a
        reconnecting browser can replay it. The return value reports only
        whether a browser is present *now*: when it is not, the router falls
        through to a channel that reaches the user (speaker/push), while the
        buffered copy still lets the dashboard show the reply in the transcript
        when it is next opened.

        Args:
            user_id: The user ID to deliver to.
            message: The message text to deliver.
            session: The user's current session.

        Returns:
            True if a browser is currently long-polling (so the reply reaches it
            live); False if none is, so the router continues its fallback chain.
        """
        seq = self._seq.get(user_id, 0) + 1
        self._seq[user_id] = seq
        buf = self._buffers.setdefault(user_id, [])
        buf.append((seq, message))
        if len(buf) > MAX_BUFFER:
            del buf[: len(buf) - MAX_BUFFER]

        # Wake any poll blocked on this user, then reset so the next wait blocks.
        ev = self._event(user_id)
        ev.set()
        ev.clear()

        present = self.is_present(user_id)
        if not present:
            logger.debug("web channel: no active poll for %s; buffered only", user_id)
        return present

    async def wait_for_messages(self, user_id: str, timeout: float) -> None:
        """Block until a new message is delivered for this user, or timeout.

        Swallows the timeout: callers re-check the buffer after this returns
        either way.
        """
        try:
            await asyncio.wait_for(self._event(user_id).wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
