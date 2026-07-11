"""Filesystem-based memory storage backend."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ares.core.memory.base import BaseMemory
from ares.core.utils.logging import get_logger

logger = get_logger(__name__)


class FilesystemMemory(BaseMemory):
    """Memory storage backed by the filesystem."""

    def __init__(self, root: Path) -> None:
        """Initialize with a root directory for memory storage."""
        self.root = Path(root).resolve()

    def _safe_path(self, rel_path: str) -> Path | None:
        """
        Validate and resolve a relative path against root.

        Returns the resolved Path if valid, else None.
        Rejects: absolute paths, paths escaping root, non-.md files.
        """
        # Reject absolute paths
        if os.path.isabs(rel_path):
            return None

        # Resolve the path
        resolved = (self.root / rel_path).resolve()

        # Check if path escapes root
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None

        # Check for .md extension (case-insensitive)
        if not resolved.suffix.lower() == ".md":
            return None

        return resolved

    async def grep(self, pattern: str, scope: str = "all") -> str:
        """
        Search memory for pattern; scope: all|short|long.

        Returns matching lines capped at 4000 chars, or "No matches."
        """
        # Map scope to directory
        if scope == "short":
            directory = self.root / "short-term"
        elif scope == "long":
            directory = self.root / "long-term"
        else:
            # Default to all for any other value
            directory = self.root

        # Check if directory exists
        if not directory.exists():
            return "No matches."

        # Run grep with safe argument handling
        proc = await asyncio.create_subprocess_exec(
            "grep",
            "-r",
            "-i",
            "-n",
            "-C",
            "1",
            "-e",
            pattern,
            "--",
            str(directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        # Handle return codes
        if proc.returncode == 1:
            # No matches
            return "No matches."
        elif proc.returncode > 1:
            # Error occurred
            logger.debug(f"grep error (code {proc.returncode}): {stderr.decode(errors='replace')}")
            return "No matches."

        # Decode stdout
        result = stdout.decode(errors="replace")

        # Cap at 4000 characters
        if len(result) > 4000:
            result = result[:4000] + "\n...truncated"

        return result

    async def read(self, rel_path: str) -> str:
        """Read a file from memory; return content or error string."""
        path = self._safe_path(rel_path)
        if path is None:
            return f"error: invalid path '{rel_path}' (must be a relative .md path within memory)"

        if not path.exists():
            return f"error: '{rel_path}' not found"

        def _read_file() -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        return await asyncio.to_thread(_read_file)

    async def write(
        self, rel_path: str, content: str, mode: str = "append"
    ) -> str:
        """Write content to file; mode: append|overwrite; return status or error."""
        path = self._safe_path(rel_path)
        if path is None:
            return f"error: invalid path '{rel_path}' (must be a relative .md path within memory)"

        def _write_file() -> None:
            # Create parent directories
            path.parent.mkdir(parents=True, exist_ok=True)

            if mode == "overwrite":
                # Write, replacing the file
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            else:
                # Append mode (default): add leading newline then content
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n" + content)

        await asyncio.to_thread(_write_file)
        return f"Written to {rel_path}."

    async def list(self) -> str:
        """List all files in memory with sizes; return listing or error string."""

        def _list_files() -> str:
            lines = []
            for file_path in sorted(self.root.rglob("*.md")):
                if file_path.is_file():
                    rel = file_path.relative_to(self.root)
                    size = file_path.stat().st_size
                    lines.append(f"{rel} ({size} bytes)")

            if not lines:
                return "Memory is empty."

            return "\n".join(lines)

        return await asyncio.to_thread(_list_files)

    async def delete(self, rel_path: str) -> str:
        """Delete a file from memory; return status or error string."""
        path = self._safe_path(rel_path)
        if path is None:
            return f"error: invalid path '{rel_path}' (must be a relative .md path within memory)"

        if not path.exists():
            return f"error: '{rel_path}' not found"

        def _delete_file() -> None:
            path.unlink()

        await asyncio.to_thread(_delete_file)
        return f"Deleted {rel_path}."

    async def prune_short_term(self, retention_days: int) -> int:
        """Delete short-term files older than retention window; return count deleted."""
        short_term_dir = self.root / "short-term"

        if not short_term_dir.exists():
            return 0

        def _prune() -> int:
            now = datetime.now(timezone.utc).date()
            cutoff_date = now - timedelta(days=retention_days)
            deleted_count = 0

            for file_path in short_term_dir.glob("*.md"):
                if not file_path.is_file():
                    continue

                # Try to parse filename stem as YYYY-MM-DD
                stem = file_path.stem
                try:
                    file_date = datetime.strptime(stem, "%Y-%m-%d").date()
                except ValueError:
                    # Skip files whose stem is not a valid date
                    continue

                # Delete if strictly older than retention window
                if file_date < cutoff_date:
                    file_path.unlink()
                    deleted_count += 1

            return deleted_count

        return await asyncio.to_thread(_prune)
