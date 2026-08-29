"""Background subagents — bounded autonomous runs (spec §20).

The main agent is strictly serialized: every event for a user waits behind the
current one. Anything long-running (research across a dozen pages, watching a
story develop over an hour) therefore blocks the household's assistant for its
whole duration. A subagent moves that work off the critical path.

A subagent is NOT a second ARES. It cannot speak to anyone, cannot touch the
home, cannot write memory and cannot escalate — see SUBAGENT_ALLOWED_TOOLS. It
reads, it thinks, and it returns text, which comes back to the main agent as an
event payload and is treated as DATA like any other tool output.

Each run is a `multi_step` task row (§20.1), so durability, prompt visibility
and dashboard rendering all come from mechanisms that already exist.
"""
from __future__ import annotations

import asyncio
import typing
from datetime import datetime, timezone

from ares.core.event import Event, Priority
from ares.core.subagent_run import SUBAGENT_ALLOWED_TOOLS, run_loop
from ares.core.utils.ids import new_id
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.event import EventBus
    from ares.core.llm.client import LLMClient
    from ares.core.memory.base import BaseMemory
    from ares.core.tasks.store import Task, TaskStore
    from ares.core.tool import ToolRegistry

log = get_logger(__name__)

SUBAGENT_TASK_TYPE = "multi_step"

# Statuses a run can end in. `interrupted` is set on startup for rows left
# `running` by a process that died (§20.1).
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "timeout", "interrupted"})

DEFAULT_MAX_ITERATIONS = 25
DEFAULT_TIMEOUT_S = 900
MAX_TIMEOUT_S = 3600
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_MAX_RESULT_CHARS = 4000
_MAX_PROGRESS_NOTES = 20


