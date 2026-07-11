from __future__ import annotations

import typing

from ares.core.tool import BaseTool, ToolContext, ToolResult

if typing.TYPE_CHECKING:
    pass


class UpdateTask(BaseTool):
    """Update a task's fields."""

    name = "update_task"
    description = (
        "Update a task's title, detail, due date, trigger condition, or data. "
        "Provide the task ID and the fields to change. "
        "Use this to reschedule, edit details, or modify a task's trigger."
    )
    keywords = ("task", "update", "change", "postpone", "edit")
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "ID of the task to update",
            },
            "title": {
                "type": "string",
                "description": "New title for the task",
            },
            "detail": {
                "type": "string",
                "description": "New detail text for the task",
            },
            "due_at": {
                "type": "string",
                "description": "New due date in ISO8601 format (UTC)",
            },
            "trigger": {
                "type": "string",
                "description": "New trigger condition description",
            },
            "data": {
                "type": "object",
                "description": "Additional structured data to store with the task",
            },
        },
        "required": ["task_id"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute task update."""
        task_id = kwargs["task_id"]

        # Collect only the optional fields that were provided
        fields: dict[str, object] = {}
        for key in ("title", "detail", "due_at", "trigger", "data"):
            if key in kwargs:
                fields[key] = kwargs[key]

        task = await ctx.tasks.update(task_id, **fields)

        if task is None:
            return ToolResult(ok=False, content="No such task.")

        return ToolResult(ok=True, content=f"Task {task.id} updated.")


class GetTaskHistory(BaseTool):
    """Retrieve closed tasks from history."""

    name = "get_task_history"
    description = (
        "Retrieve your closed tasks or task history. "
        "Shows recently completed or closed tasks by default. "
        "Use this to review what you've accomplished or check task resolutions."
    )
    keywords = ("task", "history", "closed", "past", "previous")
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 20,
                "description": "Maximum number of tasks to retrieve (default 20)",
            },
        },
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute task history retrieval."""
        limit = kwargs.get("limit", 20)
        tasks = await ctx.tasks.history(ctx.user_id, limit)

        if not tasks:
            return ToolResult(ok=True, content="No task history.")

        lines = []
        for task in tasks:
            # Render: [type] title (id=..., resolution=...)
            line = f"[{task.type}] {task.title} (id={task.id}, resolution={task.resolution})"
            lines.append(line)

        content = "\n".join(lines)
        return ToolResult(ok=True, content=content)


TASK_TOOLS: list[BaseTool] = [
    UpdateTask(),
    GetTaskHistory(),
]
