from __future__ import annotations

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
    description = "Create a task to track something you are waiting on or need to remember."
    keywords = ()
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["awaiting_response", "monitoring", "reminder_pending", "multi_step", "deferred"]
            },
            "title": {"type": "string"},
            "detail": {"type": "string"},
            "due_at": {"type": "string"},
            "trigger": {"type": "string"}
        },
        "required": ["type", "title"]
    }
    core = True

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Create a new task."""
        task = await ctx.tasks.create(
            ctx.user_id,
            type=kwargs["type"],
            title=kwargs["title"],
            detail=kwargs.get("detail", ""),
            due_at=kwargs.get("due_at"),
            trigger=kwargs.get("trigger")
        )
        return ToolResult(ok=True, content=f"Task {task.id} created.")


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
