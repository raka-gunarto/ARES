"""Background subagents (spec §20).

Covers the three things that make this feature safe rather than merely useful:
the toolset boundary, the bounds on a run, and the fact that a run can only
report back as DATA on the bus rather than acting on anyone's behalf.
"""
from __future__ import annotations

import asyncio
import json

from ares.core.event import EventBus, Priority
from ares.core.subagent_run import (
    SUBAGENT_ALLOWED_TOOLS,
    SUBAGENT_FORBIDDEN_TOOLS,
    ReportProgress,
)
from ares.core.subagents import SubagentManager
from ares.core.tasks.store import TaskStore
from ares.core.tool import BaseTool, ToolRegistry, ToolResult


class _FakeLLM:
    """Scripted LLM; records the tool schemas each call was offered."""

    def __init__(self, script):
        self.script = list(script)
        self.offered = []

    async def chat(self, messages, tools=None, temperature=0.7):
        self.offered.append([t["function"]["name"] for t in (tools or [])])
        self.messages = messages
        return self.script.pop(0) if self.script else {"role": "assistant", "content": "done"}


class _Tool(BaseTool):
    def __init__(self, name, core=False, content="ok"):
        self.name = name
        self.description = f"{name} tool"
        self.keywords = (name,)
        self.parameters = {"type": "object", "properties": {}}
        self.core = core
        self._content = content
        self.calls = 0

    async def run(self, ctx, **kwargs):
        self.calls += 1
        return ToolResult(True, self._content)


def _call(name, args=None):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ],
    }


async def _mgr(tmp_path, llm, **kw):
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    registry = ToolRegistry()
    for name in ("fetch_page", "memory_grep", "search_tools", "speak", "run_shell"):
        registry.register(_Tool(name))
    bus = EventBus()
    mgr = SubagentManager(
        bus=bus, llm=llm, registry=registry, tasks=store, memory=None,
        services={}, **kw,
    )
    return mgr, store, bus, registry


