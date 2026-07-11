"""Test cases for FilesystemMemory. See spec §4.12.

Covers: path escape rejection, non-.md rejection, append vs overwrite modes,
grep with truncation, and prune_short_term with date-based filtering.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ares.core.memory.filesystem import FilesystemMemory


class TestPathEscapeRejection:
    """Verify that escaping paths are rejected and no files are created outside tmp_path."""

    async def test_write_relative_escape_rejected(self, tmp_path: Path) -> None:
        """Test that write("../evil.md", "x") returns error and creates no file."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("../evil.md", "x")
        assert result.startswith("error:")
        # Verify no file was created outside tmp_path
        assert not (tmp_path.parent / "evil.md").exists()

    async def test_write_deep_escape_rejected(self, tmp_path: Path) -> None:
        """Test that write("../../etc/passwd.md", "x") returns error and creates no file."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("../../etc/passwd.md", "x")
        assert result.startswith("error:")
        # Verify no file was created outside tmp_path
        assert not (tmp_path.parent / "etc" / "passwd.md").exists()

    async def test_write_subdir_escape_rejected(self, tmp_path: Path) -> None:
        """Test that write("subdir/../../escape.md", "x") returns error and creates no file."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("subdir/../../escape.md", "x")
        assert result.startswith("error:")
        # Verify no file was created outside tmp_path
        assert not (tmp_path.parent / "escape.md").exists()

    async def test_write_absolute_path_rejected(self, tmp_path: Path) -> None:
        """Test that write("/tmp/abs.md", "x") returns error and creates no file."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("/tmp/abs.md", "x")
        assert result.startswith("error:")
        # Verify no file was created (it should NOT be in /tmp with our control)
        # We can't strictly verify /tmp/abs.md wasn't created, but the error should be present
        assert "error:" in result

    async def test_read_escape_rejected(self, tmp_path: Path) -> None:
        """Test that read("../evil.md") returns error string."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.read("../evil.md")
        assert result.startswith("error:")

    async def test_read_absolute_path_rejected(self, tmp_path: Path) -> None:
        """Test that read("/etc/passwd.md") returns error string."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.read("/etc/passwd.md")
        assert result.startswith("error:")


class TestNonMdRejection:
    """Verify that non-.md files are rejected."""

    async def test_write_non_md_rejected(self, tmp_path: Path) -> None:
        """Test that write("notes.txt", "x") returns error."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("notes.txt", "x")
        assert result.startswith("error:")
        # Verify no file was created
        assert not (tmp_path / "notes.txt").exists()

    async def test_read_non_md_rejected(self, tmp_path: Path) -> None:
        """Test that read("data.json") returns error."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.read("data.json")
        assert result.startswith("error:")

    async def test_md_extension_accepted(self, tmp_path: Path) -> None:
        """Test that .md files are accepted."""
        memory = FilesystemMemory(tmp_path)
        result = await memory.write("notes.md", "test content")
        assert "Written to" in result
        assert (tmp_path / "notes.md").exists()


class TestAppendVsOverwrite:
    """Verify append and overwrite modes work correctly."""

    async def test_append_mode_default(self, tmp_path: Path) -> None:
        """Test that write in append mode (default) concatenates with newline."""
        memory = FilesystemMemory(tmp_path)

        # Write initial content
        await memory.write("long-term/p.md", "alpha")
        content = await memory.read("long-term/p.md")
        assert "alpha" in content

        # Append (default mode)
        await memory.write("long-term/p.md", "beta")
        content = await memory.read("long-term/p.md")
        assert "alpha" in content
        assert "beta" in content

    async def test_overwrite_mode(self, tmp_path: Path) -> None:
        """Test that overwrite mode replaces the entire file."""
        memory = FilesystemMemory(tmp_path)

        # Write initial content
        await memory.write("long-term/p.md", "alpha")
        await memory.write("long-term/p.md", "beta")
        content = await memory.read("long-term/p.md")
        assert "alpha" in content
        assert "beta" in content

        # Overwrite
        await memory.write("long-term/p.md", "gamma", mode="overwrite")
        content = await memory.read("long-term/p.md")
        assert "gamma" in content
        assert "alpha" not in content
        assert "beta" not in content


class TestGrep:
    """Verify grep search functionality."""

    async def test_grep_finds_pattern(self, tmp_path: Path) -> None:
        """Test that grep finds a matching pattern."""
        memory = FilesystemMemory(tmp_path)
        await memory.write("notes.md", "the temperature is 20 degrees")
        result = await memory.grep("degrees")
        assert "degrees" in result

    async def test_grep_no_match(self, tmp_path: Path) -> None:
        """Test that grep returns "No matches." when pattern not found."""
        memory = FilesystemMemory(tmp_path)
        await memory.write("notes.md", "the temperature is 20 degrees")
        result = await memory.grep("zzznomatch")
        assert result == "No matches."

    async def test_grep_empty_directory(self, tmp_path: Path) -> None:
        """Test that grep on empty memory returns "No matches." """
        memory = FilesystemMemory(tmp_path)
        result = await memory.grep("anything")
        assert result == "No matches."


class TestGrepTruncation:
    """Verify grep truncation at 4000 characters."""

    async def test_grep_truncation_long_output(self, tmp_path: Path) -> None:
        """Test that grep output is truncated at 4000 chars with ...truncated marker."""
        memory = FilesystemMemory(tmp_path)

        # Create a file with many lines, each containing the search term
        # 500 lines of "match line N" will easily exceed 4000 chars
        content = "\n".join(f"match line {i}" for i in range(500))
        await memory.write("long-file.md", content, mode="overwrite")

        # Search for a pattern that matches all lines
        result = await memory.grep("match")

        # Verify truncation
        assert len(result) <= 4020  # 4000 + newline + "...truncated"
        assert result.endswith("...truncated")


class TestPruneShortTerm:
    """Verify prune_short_term removes old dated files."""

    async def test_prune_removes_old_dated_files(self, tmp_path: Path) -> None:
        """Test that prune_short_term removes files older than retention window."""
        memory = FilesystemMemory(tmp_path)

        # Create short-term directory and files
        short_term_dir = tmp_path / "short-term"
        short_term_dir.mkdir(parents=True, exist_ok=True)

        # Create an old dated file (2000-01-01)
        old_file = short_term_dir / "2000-01-01.md"
        old_file.write_text("old content")

        # Create a today-dated file
        today_date = datetime.now(timezone.utc).date().isoformat()
        today_file = short_term_dir / f"{today_date}.md"
        today_file.write_text("today content")

        # Create a non-date file
        keepme_file = short_term_dir / "keepme.md"
        keepme_file.write_text("keep me")

        # Verify all files exist before prune
        assert old_file.exists()
        assert today_file.exists()
        assert keepme_file.exists()

        # Prune with 14-day retention
        deleted_count = await memory.prune_short_term(14)

        # Verify only the old file was deleted
        assert deleted_count == 1
        assert not old_file.exists()
        assert today_file.exists()
        assert keepme_file.exists()

    async def test_prune_empty_short_term(self, tmp_path: Path) -> None:
        """Test that prune_short_term returns 0 when directory doesn't exist."""
        memory = FilesystemMemory(tmp_path)
        deleted_count = await memory.prune_short_term(14)
        assert deleted_count == 0

    async def test_prune_keeps_recent_dated_files(self, tmp_path: Path) -> None:
        """Test that prune_short_term keeps files within retention window."""
        memory = FilesystemMemory(tmp_path)

        short_term_dir = tmp_path / "short-term"
        short_term_dir.mkdir(parents=True, exist_ok=True)

        # Create a file from 5 days ago (within 14-day retention)
        from datetime import timedelta
        five_days_ago = (
            datetime.now(timezone.utc).date() - timedelta(days=5)
        ).isoformat()
        recent_file = short_term_dir / f"{five_days_ago}.md"
        recent_file.write_text("recent content")

        assert recent_file.exists()

        # Prune with 14-day retention
        deleted_count = await memory.prune_short_term(14)

        # Verify the recent file was kept
        assert deleted_count == 0
        assert recent_file.exists()

    async def test_prune_respects_retention_boundary(self, tmp_path: Path) -> None:
        """Test that prune correctly handles files at the retention boundary."""
        memory = FilesystemMemory(tmp_path)

        short_term_dir = tmp_path / "short-term"
        short_term_dir.mkdir(parents=True, exist_ok=True)

        # Create a file exactly 14 days old (should be kept, as it's not "strictly older")
        from datetime import timedelta
        fourteen_days_ago = (
            datetime.now(timezone.utc).date() - timedelta(days=14)
        ).isoformat()
        boundary_file = short_term_dir / f"{fourteen_days_ago}.md"
        boundary_file.write_text("boundary content")

        # Prune with 14-day retention
        deleted_count = await memory.prune_short_term(14)

        # The file should be kept (spec says "strictly older than")
        assert deleted_count == 0
        assert boundary_file.exists()

    async def test_prune_deletes_older_than_boundary(self, tmp_path: Path) -> None:
        """Test that prune deletes files strictly older than retention window."""
        memory = FilesystemMemory(tmp_path)

        short_term_dir = tmp_path / "short-term"
        short_term_dir.mkdir(parents=True, exist_ok=True)

        # Create a file 15 days old (strictly older than 14-day retention)
        from datetime import timedelta
        fifteen_days_ago = (
            datetime.now(timezone.utc).date() - timedelta(days=15)
        ).isoformat()
        old_file = short_term_dir / f"{fifteen_days_ago}.md"
        old_file.write_text("old content")

        # Prune with 14-day retention
        deleted_count = await memory.prune_short_term(14)

        # The file should be deleted
        assert deleted_count == 1
        assert not old_file.exists()
