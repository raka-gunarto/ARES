"""Scheduler-driven recurring checks for monitoring tasks (spec §7.2).

The behaviour these lock down is the one the live trace showed missing: a
monitoring task promised "every 30 minutes" got exactly one check in nine
hours, because nothing but unrelated events ever woke the agent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ares.core.event import EventBus, Priority
from ares.core.tasks.store import TaskStore, check_schedule
from ares.plugins.sources.scheduler import SchedulerSource


class _NoMemory:
    async def prune_short_term(self, days):  # pragma: no cover - unused here
        return 0


async def _sched(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    bus = EventBus()
    return store, bus, SchedulerSource(bus, {}, store, _NoMemory(), 14)


async def test_check_emits_task_check_and_rearms(tmp_path):
    store, bus, sched = await _sched(tmp_path)
    now = datetime.now(timezone.utc)
    t = await store.create(
        "primary", "monitoring", title="watch temps",
        detail="check both rooms", trigger="both below 22C",
        data=check_schedule(1800, first_at=now - timedelta(seconds=1)),
    )

    await sched._run_due_checks(now)

    assert bus.qsize() == 1
    ev = await bus.get()
    assert ev.type == "task_check"
    assert ev.priority == Priority.NORMAL, "a routine check must not jump the queue"
    assert ev.payload["task_id"] == t.id
    # trigger/detail are NOT in the rendered system prompt, so the event must
    # carry them or the agent is told to check without being told what.
    assert ev.payload["trigger"] == "both below 22C"
    assert ev.payload["detail"] == "check both rooms"
    assert ev.payload["final"] is False

    # Re-armed roughly one interval out, and no longer due right now.
    again = await store.update(t.id)
    assert again.data["check_interval_s"] == 1800
    assert await store.list_checks_due(now) == []
    await store.aclose()


async def test_check_repeats_unlike_a_reminder(tmp_path):
    """A reminder fires once (`fired`); a check must keep coming back."""
    store, bus, sched = await _sched(tmp_path)
    now = datetime.now(timezone.utc)
    await store.create(
        "primary", "monitoring", title="watch",
        data=check_schedule(600, first_at=now - timedelta(seconds=1)),
    )

    fired = 0
    for tick in range(4):
        moment = now + timedelta(seconds=600 * tick)
        await sched._run_due_checks(moment)
        fired += bus.qsize()
        while bus.qsize():
            await bus.get()
    assert fired == 4, f"expected a check per interval, got {fired}"
    await store.aclose()


async def test_due_at_bounds_the_watch_and_flags_the_last_check(tmp_path):
    """'keep going until 9am' must actually stop, and say so."""
    store, bus, sched = await _sched(tmp_path)
    now = datetime.now(timezone.utc)
    t = await store.create(
        "primary", "monitoring", title="until 9",
        due_at=(now - timedelta(minutes=1)).isoformat(),
        data=check_schedule(600, first_at=now - timedelta(seconds=1)),
    )

    await sched._run_due_checks(now)
    ev = await bus.get()
    assert ev.payload["final"] is True

    # Disarmed: no further checks, and it did not silently keep polling.
    after = await store.update(t.id)
    assert "check_interval_s" not in after.data
    assert await store.list_checks_due(now + timedelta(hours=2)) == []
    await store.aclose()


async def test_unarmed_monitoring_task_emits_nothing(tmp_path):
    """The old passive-note behaviour stays available and stays silent."""
    store, bus, sched = await _sched(tmp_path)
    now = datetime.now(timezone.utc)
    await store.create("primary", "monitoring", title="passive")
    await sched._run_due_checks(now)
    assert bus.qsize() == 0
    await store.aclose()


async def test_reminders_still_fire_once_alongside_checks(tmp_path):
    """The new path must not disturb the existing reminder path."""
    store, bus, sched = await _sched(tmp_path)
    now = datetime.now(timezone.utc)
    await store.create(
        "primary", "reminder_pending", title="ping",
        due_at=(now - timedelta(minutes=1)).isoformat(),
    )
    due = await store.list_due(now)
    assert len(due) == 1
    # And that reminder is not also treated as a recurring check.
    await sched._run_due_checks(now)
    assert bus.qsize() == 0
    await store.aclose()
