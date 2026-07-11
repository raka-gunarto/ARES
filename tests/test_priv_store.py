from __future__ import annotations

import pytest
import aiosqlite
from pathlib import Path

from ares.plugins.privileges.store import PrivStore, PrivRequest


@pytest.fixture
async def store(tmp_path: Path) -> PrivStore:
    """Fixture that creates and initializes a PrivStore for each test."""
    priv_store = PrivStore(tmp_path / "privq.db")
    await priv_store.init()
    yield priv_store
    await priv_store.aclose()


class TestCreate:
    """Test create operation and initial state."""

    async def test_create_returns_pending_with_no_execution_data(
        self, store: PrivStore
    ) -> None:
        """Test that create returns a PrivRequest with status 'pending' and no execution data."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )

        assert req.id
        assert req.user_id == "primary"
        assert req.kind == "package_install"
        assert req.command == "htop"
        assert req.reason == "monitoring"
        assert req.status == "pending"
        assert req.decided_at is None
        assert req.executed_at is None
        assert req.output is None
        assert req.exit_code is None
        assert req.created_at

    async def test_list_all_includes_created_request(
        self, store: PrivStore
    ) -> None:
        """Test that a created request appears in list() with no filter."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        all_reqs = await store.list()

        assert len(all_reqs) == 1
        assert all_reqs[0].id == req.id

    async def test_list_pending_includes_newly_created(
        self, store: PrivStore
    ) -> None:
        """Test that list('pending') includes a newly created request."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        pending = await store.list("pending")

        assert len(pending) == 1
        assert pending[0].id == req.id
        assert pending[0].status == "pending"

    async def test_list_approved_is_empty_for_new_request(
        self, store: PrivStore
    ) -> None:
        """Test that list('approved') is empty when only pending requests exist."""
        await store.create("primary", "package_install", "htop", "monitoring")
        approved = await store.list("approved")

        assert len(approved) == 0


class TestApprove:
    """Test approve transition (pending -> approved)."""

    async def test_approve_changes_status_and_sets_decided_at(
        self, store: PrivStore
    ) -> None:
        """Test that approve sets status to 'approved' and sets decided_at."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        approved_req = await store.approve(req.id)

        assert approved_req is not None
        assert approved_req.status == "approved"
        assert approved_req.decided_at is not None

    async def test_approve_nonexistent_returns_none(
        self, store: PrivStore
    ) -> None:
        """Test that approving a nonexistent request returns None."""
        result = await store.approve("nonexistent-id")

        assert result is None

    async def test_deny_after_approve_returns_none(
        self, store: PrivStore
    ) -> None:
        """Test that trying to deny an already-approved request returns None."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        await store.approve(req.id)

        deny_result = await store.deny(req.id)

        assert deny_result is None


class TestDeny:
    """Test deny transition (pending -> denied)."""

    async def test_deny_changes_status_and_sets_decided_at(
        self, store: PrivStore
    ) -> None:
        """Test that deny sets status to 'denied' and sets decided_at."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        denied_req = await store.deny(req.id)

        assert denied_req is not None
        assert denied_req.status == "denied"
        assert denied_req.decided_at is not None

    async def test_approve_after_deny_returns_none(
        self, store: PrivStore
    ) -> None:
        """Test that trying to approve a denied request returns None."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        await store.deny(req.id)

        approve_result = await store.approve(req.id)

        assert approve_result is None


class TestFullHappyPath:
    """Test the full lifecycle: create -> approve -> mark_executing -> mark_done."""

    async def test_full_happy_path_end_to_end(
        self, store: PrivStore
    ) -> None:
        """Test the complete happy path from creation to completion."""
        # Create
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        assert req.status == "pending"
        assert req.decided_at is None
        assert req.executed_at is None
        assert req.output is None
        assert req.exit_code is None

        # Approve
        approved_req = await store.approve(req.id)
        assert approved_req is not None
        assert approved_req.status == "approved"
        assert approved_req.decided_at is not None
        assert approved_req.executed_at is None

        # Mark executing
        executing_req = await store.mark_executing(req.id)
        assert executing_req is not None
        assert executing_req.status == "executing"
        assert executing_req.decided_at is not None
        assert executing_req.executed_at is None

        # Mark done
        done_req = await store.mark_done(req.id, 0, "installed ok")
        assert done_req is not None
        assert done_req.status == "done"
        assert done_req.exit_code == 0
        assert done_req.output == "installed ok"
        assert done_req.executed_at is not None
        assert done_req.decided_at is not None


class TestMarkExecuting:
    """Test mark_executing transition (approved -> executing)."""

    async def test_mark_executing_on_pending_returns_none(
        self, store: PrivStore
    ) -> None:
        """Test that mark_executing on a pending (not approved) request returns None."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )

        result = await store.mark_executing(req.id)

        assert result is None

    async def test_mark_executing_on_nonexistent_returns_none(
        self, store: PrivStore
    ) -> None:
        """Test that mark_executing on a nonexistent request returns None."""
        result = await store.mark_executing("nonexistent-id")

        assert result is None

    async def test_mark_executing_on_approved_succeeds(
        self, store: PrivStore
    ) -> None:
        """Test that mark_executing on an approved request succeeds."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        await store.approve(req.id)

        executing_req = await store.mark_executing(req.id)

        assert executing_req is not None
        assert executing_req.status == "executing"


class TestMarkFailed:
    """Test mark_failed transition and behavior."""

    async def test_mark_failed_sets_status_and_output_and_executed_at(
        self, store: PrivStore
    ) -> None:
        """Test that mark_failed sets status to 'failed', output, and executed_at."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        await store.approve(req.id)

        failed_req = await store.mark_failed(req.id, "rejected: not allowlisted")

        assert failed_req is not None
        assert failed_req.status == "failed"
        assert failed_req.output == "rejected: not allowlisted"
        assert failed_req.executed_at is not None
        assert failed_req.exit_code is None

    async def test_mark_failed_with_exit_code(
        self, store: PrivStore
    ) -> None:
        """Test that mark_failed can set exit_code if provided."""
        req = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        await store.approve(req.id)

        failed_req = await store.mark_failed(
            req.id, "command failed", exit_code=127
        )

        assert failed_req is not None
        assert failed_req.status == "failed"
        assert failed_req.exit_code == 127
        assert failed_req.output == "command failed"


class TestTypeCheck:
    """Test type validation via CHECK constraints."""

    async def test_create_with_invalid_kind_raises_integrity_error(
        self, store: PrivStore
    ) -> None:
        """Test that create with an invalid kind raises aiosqlite.IntegrityError."""
        with pytest.raises(aiosqlite.IntegrityError):
            await store.create("primary", "bogus_kind", "x", "y")


class TestListFilter:
    """Test list filtering by status."""

    async def test_list_filters_by_status_correctly(
        self, store: PrivStore
    ) -> None:
        """Test that list() correctly filters pending vs approved requests."""
        # Create two requests
        req1 = await store.create(
            "primary", "package_install", "htop", "monitoring"
        )
        req2 = await store.create(
            "primary", "package_install", "vim", "editor"
        )

        # Approve only the first one
        await store.approve(req1.id)

        # Check pending list
        pending = await store.list("pending")
        assert len(pending) == 1
        assert pending[0].id == req2.id

        # Check approved list
        approved = await store.list("approved")
        assert len(approved) == 1
        assert approved[0].id == req1.id

        # Check all list
        all_reqs = await store.list()
        assert len(all_reqs) == 2
