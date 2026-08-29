"""Duplicate-task guard and check disarming on close.

The live task store holds 21 tasks, 5 of them duplicate pairs created minutes
apart — "Call Raka on Phone" twice, "Turn on cooling to 21C at 3pm" twice — so
two reminders fired for one intention.
"""
from __future__ import annotations

from ares.core.tasks.store import TaskStore, check_schedule
from ares.core.tool import ToolContext
from ares.plugins.tools.core_tools import CloseTask, CreateTask, _similarity


async def _store(tmp_path) -> TaskStore:
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    return store


def _ctx(store):
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=store, registry=None, services={},
    )


# ---- similarity scoring ----------------------------------------------------

def test_the_real_duplicate_pairs_score_above_the_threshold():
    """Every pair here is an actual duplicate from the live store."""
    pairs = [
        ("Call Raka on Phone", "Call Raka on Phone"),
        ("Call user at 11:30 — go home for lunch", "Call user — go home for lunch"),
        ("Turn on cooling to 21°C at 3pm", "Turn on cooling to 21°C at 3pm — both zones"),
        (
            "Monitor room temps & turn off cooling if all below 22",
            "Monitor temps every 30 min, turn off cooling if all below 22",
        ),
    ]
    for a, b in pairs:
        assert _similarity(a, b) >= 0.6, (a, b)


def test_unrelated_tasks_score_low():
    assert _similarity("Pack Nadia's perfume", "Wake-up call for Raka") < 0.3
    assert _similarity("Turn off cooling at 8am", "Check bedroom temp") < 0.3


# ---- the guard in create_task ----------------------------------------------

async def test_second_identical_task_is_refused(tmp_path):
    store = await _store(tmp_path)
    ok = await CreateTask().run(
        _ctx(store), type="awaiting_response", title="Call Raka on Phone"
    )
    assert ok.ok is True
    dup = await CreateTask().run(
        _ctx(store), type="awaiting_response", title="Call Raka on Phone"
    )
    assert dup.ok is False
    assert "already covers this" in dup.content
    assert len(await store.list_open("primary")) == 1
    await store.aclose()


async def test_near_duplicate_is_refused_and_names_the_existing_task(tmp_path):
    store = await _store(tmp_path)
    first = await CreateTask().run(
        _ctx(store), type="monitoring",
        title="Monitor room temps & turn off cooling if all below 22",
    )
    r = await CreateTask().run(
        _ctx(store), type="monitoring",
        title="Monitor temps every 30 min, turn off cooling if all below 22",
    )
    assert r.ok is False
    existing = (await store.list_open("primary"))[0]
    assert existing.id in r.content
    await store.aclose()


async def test_different_type_is_not_a_duplicate(tmp_path):
    store = await _store(tmp_path)
    await CreateTask().run(_ctx(store), type="monitoring", title="watch the temperature")
    r = await CreateTask().run(_ctx(store), type="deferred", title="watch the temperature")
    assert r.ok is True
    await store.aclose()


async def test_same_title_different_due_time_is_allowed(tmp_path):
    """The same errand tomorrow is a real second task."""
    store = await _store(tmp_path)
    await CreateTask().run(
        _ctx(store), type="reminder_pending", title="Turn on cooling",
        due_at="2026-08-29T15:00:00Z",
    )
    r = await CreateTask().run(
        _ctx(store), type="reminder_pending", title="Turn on cooling",
        due_at="2026-08-30T15:00:00Z",
    )
    assert r.ok is True
    assert len(await store.list_open("primary")) == 2
    await store.aclose()


async def test_force_overrides_the_guard(tmp_path):
    store = await _store(tmp_path)
    await CreateTask().run(_ctx(store), type="monitoring", title="watch the door")
    r = await CreateTask().run(
        _ctx(store), type="monitoring", title="watch the door", force=True
    )
    assert r.ok is True
    assert len(await store.list_open("primary")) == 2
    await store.aclose()


async def test_a_closed_task_does_not_block_a_new_one(tmp_path):
    store = await _store(tmp_path)
    await CreateTask().run(_ctx(store), type="monitoring", title="watch the door")
    task = (await store.list_open("primary"))[0]
    await store.close(task.id, "done")
    r = await CreateTask().run(_ctx(store), type="monitoring", title="watch the door")
    assert r.ok is True
    await store.aclose()


# ---- closing disarms the recurring check -----------------------------------

async def test_close_clears_the_check_schedule(tmp_path):
    """The live store has a closed task still carrying check_interval_s=600."""
    store = await _store(tmp_path)
    task = await store.create(
        "primary", "monitoring", title="watch", data=check_schedule(600)
    )
    assert task.data["check_interval_s"] == 600

    closed = await store.close(task.id, "done")
    assert "check_interval_s" not in closed.data
    assert "next_check_at" not in closed.data

    reread = await store.update(task.id)
    assert reread.data == {}
    await store.aclose()


async def test_close_preserves_other_data_fields(tmp_path):
    store = await _store(tmp_path)
    task = await store.create(
        "primary", "monitoring", title="watch",
        data={**check_schedule(600), "notes": "keep me"},
    )
    closed = await store.close(task.id, "done")
    assert closed.data == {"notes": "keep me"}
    await store.aclose()


async def test_close_tool_still_reports_success(tmp_path):
    store = await _store(tmp_path)
    task = await store.create("primary", "monitoring", title="watch", data=check_schedule(600))
    r = await CloseTask().run(_ctx(store), task_id=task.id, resolution="done")
    assert r.ok is True
    await store.aclose()
