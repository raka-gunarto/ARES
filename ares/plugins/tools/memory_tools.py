from __future__ import annotations

import typing

from ares.core.tool import BaseTool, ToolContext, ToolResult

if typing.TYPE_CHECKING:
    pass


class MemoryGrep(BaseTool):
    """Search memory for patterns and return matching entries."""

    name = "memory_grep"
    description = (
        "Search your memory files for patterns or keywords. "
        "Searches across all scopes (all, short-term, long-term) by default. "
        "Use this to recall what you know about a topic."
    )
    keywords = ("memory", "remember", "recall", "search", "grep", "know", "history")
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Text pattern to search for in memory files",
            },
            "scope": {
                "type": "string",
                "enum": ["all", "short", "long"],
                "default": "all",
                "description": "Search scope: 'all' (both), 'short' (short-term only), 'long' (long-term only)",
            },
        },
        "required": ["pattern"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute memory grep."""
        pattern = kwargs["pattern"]
        scope = kwargs.get("scope", "all")
        content = await ctx.memory.grep(pattern, scope)
        return ToolResult(ok=True, content=content)


class MemoryRead(BaseTool):
    """Read a file from memory."""

    name = "memory_read"
    description = (
        "Read a file from your memory system. "
        "Start by reading INDEX.md to see what files you have stored."
    )
    keywords = ("memory", "read", "file", "notes")
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to memory file to read (e.g., 'INDEX.md', 'short-term/2026-07-11.md')",
            },
        },
        "required": ["path"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute memory read."""
        path = kwargs["path"]
        content = await ctx.memory.read(path)
        return ToolResult(ok=True, content=content)


class MemoryWrite(BaseTool):
    """Write or append to a file in memory."""

    name = "memory_write"
    description = (
        "Write or append to your memory files. "
        "Tag entries with '<!-- tags: ... -->' for organization. "
        "Keep INDEX.md updated after structural changes. "
        "Put daily logs in short-term/YYYY-MM-DD.md."
    )
    keywords = ("memory", "write", "save", "remember", "note", "learn")
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to memory file to write (e.g., 'short-term/2026-07-11.md', 'long-term/preferences.md')",
            },
            "content": {
                "type": "string",
                "description": "Content to write or append",
            },
            "mode": {
                "type": "string",
                "enum": ["append", "overwrite"],
                "default": "append",
                "description": "Whether to append to or overwrite the file",
            },
        },
        "required": ["path", "content"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute memory write."""
        path = kwargs["path"]
        content = kwargs["content"]
        mode = kwargs.get("mode", "append")
        result = await ctx.memory.write(path, content, mode)
        return ToolResult(ok=True, content=result)


class MemoryList(BaseTool):
    """List all memory files."""

    name = "memory_list"
    description = "List all files in your memory system."
    keywords = ("memory", "list", "files", "index")
    parameters = {
        "type": "object",
        "properties": {},
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute memory list."""
        content = await ctx.memory.list()
        return ToolResult(ok=True, content=content)


class MemoryDelete(BaseTool):
    """Delete a file from memory."""

    name = "memory_delete"
    description = "Delete a file from your memory system. Use with caution."
    keywords = ("memory", "delete", "remove", "forget", "prune")
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to memory file to delete",
            },
        },
        "required": ["path"],
    }
    core = False

    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute memory delete."""
        path = kwargs["path"]
        content = await ctx.memory.delete(path)
        return ToolResult(ok=True, content=content)


MEMORY_TOOLS: list[BaseTool] = [
    MemoryGrep(),
    MemoryRead(),
    MemoryWrite(),
    MemoryList(),
    MemoryDelete(),
]
