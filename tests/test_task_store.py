from __future__ import annotations

import pytest
import aiosqlite
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ares.core.tasks.store import TaskStore, Task


@pytest.fixture
async def store(tmp_path: Path) -> TaskStore:
    """Fixture that creates and initializes a TaskStore for each test."""
    task_store = TaskStore(tmp_path / "tasks.db")
    await task_store.init()
    yield task_store
    await task_store.aclose()


class TestCRUD:
    """Test create, read (list_open), update operations."""

    async def test_create_returns_task_with_id_and_defaults(self, store: TaskStore) -> None:
        """Test that create returns a Task with a non-empty id, status open, and empty data dict."""
        task = await store.create(
            "primary",
            "reminder_pending",
            "Check oven",
            detail="gas hob",
            due_at="2030-01-01T00:00:00+00:00",
        )

        assert task.id
        assert task.status == "open"
        assert task.type == "reminder_pending"
        assert task.title == "Check oven"
        assert task.detail == "gas hob"
        assert isinstance(task.data, dict)
        assert task.data == {}
        assert task.due_at == "2030-01-01T00:00:00+00:00"

    async def test_list_open_includes_created_task(self, store: TaskStore) -> None:
        """Test that a created task appears in list_open."""
        task = await store.create("primary", "reminder_pending", "Check oven")
        open_tasks = await store.list_open("primary")

        assert len(open_tasks) == 1
        assert open_tasks[0].id == task.id
        assert open_tasks[0].title == "Check oven"

    async def test_update_changes_title(self, store: TaskStore) -> None:
        """Test that update changes the title and bumps updated_at."""
        task = await store.create("primary", "reminder_pending", "Check oven")
        original_updated_at = task.updated_at

        updated_task = await store.update(task.id, title="Check the oven")

        assert updated_task is not None
        assert updated_task.title == "Check the oven"
        assert updated_task.updated_at > original_updated_at

    async def test_update_nonexistent_returns_none(self, store: TaskStore) -> None:
        """Test that update on a nonexistent task_id returns None."""
        result = await store.update("nope", title="something")
        assert result is None

    async def test_update_data_round_trips(self, store: TaskStore) -> None:
        """Test that data dict is stored and retrieved correctly."""
        task = await store.create("primary", "reminder_pending", "Check oven")

        updated = await store.update(task.id, data={"fired": True})

        assert updated is not None
        assert updated.data == {"fired": True}
        assert isinstance(updated.data, dict)


