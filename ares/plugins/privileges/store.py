from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from ares.core.utils.ids import new_id


# Statuses a request can no longer move out of — the point at which the outcome
# is worth telling ARES about exactly once.
TERMINAL_STATUSES = ("done", "failed", "denied")


@dataclasses.dataclass
class PrivRequest:
    """Mirrors the priv_requests table schema."""

    id: str
    user_id: str
    kind: str
    command: str
    reason: str
    status: str
    exit_code: int | None
    output: str | None
    created_at: str
    decided_at: str | None
    executed_at: str | None
    notified: bool = False


class PrivStore:
    """SQLite-backed privilege request storage with aiosqlite."""

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

        # Create schema from spec §16.1
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS priv_requests (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              kind TEXT NOT NULL CHECK (kind IN ('package_install','service_action','command')),
              command TEXT NOT NULL,
              reason TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','denied','executing','done','failed')),
              exit_code INTEGER,
              output TEXT,
              created_at TEXT NOT NULL,
              decided_at TEXT,
              executed_at TEXT,
              notified INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        await self._migrate_notified()
        await self._db.commit()

    async def _migrate_notified(self) -> None:
        """Add `notified` to a pre-existing table, back-filling old outcomes.

        Before this column the source deduplicated emitted updates in an
        in-memory set, so every daemon restart re-announced every completed
        request as though it were new — a rejection from weeks earlier would
        arrive as a fresh event on every boot. Existing terminal rows are
        back-filled to notified=1: they have already been announced (repeatedly),
        and the rows themselves are kept so the audit trail of what was
        requested and refused stays intact.
        """
        cursor = await self._db.execute("PRAGMA table_info(priv_requests)")
        cols = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        if "notified" in cols:
            return

        await self._db.execute(
            "ALTER TABLE priv_requests ADD COLUMN notified INTEGER NOT NULL DEFAULT 0"
        )
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        await self._db.execute(
            f"UPDATE priv_requests SET notified = 1 WHERE status IN ({placeholders})",
            TERMINAL_STATUSES,
        )

    def _now(self) -> str:
        """Return current UTC time in ISO8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def _row_to_priv_request(self, row: aiosqlite.Row) -> PrivRequest:
        """Convert database row to PrivRequest dataclass."""
        return PrivRequest(
            id=row["id"],
            user_id=row["user_id"],
            kind=row["kind"],
            command=row["command"],
            reason=row["reason"],
            status=row["status"],
            exit_code=row["exit_code"],
            output=row["output"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            executed_at=row["executed_at"],
            notified=bool(row["notified"]),
        )

    async def create(
        self, user_id: str, kind: str, command: str, reason: str
    ) -> PrivRequest:
        """Create a new privilege request. Status defaults to 'pending'.
        CHECK constraint on kind will raise IntegrityError if invalid."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        req_id = new_id()
        now = self._now()

        await self._db.execute(
            """
            INSERT INTO priv_requests
            (id, user_id, kind, command, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (req_id, user_id, kind, command, reason, "pending", now),
        )
        await self._db.commit()

        # Fetch and return the created request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (req_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row)

    async def list(self, status: str | None = None) -> list[PrivRequest]:
        """List all privilege requests, optionally filtered by status.
        Ordered by created_at."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        if status is None:
            cursor = await self._db.execute(
                "SELECT * FROM priv_requests ORDER BY created_at"
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM priv_requests WHERE status = ? ORDER BY created_at",
                (status,),
            )

        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_priv_request(row) for row in rows]

    async def list_unnotified(self) -> list[PrivRequest]:
        """Terminal requests whose outcome has not been announced yet.

        Survives restarts (unlike the in-memory set this replaces), so a
        completed request is reported exactly once for the life of the row.
        """
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        cursor = await self._db.execute(
            f"SELECT * FROM priv_requests "
            f"WHERE notified = 0 AND status IN ({placeholders}) "
            f"ORDER BY created_at",
            TERMINAL_STATUSES,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_priv_request(row) for row in rows]

    async def mark_notified(self, id: str) -> None:
        """Record that this request's outcome has been announced."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        await self._db.execute(
            "UPDATE priv_requests SET notified = 1 WHERE id = ?", (id,)
        )
        await self._db.commit()

    async def get(self, id: str) -> PrivRequest | None:
        """Get a privilege request by id."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def approve(self, id: str) -> PrivRequest | None:
        """Approve a pending request (pending -> approved).
        Sets decided_at to current time. DASHBOARD ONLY.
        Returns None if not found or not in pending status."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        now = self._now()
        cursor = await self._db.execute(
            """
            UPDATE priv_requests
            SET status = ?, decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            ("approved", now, id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the updated request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def deny(self, id: str) -> PrivRequest | None:
        """Deny a pending request (pending -> denied).
        Sets decided_at to current time. DASHBOARD ONLY.
        Returns None if not found or not in pending status."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        now = self._now()
        cursor = await self._db.execute(
            """
            UPDATE priv_requests
            SET status = ?, decided_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            ("denied", now, id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the updated request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def mark_executing(self, id: str) -> PrivRequest | None:
        """Mark a request as executing (approved -> executing). BROKER ONLY.
        Returns None if not found or not in approved status."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        cursor = await self._db.execute(
            """
            UPDATE priv_requests
            SET status = ?
            WHERE id = ? AND status = 'approved'
            """,
            ("executing", id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the updated request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def mark_done(
        self, id: str, exit_code: int, output: str
    ) -> PrivRequest | None:
        """Mark a request as done with exit code and output.
        Sets status to 'done' and executed_at to current time."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        now = self._now()
        cursor = await self._db.execute(
            """
            UPDATE priv_requests
            SET status = ?, exit_code = ?, output = ?, executed_at = ?
            WHERE id = ?
            """,
            ("done", exit_code, output, now, id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the updated request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def mark_failed(
        self, id: str, output: str, exit_code: int | None = None
    ) -> PrivRequest | None:
        """Mark a request as failed with output and optional exit code.
        Sets status to 'failed' and executed_at to current time."""
        if self._db is None:
            raise RuntimeError("PrivStore not initialized; call await init()")

        now = self._now()
        cursor = await self._db.execute(
            """
            UPDATE priv_requests
            SET status = ?, exit_code = ?, output = ?, executed_at = ?
            WHERE id = ?
            """,
            ("failed", exit_code, output, now, id),
        )
        await self._db.commit()

        if cursor.rowcount == 0:
            await cursor.close()
            return None

        await cursor.close()

        # Fetch and return the updated request
        cursor = await self._db.execute(
            "SELECT * FROM priv_requests WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_priv_request(row) if row else None

    async def aclose(self) -> None:
        """Close database connection."""
        if self._db is not None:
            await self._db.close()
