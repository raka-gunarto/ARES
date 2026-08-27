from __future__ import annotations

from ares.core.tasks.store import check_schedule
from ares.core.tool import BaseTool, ToolContext, ToolResult


class Speak(BaseTool):
    """Speak to the user via the active channel."""

    name = "speak"
    description = "Send a message to the user through their active channel (chat, voice, etc)."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"}
        },
        "required": ["message"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Deliver a message to the user."""
        message = kwargs["message"]
        await ctx.router.speak(ctx.user_id, message)
        return ToolResult(ok=True, content="Delivered.")


class SendNotification(BaseTool):
    """Send a notification to the user."""

    name = "send_notification"
    description = "Send a notification message to the user (often asynchronous, outside the current conversation)."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string"}
        },
        "required": ["message"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Send a notification to the user."""
        message = kwargs["message"]
        await ctx.router.notify(ctx.user_id, message)
        return ToolResult(ok=True, content="Sent.")


class SearchTools(BaseTool):
    """Search for available tools by keyword."""

    name = "search_tools"
    description = "Search for tools by name or keyword to extend your capabilities."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"}
        },
        "required": ["query"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Search for tools matching the query."""
        query = kwargs["query"]
        matches = ctx.registry.search(query)
        if matches:
            content = "\n".join(f"{t.name} — {t.description}" for t in matches)
        else:
            content = "No tools matched. Try different words."
        return ToolResult(ok=True, content=content)


class GetActiveTasks(BaseTool):
    """List the user's open tasks."""

    name = "get_active_tasks"
    description = "Retrieve the user's currently open tasks with their ids, types, titles, and due dates."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {}
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Get open tasks for the user."""
        tasks = await ctx.tasks.list_open(ctx.user_id)
        if tasks:
            lines = []
            for t in tasks:
                line = f"[{t.type}] {t.title} (id={t.id}, due={t.due_at})"
                lines.append(line)
            content = "\n".join(lines)
        else:
            content = "No open tasks."
        return ToolResult(ok=True, content=content)


class CreateTask(BaseTool):
    """Create a new task for follow-up."""

    name = "create_task"
    description = (
        "Create a task to track something you are waiting on or need to remember. "
        "For anything time-based — a reminder, an alarm, or a 'remind me / call me at "
        "<time>' request — use type 'reminder_pending' with a due_at; it fires then."
    )
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["awaiting_response", "monitoring", "reminder_pending", "multi_step", "deferred"],
                "description": (
                    "Task kind. 'reminder_pending' = fires at due_at (use for any "
                    "time-based reminder/alarm/scheduled call). 'awaiting_response' = "
                    "waiting on a person. 'monitoring' = watching a condition. "
                    "'multi_step' = an ongoing multi-step job. 'deferred' = do later."
                ),
            },
            "title": {"type": "string", "description": "Short summary of the task."},
            "detail": {
                "type": "string",
                "description": (
                    "What to do when it fires, including how to reach the person if "
                    "relevant, e.g. 'call the user to remind them to head home'."
                ),
            },
            "due_at": {
                "type": "string",
                "description": (
                    "Absolute due time as an ISO-8601 UTC timestamp, e.g. "
                    "'2026-07-14T10:30:00Z'. REQUIRED for 'reminder_pending' — without it "
                    "the reminder never fires. Convert the person's local time to UTC "
                    "using the 'UTC now' value shown in your context."
                ),
            },
            "trigger": {
                "type": "string",
                "description": (
                    "For condition-based tasks (monitoring/awaiting_response): a short "
                    "description of the event that resolves it. Omit for plain reminders."
                ),
            },
            "check_every_minutes": {
                "type": "integer",
                "description": (
                    "Re-check this task on a timer, every N minutes (minimum 5). "
                    "REQUIRED if you intend to watch something on your own — without "
                    "it a monitoring task is only a passive note you happen to "
                    "reconsider when some unrelated event wakes you, which may be "
                    "hours or never. Set it whenever you tell someone you will keep "
                    "checking. Combine with due_at to stop checking at a given time. "
                    "Ignored for reminder_pending, which fires once at due_at."
                ),
            },
        },
        "required": ["type", "title"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Create a new task."""
        due_at = kwargs.get("due_at")
        # A reminder with no due time silently never fires — reject it so the model
        # gets immediate feedback and retries with a computed UTC timestamp.
        if kwargs["type"] == "reminder_pending" and not due_at:
            return ToolResult(
                ok=False,
                content=(
                    "reminder_pending requires due_at (ISO-8601 UTC, e.g. "
                    "2026-07-14T10:30:00Z). Compute it from the 'UTC now' in your "
                    "context and call create_task again."
                ),
            )
        # A reminder fires once at due_at; a recurring check would double up.
        interval_min = kwargs.get("check_every_minutes")
        schedule = {}
        if interval_min and kwargs["type"] != "reminder_pending":
            schedule = check_schedule(int(interval_min) * 60)

        task = await ctx.tasks.create(
            ctx.user_id,
            type=kwargs["type"],
            title=kwargs["title"],
            detail=kwargs.get("detail", ""),
            due_at=due_at,
            trigger=kwargs.get("trigger"),
            data=schedule or None,
        )
        when = f" (due {due_at})" if due_at else ""
        if schedule:
            every = schedule["check_interval_s"] // 60
            until = f", stopping at {due_at}" if due_at else ""
            return ToolResult(
                ok=True,
                content=(
                    f"Task {task.id} created; I will re-check it every {every} "
                    f"minutes{until}."
                ),
            )
        note = ""
        if kwargs["type"] == "monitoring":
            note = (
                " NOTE: no timer set — this is a passive note, not an active watch. "
                "Do not tell anyone you will keep checking. Pass check_every_minutes "
                "if you meant to watch it."
            )
        return ToolResult(ok=True, content=f"Task {task.id} created{when}.{note}")


class CloseTask(BaseTool):
    """Close a task that is no longer needed."""

    name = "close_task"
    description = "Mark a task as complete or resolved."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "resolution": {"type": "string"}
        },
        "required": ["task_id", "resolution"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Close a task."""
        task = await ctx.tasks.close(kwargs["task_id"], kwargs["resolution"])
        if task is not None:
            return ToolResult(ok=True, content="Task closed.")
        else:
            return ToolResult(ok=False, content="No such open task.")


CORE_TOOLS: list[BaseTool] = [
    Speak(),
    SendNotification(),
    SearchTools(),
    GetActiveTasks(),
    CreateTask(),
    CloseTask()
]