class SubagentManager:
    """Owns background runs: spawn, track, cancel, report (spec §20)."""

    def __init__(
        self,
        bus: EventBus,
        llm: LLMClient,
        registry: ToolRegistry,
        tasks: TaskStore,
        memory: BaseMemory,
        services: dict[str, object],
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ) -> None:
        """Store collaborators and run bounds."""
        self.bus = bus
        self.llm = llm
        self.registry = registry
        self.tasks = tasks
        self.memory = memory
        self.services = services
        self.max_iterations = max_iterations
        self.timeout_s = min(int(timeout_s), MAX_TIMEOUT_S)
        self.max_concurrent = max_concurrent
        self.max_result_chars = max_result_chars
        self._running: dict[str, asyncio.Task] = {}

    # --- lifecycle ----------------------------------------------------------

    async def recover_orphans(self) -> list[str]:
        """Mark runs left `running` by a dead process as `interrupted` (§20.1).

        Without this a restart leaves rows that claim to be in flight and that
        nothing will ever finish — the same class of bug the privilege queue's
        `notified` column fixed.
        """
        recovered: list[str] = []
        for task in await self._all_open_runs():
            block = task.data.get("subagent") or {}
            if block.get("status") == "running":
                await self._finish(
                    task.id,
                    "interrupted",
                    result="The daemon restarted while this run was in flight.",
                    emit=False,
                )
                recovered.append(task.id)
        if recovered:
            log.warning("marked %d orphaned subagent run(s) interrupted", len(recovered))
        return recovered

    def running_count(self) -> int:
        """Number of runs currently executing in this process."""
        return sum(1 for t in self._running.values() if not t.done())

    async def spawn(
        self,
        user_id: str,
        objective: str,
        title: str = "",
        timeout_s: int | None = None,
    ) -> tuple[Task | None, str]:
        """Start a background run. Returns (task, error_message)."""
        objective = (objective or "").strip()
        if not objective:
            return None, "error: objective is required"

        if self.running_count() >= self.max_concurrent:
            # Refused, not queued: a silent wait looks identical to a hang.
            return None, (
                f"error: {self.max_concurrent} background runs are already going. "
                "Wait for one to finish or cancel one with cancel_subagent."
            )

        budget = min(int(timeout_s or self.timeout_s), MAX_TIMEOUT_S)
        now = datetime.now(timezone.utc).isoformat()
        task = await self.tasks.create(
            user_id,
            type=SUBAGENT_TASK_TYPE,
            title=(title or objective)[:120],
            detail=objective,
            data={
                "subagent": {
                    "status": "running",
                    "objective": objective,
                    "started_at": now,
                    "ended_at": None,
                    "progress": [],
                    "result": None,
                    "iterations": 0,
                    "timeout_s": budget,
                }
            },
        )
        self._running[task.id] = asyncio.create_task(self._guarded_run(task, budget))
        log.info("subagent %s spawned: %s", task.id[:8], task.title)
        return task, ""

    async def cancel(self, run_id: str) -> str:
        """Cancel a running subagent. Returns a status message.

        The bookkeeping happens HERE, not in the run's CancelledError handler:
        a coroutine that is being cancelled cannot reliably await anything more
        (the cancellation is redelivered at the next await), so a status written
        from inside the handler is lost and the row stays `running` forever.
        """
        handle = self._running.get(run_id)
        if handle is None or handle.done():
            return f"No running subagent with id {run_id}."
        handle.cancel()
        await self._finish(run_id, "cancelled", "Cancelled before finishing.")
        return f"Cancelled subagent {run_id}."

    async def shutdown(self) -> None:
        """Cancel every in-flight run (daemon shutdown)."""
        for handle in list(self._running.values()):
            handle.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    # --- state --------------------------------------------------------------

    async def _all_open_runs(self) -> list[Task]:
        """Open task rows that carry a subagent block."""
        rows = await self.tasks.list_open("primary")
        return [t for t in rows if isinstance(t.data.get("subagent"), dict)]

    async def list_runs(self, user_id: str = "primary") -> list[dict]:
        """Summaries of every open run, newest last."""
        out = []
        for task in await self.tasks.list_open(user_id):
            block = task.data.get("subagent")
            if not isinstance(block, dict):
                continue
            out.append(
                {
                    "run_id": task.id,
                    "title": task.title,
                    "status": block.get("status", "unknown"),
                    "started_at": block.get("started_at"),
                    "iterations": block.get("iterations", 0),
                    "last_progress": (block.get("progress") or [None])[-1],
                    "result": block.get("result"),
                }
            )
        return out

    async def update_block(self, run_id: str, **fields) -> None:
        """Merge fields into a run's `subagent` block."""
        task = await self.tasks.update(run_id)
        if task is None:
            return
        block = dict(task.data.get("subagent") or {})
        block.update(fields)
        await self.tasks.update(run_id, data={**task.data, "subagent": block})

    async def record_progress(self, run_id: str, note: str) -> None:
        """Append a progress note and emit a LOW event."""
        task = await self.tasks.update(run_id)
        if task is None:
            return
        block = dict(task.data.get("subagent") or {})
        notes = list(block.get("progress") or [])
        notes.append(note)
        block["progress"] = notes[-_MAX_PROGRESS_NOTES:]
        await self.tasks.update(run_id, data={**task.data, "subagent": block})
        await self._publish(
            "subagent_progress",
            Priority.LOW,
            task.user_id,
            {"run_id": run_id, "title": task.title, "note": note},
        )

    async def _finish(
        self, run_id: str, status: str, result: str, emit: bool = True
    ) -> None:
        """Record a terminal status, close the task, and announce it."""
        task = await self.tasks.update(run_id)
        if task is None:
            return
        block = dict(task.data.get("subagent") or {})
        started = block.get("started_at")
        block.update(
            {
                "status": status,
                "result": result[: self.max_result_chars],
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.tasks.update(run_id, data={**task.data, "subagent": block})
        await self.tasks.close(run_id, f"subagent {status}")

        if not emit:
            return
        duration = None
        try:
            if started:
                duration = round(
                    (
                        datetime.now(timezone.utc) - datetime.fromisoformat(started)
                    ).total_seconds(),
                    1,
                )
        except (TypeError, ValueError):
            duration = None
        await self._publish(
            "subagent_done",
            Priority.NORMAL,
            task.user_id,
            {
                "run_id": run_id,
                "title": task.title,
                "objective": block.get("objective"),
                "status": status,
                "result": block.get("result"),
                "iterations": block.get("iterations", 0),
                "duration_s": duration,
            },
        )

    async def _publish(
        self, type_: str, priority: Priority, user_id: str, payload: dict
    ) -> None:
        """Publish one subagent event on the bus."""
        await self.bus.publish(
            Event(
                id=new_id(),
                source="subagents",
                type=type_,
                payload=payload,
                priority=priority,
                user_id=user_id,
            )
        )

    # --- the run itself -----------------------------------------------------

    async def _guarded_run(self, task: Task, budget_s: int) -> None:
        """Run one subagent to completion, mapping every outcome to a status."""
        try:
            result = await asyncio.wait_for(run_loop(self, task), timeout=budget_s)
            await self._finish(task.id, "done", result)
        except asyncio.TimeoutError:
            log.warning("subagent %s timed out after %ss", task.id[:8], budget_s)
            await self._finish(
                task.id,
                "timeout",
                f"Ran out of time after {budget_s}s without reaching a conclusion.",
            )
        except asyncio.CancelledError:
            # cancel() has already recorded the status; a shutdown leaves the row
            # `running` on purpose, for recover_orphans() to mark `interrupted`.
            raise
        except Exception as e:  # spec §0 rule 12 — never crash the daemon
            log.exception("subagent %s failed", task.id[:8])
            await self._finish(task.id, "failed", f"{type(e).__name__}: {e}")
        finally:
            self._running.pop(task.id, None)
