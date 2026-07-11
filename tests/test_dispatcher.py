"""Test cases for the Dispatcher: per-user serialisation, LOW-drop, HIGH-jump.

See spec §4.3. Uses a fake agent whose `handle()` blocks on a controllable
gate (`asyncio.Event`) so tests can deterministically hold an event
"in-flight" and observe queue state, instead of racing on sleep durations.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from ares.core.channel import ChannelType  # noqa: F401  (imported per read-first list)
from ares.core.critical import CriticalHandlerRegistry
from ares.core.dispatcher import Dispatcher
from ares.core.event import Event, EventBus, Priority
from ares.core.router import ResponseRouter
from ares.core.session import SessionManager
from ares.core.utils.ids import new_id

# Bound on polling loops below: number of 10ms ticks to wait for a condition
# before giving up. Keeps tests deterministic (poll-until-true) without
# relying on a single fixed sleep duration.
_POLL_ITERS = 200
_POLL_STEP = 0.01


async def _poll_until(predicate, iters: int = _POLL_ITERS, step: float = _POLL_STEP) -> bool:
    """Poll `predicate()` until truthy or the iteration budget is exhausted."""
    for _ in range(iters):
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


class FakeAgent:
    """
    A minimal fake agent for dispatcher tests.

    `handle()` records the event as "started" immediately, bumps a
    concurrency counter (tracking the max seen), then blocks on `self.release`
    until the test lets it proceed, at which point it records the event as
    "handled". This lets tests hold a cycle in-flight deterministically and
    assert on ordering/concurrency without sleep races.
    """

    def __init__(self) -> None:
        self.sessions = SessionManager()
        self.started: list[Event] = []
        self.handled: list[Event] = []
        self.concurrency = 0
        self.max_concurrency = 0
        self.release = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.concurrency += 1
        self.max_concurrency = max(self.max_concurrency, self.concurrency)
        self.started.append(event)
        try:
            await self.release.wait()
        finally:
            self.concurrency -= 1
        self.handled.append(event)


def make_event(
    n: int, priority: Priority = Priority.NORMAL, user_id: str = "u"
) -> Event:
    """Build a test Event carrying payload {"n": n}."""
    return Event(
        id=new_id(),
        source="cli",
        type="cli_input",
        payload={"n": n},
        priority=priority,
        user_id=user_id,
    )


@pytest.fixture
async def harness():
    """
    Build a real EventBus + Dispatcher wired to a FakeAgent, with the
    dispatcher's run() loop running as a background task. Always cancels
    and awaits the task on teardown, even if the test fails.
    """
    bus = EventBus()
    agent = FakeAgent()
    router = ResponseRouter(SessionManager())
    critical = CriticalHandlerRegistry(router)
    dispatcher = Dispatcher(bus, agent, critical)
    task = asyncio.create_task(dispatcher.run())
    try:
        yield bus, agent, dispatcher
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_per_user_serialisation_and_fifo_order(harness):
    """Events for the same user are handled one at a time, in FIFO order."""
    bus, agent, _dispatcher = harness
    agent.release.set()  # let each event complete as soon as it's handled

    e1, e2, e3 = make_event(1), make_event(2), make_event(3)
    await bus.publish(e1)
    await bus.publish(e2)
    await bus.publish(e3)

    assert await _poll_until(lambda: len(agent.handled) >= 3)
    assert [e.payload["n"] for e in agent.handled] == [1, 2, 3]
    assert agent.max_concurrency == 1


async def test_different_users_are_isolated_and_can_run_concurrently(harness):
    """Per-user serialisation is not global: two users' events overlap."""
    bus, agent, _dispatcher = harness
    agent.release.clear()

    e_u1 = make_event(1, user_id="u1")
    e_u2 = make_event(2, user_id="u2")
    await bus.publish(e_u1)
    await bus.publish(e_u2)

    # Both should enter handle() and block on the (unset) gate concurrently.
    assert await _poll_until(lambda: len(agent.started) >= 2)
    assert agent.max_concurrency == 2

    agent.release.set()
    assert await _poll_until(lambda: len(agent.handled) >= 2)
    assert {e.payload["n"] for e in agent.handled} == {1, 2}


async def test_low_priority_dropped_when_busy_but_handled_when_idle(harness):
    """A LOW event is dropped if the user's worker is busy or queued, but
    processed normally when the user is idle with an empty queue."""
    bus, agent, _dispatcher = harness
    agent.release.clear()

    first = make_event(1, user_id="u")
    await bus.publish(first)

    # Ensure the first event is truly in-flight (busy) before publishing LOW.
    assert await _poll_until(lambda: len(agent.started) >= 1)
    assert agent.concurrency == 1

    low = make_event(99, priority=Priority.LOW, user_id="u")
    await bus.publish(low)

    # Let the dispatcher's run() loop pull the LOW event off the bus and
    # apply the drop policy (it sees the user is busy).
    assert await _poll_until(lambda: bus.qsize() == 0)

    # Release the in-flight event and let it complete.
    agent.release.set()
    assert await _poll_until(lambda: len(agent.handled) >= 1)

    assert all(e.payload["n"] != 99 for e in agent.started)
    assert all(e.payload["n"] != 99 for e in agent.handled)

    # Now the user is idle with an empty queue: a LOW event should go through.
    low2 = make_event(100, priority=Priority.LOW, user_id="u")
    await bus.publish(low2)
    assert await _poll_until(
        lambda: any(e.payload["n"] == 100 for e in agent.handled)
    )


async def test_high_priority_jumps_queue_but_does_not_preempt_in_flight(harness):
    """A HIGH event jumps ahead of already-queued NORMAL events, but does not
    interrupt an event that is already in-flight."""
    bus, agent, _dispatcher = harness
    agent.release.clear()

    first = make_event(1, user_id="u")
    await bus.publish(first)
    assert await _poll_until(lambda: len(agent.started) >= 1)
    assert agent.concurrency == 1  # first is in-flight (busy)

    n2 = make_event(2, user_id="u")
    n3 = make_event(3, user_id="u")
    high = make_event(99, priority=Priority.HIGH, user_id="u")
    await bus.publish(n2)
    await bus.publish(n3)
    await bus.publish(high)

    # Let the dispatcher's run() loop drain the bus and apply enqueue policy
    # (NORMAL appended at back, HIGH pushed to front) before we release.
    assert await _poll_until(lambda: bus.qsize() == 0)

    # The first event must still be the only one in-flight/handled so far —
    # HIGH does not preempt it.
    assert len(agent.started) == 1
    assert len(agent.handled) == 0

    agent.release.set()
    assert await _poll_until(lambda: len(agent.handled) >= 4)

    order = [e.payload["n"] for e in agent.handled]
    assert order == [1, 99, 2, 3]
