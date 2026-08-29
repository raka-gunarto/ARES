"""INDEX.md reconciliation (spec §4.12 / F5).

The live memory store shows the drift this fixes: INDEX.md described 2 of the 6
long-term files, so people.md, communication-preferences.md, tools-registry.md
and todoist-key.md were invisible to anything consulting the index first.
Housekeeping pruned short-term files every night and never touched the index.
"""
from __future__ import annotations

from pathlib import Path

from ares.core.memory.filesystem import FilesystemMemory


def _mem(tmp_path: Path) -> FilesystemMemory:
    (tmp_path / "long-term").mkdir(parents=True)
    return FilesystemMemory(tmp_path)


async def test_unindexed_files_are_added(tmp_path):
    mem = _mem(tmp_path)
    for name in ("people.md", "preferences.md", "tools-registry.md"):
        (tmp_path / "long-term" / name).write_text("x")
    (tmp_path / "INDEX.md").write_text(
        "# Memory Index\n\n- **long-term/preferences.md** — Household preferences.\n"
    )

    result = await mem.reconcile_index()

    assert sorted(result["added"]) == ["long-term/people.md", "long-term/tools-registry.md"]
    index = (tmp_path / "INDEX.md").read_text()
    assert "long-term/people.md" in index
    assert "long-term/tools-registry.md" in index


async def test_the_models_own_descriptions_are_preserved(tmp_path):
    mem = _mem(tmp_path)
    (tmp_path / "long-term" / "preferences.md").write_text("x")
    (tmp_path / "long-term" / "people.md").write_text("x")
    (tmp_path / "INDEX.md").write_text(
        "- **long-term/preferences.md** — Household preferences and settings.\n"
    )

    await mem.reconcile_index()

    index = (tmp_path / "INDEX.md").read_text()
    assert "Household preferences and settings." in index, "must not rewrite the model's text"


async def test_already_complete_index_is_untouched(tmp_path):
    mem = _mem(tmp_path)
    (tmp_path / "long-term" / "people.md").write_text("x")
    original = "- **long-term/people.md** — Who lives here.\n"
    (tmp_path / "INDEX.md").write_text(original)

    result = await mem.reconcile_index()

    assert result["added"] == []
    assert (tmp_path / "INDEX.md").read_text() == original


async def test_stale_entries_are_reported_but_never_deleted(tmp_path):
    """Removing a line the model wrote is destructive; report it instead."""
    mem = _mem(tmp_path)
    (tmp_path / "long-term" / "people.md").write_text("x")
    (tmp_path / "INDEX.md").write_text(
        "- **long-term/people.md** — Who lives here.\n"
        "- **long-term/deleted-note.md** — Gone.\n"
    )

    result = await mem.reconcile_index()

    assert result["stale"] == ["long-term/deleted-note.md"]
    assert "deleted-note.md" in (tmp_path / "INDEX.md").read_text()


async def test_missing_index_is_created(tmp_path):
    mem = _mem(tmp_path)
    (tmp_path / "long-term" / "people.md").write_text("x")
    result = await mem.reconcile_index()
    assert result["added"] == ["long-term/people.md"]
    assert (tmp_path / "INDEX.md").exists()


async def test_no_long_term_directory_is_not_an_error(tmp_path):
    mem = FilesystemMemory(tmp_path)
    assert await mem.reconcile_index() == {"added": [], "stale": []}


# ---- the housekeeping hook -------------------------------------------------

async def test_housekeeping_reports_index_changes(tmp_path):
    from ares.core.event import EventBus
    from ares.plugins.sources.scheduler import SchedulerSource

    mem = _mem(tmp_path)
    (tmp_path / "long-term" / "people.md").write_text("x")

    bus = EventBus()
    await SchedulerSource(bus, {}, None, mem, 14)._run_housekeeping()

    ev = await bus.get()
    assert ev.type == "housekeeping"
    assert ev.payload["pruned"] == 0
    assert ev.payload["index_added"] == ["long-term/people.md"]
    assert ev.payload["index_stale"] == []


async def test_reconciliation_failure_never_stops_housekeeping(tmp_path):
    """A broken index must not take the nightly prune down with it."""
    from ares.core.event import EventBus
    from ares.plugins.sources.scheduler import SchedulerSource

    class _Broken(FilesystemMemory):
        async def reconcile_index(self):
            raise RuntimeError("disk on fire")

    bus = EventBus()
    await SchedulerSource(bus, {}, None, _Broken(tmp_path), 14)._run_housekeeping()

    ev = await bus.get()
    assert ev.type == "housekeeping", "the prune and its event must still happen"
    assert ev.payload["index_added"] == []