class TestListDueBoundary:
    """Test list_due boundary conditions for reminder_pending tasks."""

    async def test_list_due_returns_past_task_only(self, store: TaskStore) -> None:
        """Test that list_due returns only tasks due in the past, not future."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=1)).isoformat()
        future = (now + timedelta(hours=1)).isoformat()

        task_past = await store.create(
            "primary", "reminder_pending", "Past task", due_at=past
        )
        task_future = await store.create(
            "primary", "reminder_pending", "Future task", due_at=future
        )

        due = await store.list_due(now)

        assert len(due) == 1
        assert due[0].id == task_past.id

    async def test_list_due_includes_at_boundary_inclusive(self, store: TaskStore) -> None:
        """Test that list_due includes tasks due exactly at the boundary (<=)."""
        now = datetime.now(timezone.utc)
        exactly_now = now.isoformat()

        task = await store.create(
            "primary", "reminder_pending", "Exactly now task", due_at=exactly_now
        )

        due = await store.list_due(now)

        assert len(due) == 1
        assert due[0].id == task.id

    async def test_list_due_excludes_non_reminder_pending(self, store: TaskStore) -> None:
        """Test that list_due only returns reminder_pending, not other types with past due_at."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=1)).isoformat()

        # Create a non-reminder_pending task with past due_at
        task = await store.create(
            "primary", "awaiting_response", "Not a reminder", due_at=past
        )

        due = await store.list_due(now)

        assert len(due) == 0
        # But the task itself should exist in list_open
        open_tasks = await store.list_open("primary")
        assert len(open_tasks) == 1
        assert open_tasks[0].id == task.id

    async def test_list_due_excludes_closed_tasks(self, store: TaskStore) -> None:
        """Test that list_due does not return closed reminder_pending tasks."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=1)).isoformat()

        task = await store.create(
            "primary", "reminder_pending", "Closed reminder", due_at=past
        )
        await store.close(task.id, "handled")

        due = await store.list_due(now)

        assert len(due) == 0

    async def test_list_due_matches_z_suffix_and_naive_formats(self, store: TaskStore) -> None:
        """A due_at with a 'Z' suffix or no offset still fires (parsed, not string-compared)."""
        now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        z_task = await store.create(
            "primary", "reminder_pending", "Z suffix", due_at="2030-06-01T11:30:00Z"
        )
        naive_task = await store.create(
            "primary", "reminder_pending", "naive UTC", due_at="2030-06-01T11:45:00"
        )
        await store.create(
            "primary", "reminder_pending", "future Z", due_at="2030-06-01T12:30:00Z"
        )

        due_ids = {t.id for t in await store.list_due(now)}
        assert due_ids == {z_task.id, naive_task.id}

    async def test_list_due_skips_unparseable_due_at(self, store: TaskStore) -> None:
        """A malformed due_at is skipped, not fatal to the scheduler loop."""
        now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
        good = await store.create(
            "primary", "reminder_pending", "good", due_at="2030-06-01T11:00:00Z"
        )
        await store.create(
            "primary", "reminder_pending", "bad", due_at="whenever"
        )

        due = await store.list_due(now)
        assert [t.id for t in due] == [good.id]


class TestClose:
    """Test close operation and its side effects."""

    async def test_close_sets_resolution_and_closed_at(self, store: TaskStore) -> None:
        """Test that close sets status, resolution, and closed_at."""
        task = await store.create("primary", "reminder_pending", "Task to close")

        closed_task = await store.close(task.id, "handled")

        assert closed_task is not None
        assert closed_task.status == "closed"
        assert closed_task.resolution == "handled"
        assert closed_task.closed_at is not None

    async def test_close_again_returns_none(self, store: TaskStore) -> None:
        """Test that closing an already-closed task returns None."""
        task = await store.create("primary", "reminder_pending", "Task")
        await store.close(task.id, "handled")

        second_close = await store.close(task.id, "again")

        assert second_close is None

    async def test_close_removes_from_list_open(self, store: TaskStore) -> None:
        """Test that a closed task no longer appears in list_open."""
        task = await store.create("primary", "reminder_pending", "Task")
        await store.close(task.id, "handled")

        open_tasks = await store.list_open("primary")

        assert len(open_tasks) == 0


class TestTypeCheck:
    """Test that invalid task types raise IntegrityError."""

    async def test_create_invalid_type_raises_integrity_error(
        self, store: TaskStore
    ) -> None:
        """Test that creating a task with an invalid type raises aiosqlite.IntegrityError."""
        with pytest.raises(aiosqlite.IntegrityError):
            await store.create("primary", "not_a_valid_type", "x")


class TestHistory:
    """Test history retrieval."""

    async def test_history_returns_closed_tasks_most_recent_first(
        self, store: TaskStore
    ) -> None:
        """Test that history returns closed tasks ordered by closed_at desc."""
        task1 = await store.create("primary", "reminder_pending", "First task")
        task2 = await store.create("primary", "reminder_pending", "Second task")

        await store.close(task1.id, "handled")
        await store.close(task2.id, "handled")

        history = await store.history("primary")

        assert len(history) == 2
        assert history[0].id == task2.id
        assert history[1].id == task1.id
        assert history[0].status == "closed"
        assert history[1].status == "closed"

    async def test_history_excludes_open_tasks(self, store: TaskStore) -> None:
        """Test that open tasks are not included in history."""
        await store.create("primary", "reminder_pending", "Open task")
        task2 = await store.create("primary", "reminder_pending", "Closed task")

        await store.close(task2.id, "handled")

        history = await store.history("primary")

        assert len(history) == 1
        assert history[0].id == task2.id


# ---- recurring checks for monitoring tasks --------------------------------
#
# Before this, a `monitoring` task had no clock: it was reconsidered only when
# an unrelated event happened to wake the agent. The live trace measured one
# check in a nine-hour window against a promise of "every 30 minutes".

from datetime import timedelta  # noqa: E402

from ares.core.tasks.store import MIN_CHECK_INTERVAL_S, check_schedule  # noqa: E402


def test_check_schedule_is_empty_without_an_interval():
    """No interval must stay the default: unarmed tasks behave as before."""
    assert check_schedule(None) == {}
    assert check_schedule(0) == {}


def test_check_schedule_enforces_the_floor():
    """A check costs a full serialized agent cycle; 1-minute polls are refused."""
    s = check_schedule(60)
    assert s["check_interval_s"] == MIN_CHECK_INTERVAL_S
    s = check_schedule(1800)
    assert s["check_interval_s"] == 1800


async def test_armed_task_becomes_due_and_unarmed_never_does(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    now = datetime.now(timezone.utc)

    armed = await store.create(
        "primary", "monitoring", title="watch temps",
        data=check_schedule(1800, first_at=now - timedelta(seconds=1)),
    )
    await store.create("primary", "monitoring", title="passive note")

    due = await store.list_checks_due(now)
    assert [t.id for t in due] == [armed.id]
    await store.aclose()


async def test_future_check_is_not_yet_due(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    now = datetime.now(timezone.utc)
    await store.create(
        "primary", "monitoring", title="later",
        data=check_schedule(1800, first_at=now + timedelta(minutes=10)),
    )
    assert await store.list_checks_due(now) == []
    await store.aclose()


async def test_closed_task_stops_being_checked(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    now = datetime.now(timezone.utc)
    t = await store.create(
        "primary", "monitoring", title="watch",
        data=check_schedule(1800, first_at=now - timedelta(seconds=1)),
    )
    assert len(await store.list_checks_due(now)) == 1
    await store.close(t.id, "done")
    assert await store.list_checks_due(now) == []
    await store.aclose()


async def test_reminders_are_never_returned_as_checks(tmp_path):
    """reminder_pending fires once via list_due; it must not double up here."""
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    now = datetime.now(timezone.utc)
    await store.create(
        "primary", "reminder_pending", title="ping", due_at=now.isoformat(),
        data=check_schedule(1800, first_at=now - timedelta(seconds=1)),
    )
    assert await store.list_checks_due(now) == []
    await store.aclose()


async def test_unparseable_next_check_is_skipped_not_fatal(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    now = datetime.now(timezone.utc)
    await store.create(
        "primary", "monitoring", title="bad",
        data={"check_interval_s": 1800, "next_check_at": "not-a-time"},
    )
    assert await store.list_checks_due(now) == []
    await store.aclose()
