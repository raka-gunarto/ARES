from __future__ import annotations

import abc
import dataclasses
import re
import typing

if typing.TYPE_CHECKING:
    from ares.core.memory.base import BaseMemory
    from ares.core.tasks.store import TaskStore

from ares.core.event import Event
from ares.core.session import Session
from ares.core.router import ResponseRouter


@dataclasses.dataclass
class ToolResult:
    """Result of a tool execution."""

    ok: bool
    content: str


class BaseTool(abc.ABC):
    """Abstract base class for all tools."""

    name: str
    description: str
    keywords: tuple[str, ...]
    parameters: dict
    core: bool = False

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute the tool with the given context and arguments."""
        pass


@dataclasses.dataclass
class ToolContext:
    """Context injected per tool call."""

    user_id: str
    event: Event
    session: Session
    router: ResponseRouter
    memory: BaseMemory
    tasks: TaskStore
    registry: ToolRegistry
    services: dict[str, object]


class ToolRegistry:
    """Registry for tools with search and activation."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool by name."""
        self._tools[tool.name] = tool

    def core_tools(self) -> list[BaseTool]:
        """Return all core tools."""
        return [t for t in self._tools.values() if t.core]

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name, or None if not found."""
        return self._tools.get(name)

    def search(self, query: str, limit: int = 5) -> list[BaseTool]:
        """
        Search for non-core tools by keyword and name.

        Scoring: score = |tokenize(query) ∩ (keywords ∪ tokenize(name))|
        Returns non-core tools with score >= 1, sorted by (-score, name),
        limited to top `limit` results.
        """
        results: list[tuple[int, str, BaseTool]] = []

        for tool in self._tools.values():
            if tool.core:
                continue

            query_tokens = self._tok(query)
            matchset = set(tool.keywords) | set(self._tok(tool.name))
            score = len(set(query_tokens) & matchset)

            if score >= 1:
                results.append((score, tool.name, tool))

        # Sort by score descending, then by name ascending
        results.sort(key=lambda x: (-x[0], x[1]))
        return [tool for _, _, tool in results[:limit]]

    def to_oai_schema(self, tools: list[BaseTool]) -> list[dict]:
        """Convert tools to OpenAI function schema."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _tok(s: str) -> list[str]:
        """Tokenize: lowercase, split on non-alphanumerics, drop tokens < 3 chars."""
        return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 3]
