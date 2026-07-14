from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from ares.core.utils.ids import new_id


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp to an aware UTC datetime, or None if unparseable.

    Tolerant of the shapes an LLM tends to emit: a trailing 'Z', an explicit
    offset, or a naive value (read as UTC). This is why reminder matching parses
    rather than string-compares — 'Z' would sort wrong against '+00:00'.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclasses.dataclass
class Task:
    """Mirrors the tasks table schema."""

    id: str
    user_id: str
    type: str
    status: str
    title: str
    detail: str
    due_at: str | None
    trigger: str | None
    data: dict
    created_at: str
    updated_at: str
    closed_at: str | None
    resolution: str | None


class TaskStore:
    """SQLite-backed task storage with aiosqlite."""

    def __init__(self, db_path: Path) -> None:
        """Initialize store with database path."""
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize database connection and create schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")

        # Create schema from spec §4.13
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              type TEXT NOT NULL CHECK (type IN
                ('awaiting_response','monitoring','reminder_pending','multi_step','deferred')),
              status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
              title TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              due_at TEXT,
              trigger TEXT,
              data TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              closed_at TEXT,
              resolution TEXT
            )
            """
        )

        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(user_id, status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at)"
        )

        await self._db.commit()

    def _now(self) -> str:
        """Return current UTC time in ISO8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def _row_to_task(self, row: aiosqlite.Row) -> Task:
        """Convert database row to Task dataclass."""
        return Task(
            id=row["id"],
            user_id=row["user_id"],
            type=row["type"],
            status=row["status"],
            title=row["title"],
            detail=row["detail"],
            due_at=row["due_at"],
            trigger=row["trigger"],
            data=json.loads(row["data"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
            resolution=row["resolution"],
        )

    async def create(
        self,
        user_id: str,
        type: str,
        title: str,
        detail: str = "",
        due_at: str | None = None,
        trigger: str | None = None,
        data: dict | None = None,
    ) -> Task:
        """Create a new task. CHECK constraint on type will raise IntegrityError if invalid."""
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        tid = new_id()
        now = self._now()
        data_json = json.dumps(data or {})

        await self._db.execute(
            """
            INSERT INTO tasks
            (id, user_id, type, status, title, detail, due_at, trigger, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, user_id, type, "open", title, detail, due_at, trigger, data_json, now, now),
        )
        await self._db.commit()

        # Fetch and return the created task
        cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (tid,))
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_task(row)

    async def update(self, task_id: str, **fields) -> Task | None:
        """Update task fields. Returns updated Task or None if not found."""
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        # Allowed columns to update
        allowed = {
            "title",
            "detail",
            "due_at",
            "trigger",
            "data",
            "status",
            "closed_at",
            "resolution",
            "type",
        }

        # Filter to allowed fields and prepare updates
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            # If no valid fields, still fetch the task
            cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cursor.fetchone()
            await cursor.close()
            return self._row_to_task(row) if row else None

        # Always update updated_at
        updates["updated_at"] = self._now()

        # Handle data as JSON
        if "data" in updates and isinstance(updates["data"], dict):
            updates["data"] = json.dumps(updates["data"])

        # Build parameterized UPDATE
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [task_id]

        await self._db.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", values
        )
        await self._db.commit()

        # Fetch and return the updated task
        cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_task(row) if row else None

    async def close(self, task_id: str, resolution: str) -> Task | None:
        """Close an open task. Returns None if task not found or not open."""
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        now = self._now()
        cursor = await self._db.execute(
            """
            UPDATE tasks
            SET status = ?, resolution = ?, closed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'open'
            """,
            ("closed", resolution, now, now, task_id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the closed task
        cursor = await self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_task(row) if row else None

    async def list_open(self, user_id: str) -> list[Task]:
        """List all open tasks for a user, ordered by created_at."""
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'open' ORDER BY created_at",
            (user_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_task(row) for row in rows]

    async def list_due(self, now: datetime) -> list[Task]:
        """List open reminder_pending tasks due at or before `now`.

        due_at is parsed to a datetime (not string-compared) so a reminder fires
        regardless of the exact ISO8601 spelling the agent produced ('Z' vs
        '+00:00', with/without fractional seconds); a naive value reads as UTC and
        an unparseable value is skipped rather than crashing the scheduler loop.
        """
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        cursor = await self._db.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'open'
              AND type = 'reminder_pending'
              AND due_at IS NOT NULL
            ORDER BY due_at
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()

        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        due: list[Task] = []
        for row in rows:
            when = _parse_iso_utc(row["due_at"])
            if when is not None and when <= now:
                due.append(self._row_to_task(row))
        return due

    async def history(self, user_id: str, limit: int = 20) -> list[Task]:
        """List closed tasks for a user, most recent first."""
        if self._db is None:
            raise RuntimeError("TaskStore not initialized; call await init()")

        cursor = await self._db.execute(
            """
            SELECT * FROM tasks
            WHERE user_id = ? AND status = 'closed'
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_task(row) for row in rows]

    async def aclose(self) -> None:
        """Close database connection."""
        if self._db is not None:
            await self._db.close()
