"""Test cases for the source supervisor restart/backoff behavior. See spec §4.2.

Covers: clean source return (no restart), restart on transient failures,
max restart limit with source_failed event emission, and CancelledError
propagation (no restart on cancellation).
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib

import pytest

from ares.core.event import EventBus, Priority
from ares.core.source import BaseSource

# Load instance/main.py via importlib since it is not a package.
_spec = importlib.util.spec_from_file_location(
    "instance_main",
    str(pathlib.Path(__file__).resolve().parent.parent / "instance" / "main.py"),
)
instance_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(instance_main)


class FlakySource(BaseSource):
    """A controllable test source that fails a specified number of times."""

    name = "flaky"

    def __init__(
        self, bus: EventBus, fail_times: int, then_return: bool = True
    ) -> None:
        """Initialize the FlakySource.

        Args:
            bus: The event bus to publish to.
            fail_times: Raise on the first N calls to start().
            then_return: If True, return cleanly after fail_times exceptions.
                        If False, keep raising after fail_times.
        """
        super().__init__(bus, {})
        self.fail_times = fail_times
        self.then_return = then_return
        self.starts = 0

    async def start(self) -> None:
        """Raise on the first fail_times calls, then return or keep failing."""
        self.starts += 1
        if self.starts <= self.fail_times:
            raise RuntimeError("boom")
        if self.then_return:
            return
        raise RuntimeError("still failing")


@pytest.fixture(autouse=True)
def zero_restart_delay() -> None:
    """Set SOURCE_RESTART_DELAY_S to 0 so tests run instantly."""
    original_delay = instance_main.SOURCE_RESTART_DELAY_S
    instance_main.SOURCE_RESTART_DELAY_S = 0
    yield
    instance_main.SOURCE_RESTART_DELAY_S = original_delay


async def test_clean_return_no_restart() -> None:
    """A source that returns cleanly should not restart.

    Verifies: if source.start() returns immediately, supervise() returns;
    src.starts == 1 (started once, not restarted).
    """
    bus = EventBus()
    source = FlakySource(bus, fail_times=0, then_return=True)

    await instance_main.supervise(source, bus)

    assert source.starts == 1
    assert bus.qsize() == 0


async def test_restart_on_failure_then_success() -> None:
    """A source that fails transiently should restart and eventually succeed.

    Verifies: FlakySource with fail_times=3 will fail 3 times then succeed.
    supervise() returns after it succeeds; src.starts == 4 (3 failures + 1 success).
    No source_failed event is published.
    """
    bus = EventBus()
    source = FlakySource(bus, fail_times=3, then_return=True)

    await instance_main.supervise(source, bus)

    assert source.starts == 4
    assert bus.qsize() == 0


async def test_max_restarts_then_source_failed() -> None:
    """A source that always fails should exhaust restarts and emit source_failed.

    Verifies: FlakySource with fail_times=999 (always fails) exhausts restarts.
    supervise() returns; src.starts == 11 (initial + 10 restarts, per spec).
    A source_failed NORMAL event is published with payload["source"] == "flaky".
    """
    bus = EventBus()
    source = FlakySource(bus, fail_times=999, then_return=False)

    await instance_main.supervise(source, bus)

    # 11 total calls: 1 initial + 10 restarts (when restarts becomes 11, > MAX_SOURCE_RESTARTS=10)
    assert source.starts == 11
    assert bus.qsize() >= 1

    event = await bus.get()
    assert event.type == "source_failed"
    assert event.priority == Priority.NORMAL
    assert event.payload["source"] == "flaky"


async def test_cancelled_error_not_restarted() -> None:
    """CancelledError should propagate without restart.

    Verifies: if source.start() raises CancelledError, supervise() re-raises
    it immediately without restarting. src.starts == 1 (started once).
    """
    bus = EventBus()

    class CancellingSource(BaseSource):
        name = "cancelling"

        def __init__(self, bus: EventBus) -> None:
            super().__init__(bus, {})
            self.starts = 0

        async def start(self) -> None:
            self.starts += 1
            raise asyncio.CancelledError()

    source = CancellingSource(bus)

    with pytest.raises(asyncio.CancelledError):
        await instance_main.supervise(source, bus)

    assert source.starts == 1
