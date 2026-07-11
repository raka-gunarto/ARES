"""Abstract base class for memory storage backends."""
from __future__ import annotations

import abc


class BaseMemory(abc.ABC):
    """Abstract base class for memory storage implementations."""

    @abc.abstractmethod
    async def grep(self, pattern: str, scope: str = "all") -> str:
        """Search memory for pattern; scope: all|short|long."""

    @abc.abstractmethod
    async def read(self, rel_path: str) -> str:
        """Read a file from memory; return content or error string."""

    @abc.abstractmethod
    async def write(
        self, rel_path: str, content: str, mode: str = "append"
    ) -> str:
        """Write content to file; mode: append|overwrite; return status or error."""

    @abc.abstractmethod
    async def list(self) -> str:
        """List all files in memory with sizes; return listing or error string."""

    @abc.abstractmethod
    async def delete(self, rel_path: str) -> str:
        """Delete a file from memory; return status or error string."""

    @abc.abstractmethod
    async def prune_short_term(self, retention_days: int) -> int:
        """Delete short-term files older than retention window; return count deleted."""
