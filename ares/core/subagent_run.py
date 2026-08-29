"""Subagent execution: the toolset policy and the bounded loop (spec §20.2).

Split from `subagents.py` (§0 rule 10): this module holds what a run is allowed
to do and how it does it, while the manager holds lifecycle and bookkeeping.
"""
from __future__ import annotations

import json
import time
import typing
from datetime import datetime

from ares.core.prompt import build_subagent_prompt
from ares.core.tool import ToolContext, ToolResult
from ares.core.utils.logging import get_logger

if typing.TYPE_CHECKING:
    from ares.core.tasks.store import Task

log = get_logger(__name__)

# --- The toolset (spec §20.2) -----------------------------------------------
# A fixed code constant, exactly like RULES: never sourced from or overridable
# by config. This is an ALLOWLIST on purpose. A deny-list silently grants every
# tool added to ARES later; the failure mode of an allowlist is a subagent that
# cannot do something, while the failure mode of a deny-list is an unattended
# loop that can place a phone call.
#
# `run_shell` is deliberately absent: a subagent runs unattended with web pages
# as its main input, and shell execution plus untrusted fetched content in a loop
# nobody is watching is the combination §14 exists to prevent. The main agent
# keeps run_shell for supervised, in-conversation use.
SUBAGENT_ALLOWED_TOOLS = frozenset(
    {
        "search_tools",
        "memory_grep",
        "memory_read",
        "memory_list",
        "get_home_state",
        "list_devices",
        "get_active_tasks",
        "get_task_history",
        "get_weather",
        "get_calendar",
        "read_source",
        "get_privilege_requests",
        "get_pr_status",
        "fetch_page",
        "report_progress",
    }
)

# Named here so the security test can assert they are absent, and so the reason
# is recorded next to the list rather than in a commit message.
SUBAGENT_FORBIDDEN_TOOLS = frozenset(
    {
        "speak",
        "send_notification",
        "place_call",
        "end_call",
        "send_sip_message",
        "control_device",
        "camera_snapshot",
        "memory_write",
        "memory_delete",
        "create_task",
        "close_task",
        "update_task",
        "request_privilege",
        "open_pr",
        "read_source_write",
        "propose_change",
        "run_shell",
        "spawn_subagent",
        "cancel_subagent",
        # A run has no business inspecting the subagent system: another run's
        # report is untrusted web content, and reading it would let one poisoned
        # page reach a second unattended loop.
        "list_subagents",
        "get_subagent_result",
    }
)


class ReportProgress:
    """Subagent-only tool: record a one-line progress note.

    Never registered on the main ToolRegistry — it is constructed per run and
    injected into that run's toolset, so the main agent cannot call it.
    """

    name = "report_progress"
    description = (
        "Record a short progress note for the person watching this background "
        "run. Use it when you finish a meaningful step, not for every tool call."
    )
    keywords = ("progress", "status", "update")
    parameters = {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
    }
    core = True

    def __init__(self, manager, run_id: str) -> None:
        """Bind the tool to one run."""
        self._manager = manager
        self._run_id = run_id

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Append a progress note and emit a LOW event."""
        note = str(kwargs.get("note") or "").strip()
        if not note:
            return ToolResult(False, "error: empty note")
        await self._manager.record_progress(self._run_id, note)
        return ToolResult(True, "Noted.")


def build_toolset(manager, run_id: str) -> list:
    """The run's toolset: the fixed allowlist, plus its own progress tool."""
    tools = [
        tool
        for name in sorted(SUBAGENT_ALLOWED_TOOLS)
        if (tool := manager.registry.get(name)) is not None
    ]
    tools.append(ReportProgress(manager, run_id))
    return tools


async def run_loop(manager, task: Task) -> str:
    """The bounded tool loop for one run. Returns the final report text."""
    block = task.data.get("subagent") or {}
    objective = block.get("objective", task.detail)

    by_name = {t.name: t for t in build_toolset(manager, task.id)}
    messages = [
        build_subagent_prompt(objective, datetime.now().astimezone()),
        {"role": "user", "content": f"Objective: {objective}"},
    ]

    started = time.monotonic()
    for iteration in range(manager.max_iterations):
        reply = await manager.llm.chat(
            messages, manager.registry.to_oai_schema(list(by_name.values()))
        )
        calls = reply.get("tool_calls")
        if not calls:
            await manager.update_block(task.id, iterations=iteration)
            return reply.get("content") or "(no report produced)"

        messages.append(reply)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except Exception:
                args = None

            if args is None:
                result = ToolResult(False, "error: could not parse arguments")
            elif name not in by_name:
                # The allowlist IS the boundary. Naming a tool outside it gets
                # you told it does not exist here, never handed the tool.
                result = ToolResult(
                    False,
                    f"error: '{name}' is not available to a background run. You "
                    "cannot speak, notify, control the home, write memory, run "
                    "shell commands or escalate. Gather what you need with the "
                    "tools you have and write your report.",
                )
            else:
                ctx = ToolContext(
                    user_id=task.user_id,
                    event=None,
                    session=None,
                    router=None,
                    memory=manager.memory,
                    tasks=manager.tasks,
                    registry=manager.registry,
                    services=manager.services,
                )
                try:
                    result = await by_name[name].run(ctx, **args)
                except Exception as e:
                    result = ToolResult(False, f"error: {e}")

            if name == "search_tools" and args is not None:
                # Discovery cannot widen the boundary.
                for found in manager.registry.search(args.get("query", "")):
                    if found.name in SUBAGENT_ALLOWED_TOOLS:
                        by_name.setdefault(found.name, found)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result.content[: manager.max_result_chars],
                }
            )
        await manager.update_block(task.id, iterations=iteration + 1)

    elapsed = round(time.monotonic() - started, 1)
    messages.append(
        {
            "role": "user",
            "content": (
                f"Iteration budget ({manager.max_iterations}) exhausted after "
                f"{elapsed}s. Write your report now, without tools."
            ),
        }
    )
    final = await manager.llm.chat(messages, tools=None)
    return final.get("content") or "(budget exhausted with no report)"