async def _drain(mgr, run_id, timeout=5.0):
    """Wait for a spawned run's asyncio task to settle, leaving no orphan."""
    handle = mgr._running.get(run_id)
    if handle is None:
        return
    try:
        await asyncio.wait_for(handle, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        handle.cancel()
        await asyncio.gather(handle, return_exceptions=True)


# ---- the toolset boundary (§20.2) ------------------------------------------

def test_allowlist_and_denylist_do_not_overlap():
    assert not (SUBAGENT_ALLOWED_TOOLS & SUBAGENT_FORBIDDEN_TOOLS)


def test_no_tool_that_speaks_or_acts_is_allowed():
    """The whole safety argument rests on this list."""
    for forbidden in (
        "speak",
        "send_notification",
        "place_call",
        "end_call",
        "send_sip_message",
        "control_device",
        "memory_write",
        "memory_delete",
        "request_privilege",
        "open_pr",
        "run_shell",
        "spawn_subagent",
    ):
        assert forbidden not in SUBAGENT_ALLOWED_TOOLS, forbidden


def test_a_subagent_cannot_spawn_subagents():
    """No recursion means no unbounded fan-out to reason about."""
    assert "spawn_subagent" not in SUBAGENT_ALLOWED_TOOLS
    assert "cancel_subagent" not in SUBAGENT_ALLOWED_TOOLS


async def test_only_allowlisted_tools_are_offered_to_the_model(tmp_path):
    llm = _FakeLLM([{"role": "assistant", "content": "report"}])
    mgr, store, _, _ = await _mgr(tmp_path, llm)
    task, _ = await mgr.spawn("primary", "find something out")
    await _drain(mgr, task.id)

    offered = set(llm.offered[0])
    assert "fetch_page" in offered
    assert "report_progress" in offered
    assert "speak" not in offered, "a run must never be handed a way to talk"
    assert "run_shell" not in offered
    await store.aclose()


async def test_calling_a_forbidden_tool_is_refused_not_executed(tmp_path):
    llm = _FakeLLM([_call("speak", {"message": "hi"}), {"role": "assistant", "content": "r"}])
    mgr, store, _, registry = await _mgr(tmp_path, llm)
    task, _ = await mgr.spawn("primary", "try to talk")
    await _drain(mgr, task.id)

    assert registry.get("speak").calls == 0, "the forbidden tool must not run"
    await store.aclose()


async def test_search_tools_cannot_widen_the_allowlist(tmp_path):
    """Discovery is the obvious escape hatch; it must not be one."""
    llm = _FakeLLM(
        [_call("search_tools", {"query": "run_shell shell"}),
         {"role": "assistant", "content": "r"}]
    )
    mgr, store, _, _ = await _mgr(tmp_path, llm)
    task, _ = await mgr.spawn("primary", "look for a shell")
    await _drain(mgr, task.id)

    assert "run_shell" not in set(llm.offered[-1])
    await store.aclose()


# ---- durability and bounds (§20.1, §20.4) ----------------------------------

async def test_a_run_is_a_task_row_and_closes_when_done(tmp_path):
    llm = _FakeLLM([{"role": "assistant", "content": "the report"}])
    mgr, store, _, _ = await _mgr(tmp_path, llm)
    task, err = await mgr.spawn("primary", "research jakarta", title="Jakarta")
    assert err == ""
    assert task.type == "multi_step"

    await _drain(mgr, task.id)
    after = await store.update(task.id)
    assert after.status == "closed"
    assert after.data["subagent"]["status"] == "done"
    assert after.data["subagent"]["result"] == "the report"
    await store.aclose()


async def test_max_concurrent_is_refused_not_queued(tmp_path):
    """A silent wait is indistinguishable from a hang."""
    blocker = asyncio.Event()

    class _Slow(_FakeLLM):
        async def chat(self, messages, tools=None, temperature=0.7):
            await blocker.wait()
            return {"role": "assistant", "content": "done"}

    mgr, store, _, _ = await _mgr(tmp_path, _Slow([]), max_concurrent=1)
    first, _ = await mgr.spawn("primary", "one")
    second, err = await mgr.spawn("primary", "two")

    assert second is None
    assert "already going" in err
    blocker.set()
    await _drain(mgr, first.id)
    await store.aclose()


async def test_iteration_budget_forces_a_final_report(tmp_path):
    llm = _FakeLLM([_call("fetch_page") for _ in range(3)] + [{"role": "assistant", "content": "forced"}])
    mgr, store, _, _ = await _mgr(tmp_path, llm, max_iterations=3)
    task, _ = await mgr.spawn("primary", "loop forever")
    await _drain(mgr, task.id)

    after = await store.update(task.id)
    assert after.data["subagent"]["status"] == "done"
    assert after.data["subagent"]["result"] == "forced"
    assert llm.offered[-1] == [], "the final call must offer no tools"
    await store.aclose()


async def test_timeout_is_recorded_as_a_terminal_status(tmp_path):
    class _Hang(_FakeLLM):
        async def chat(self, messages, tools=None, temperature=0.7):
            await asyncio.sleep(30)

    mgr, store, _, _ = await _mgr(tmp_path, _Hang([]))
    task, _ = await mgr.spawn("primary", "never finish", timeout_s=1)
    await _drain(mgr, task.id, timeout=5)

    after = await store.update(task.id)
    assert after.data["subagent"]["status"] == "timeout"
    assert after.status == "closed"
    await store.aclose()


async def test_a_crashing_run_fails_cleanly(tmp_path):
    """Spec §0 rule 12: never crash the daemon."""
    class _Boom(_FakeLLM):
        async def chat(self, messages, tools=None, temperature=0.7):
            raise RuntimeError("provider exploded")

    mgr, store, _, _ = await _mgr(tmp_path, _Boom([]))
    task, _ = await mgr.spawn("primary", "explode")
    await _drain(mgr, task.id)

    after = await store.update(task.id)
    assert after.data["subagent"]["status"] == "failed"
    assert "provider exploded" in after.data["subagent"]["result"]
    await store.aclose()


async def test_orphans_from_a_dead_process_are_recovered(tmp_path):
    """A restart must never leave a row claiming to be in flight."""
    store = TaskStore(tmp_path / "t.db")
    await store.init()
    stale = await store.create(
        "primary", "multi_step", title="was running",
        data={"subagent": {"status": "running", "objective": "x", "progress": []}},
    )
    mgr = SubagentManager(
        bus=EventBus(), llm=_FakeLLM([]), registry=ToolRegistry(),
        tasks=store, memory=None, services={},
    )
    recovered = await mgr.recover_orphans()

    assert recovered == [stale.id]
    after = await store.update(stale.id)
    assert after.data["subagent"]["status"] == "interrupted"
    assert after.status == "closed"
    await store.aclose()


# ---- reporting back on the bus (§20.3) -------------------------------------

async def test_completion_publishes_a_normal_event(tmp_path):
    llm = _FakeLLM([{"role": "assistant", "content": "found it"}])
    mgr, store, bus, _ = await _mgr(tmp_path, llm)
    task, _ = await mgr.spawn("primary", "research", title="Research")
    await _drain(mgr, task.id)

    ev = await bus.get()
    assert ev.source == "subagents"
    assert ev.type == "subagent_done"
    assert ev.priority == Priority.NORMAL, "a finished run must reach the agent"
    assert ev.payload["result"] == "found it"
    assert ev.payload["run_id"] == task.id
    await store.aclose()


async def test_progress_is_low_priority_so_it_never_backs_up(tmp_path):
    llm = _FakeLLM(
        [_call("report_progress", {"note": "read 3 sources"}),
         {"role": "assistant", "content": "r"}]
    )
    mgr, store, bus, _ = await _mgr(tmp_path, llm)
    task, _ = await mgr.spawn("primary", "research")
    await _drain(mgr, task.id)

    events = []
    while bus.qsize():
        events.append(await bus.get())
    progress = [e for e in events if e.type == "subagent_progress"]
    assert len(progress) == 1
    assert progress[0].priority == Priority.LOW
    assert progress[0].payload["note"] == "read 3 sources"

    after = await store.update(task.id)
    assert after.data["subagent"]["progress"] == ["read 3 sources"]
    await store.aclose()


async def test_subagent_events_never_change_the_active_channel():
    """A completion must not hijack the channel the user is actually on."""
    from ares.core.dispatcher import _ROOM_ONLY_SOURCES, _channel_for
    from ares.core.event import Event
    from ares.core.utils.ids import new_id

    assert "subagents" in _ROOM_ONLY_SOURCES
    ev = Event(
        id=new_id(), source="subagents", type="subagent_done",
        payload={}, priority=Priority.NORMAL,
    )
    assert _channel_for(ev) is None


def test_a_subagent_report_is_not_a_user_turn():
    """It must be fenced as [EVENT ...] so RULES treats it as DATA."""
    from ares.core.agent import USER_INITIATED_TYPES

    assert "subagent_done" not in USER_INITIATED_TYPES
    assert "subagent_progress" not in USER_INITIATED_TYPES


# ---- cancellation ----------------------------------------------------------

async def test_cancel_stops_a_run_and_records_it(tmp_path):
    blocker = asyncio.Event()

    class _Slow(_FakeLLM):
        async def chat(self, messages, tools=None, temperature=0.7):
            await blocker.wait()
            return {"role": "assistant", "content": "never"}

    mgr, store, _, _ = await _mgr(tmp_path, _Slow([]))
    task, _ = await mgr.spawn("primary", "long one")
    msg = await mgr.cancel(task.id)
    assert "Cancelled" in msg

    handle = mgr._running.get(task.id)
    if handle:
        await asyncio.gather(handle, return_exceptions=True)
    after = await store.update(task.id)
    assert after.data["subagent"]["status"] == "cancelled"
    await store.aclose()


async def test_cancelling_an_unknown_run_is_not_an_error(tmp_path):
    mgr, store, _, _ = await _mgr(tmp_path, _FakeLLM([]))
    assert "No running subagent" in await mgr.cancel("nope")
    await store.aclose()
