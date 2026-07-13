"""Hardening invariants (PATCH-3: H1, H2).

H1 — no tool surfaces the daemon's secret environment (generalized SECRET_CANARY).
H2 — ARES can only ever *request* a privilege (status='pending'); it has no tool
     that approves, and request_privilege can never produce 'approved'.
"""
from __future__ import annotations

import os
from pathlib import Path

from ares.core.tool import ToolContext
from ares.plugins.tools.shell_tools import RunShell
from ares.plugins.privileges.store import PrivStore
from ares.plugins.privileges.tools import PRIV_TOOLS, RequestPrivilege


def _ctx(**services):
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=None, registry=None, services=services,
    )


# ---- H1: secret canary never reaches a tool result -------------------------

async def test_run_shell_never_surfaces_env_canary(monkeypatch):
    """A SECRET_CANARY in the daemon env must not appear in run_shell output
    (dev branch uses a scrubbed restricted env; prod uses the env -i runner)."""
    monkeypatch.setenv("SECRET_CANARY", "canary-xyzzy-do-not-leak")
    sh = RunShell("", "/tmp")  # dev (no sandbox user)
    for cmd in ("env", "echo $SECRET_CANARY", "printenv SECRET_CANARY || true"):
        r = await sh.run(None, command=cmd)
        assert "canary-xyzzy-do-not-leak" not in r.content, f"canary leaked via: {cmd}"


# ---- H2: no self-approval path ---------------------------------------------

def test_no_approve_or_deny_tool_is_exposed():
    """The tool surface exposes only request/list — never approve/deny."""
    names = {t.name for t in PRIV_TOOLS}
    assert names == {"request_privilege", "get_privilege_requests"}
    for t in PRIV_TOOLS:
        blob = f"{t.name} {t.description} {' '.join(t.keywords)}".lower()
        # a tool must not *be* an approval action
        assert "approve" not in t.name and "deny" not in t.name


async def test_request_privilege_only_produces_pending(tmp_path: Path):
    """request_privilege can only ever create a 'pending' row; nothing ARES can
    call sets it to 'approved' (that is the dashboard operator's action)."""
    store = PrivStore(tmp_path / "privq.db")
    await store.init()
    try:
        ctx = _ctx(privileges=store)
        r = await RequestPrivilege().run(
            ctx, kind="package_install", command="htop", reason="monitoring"
        )
        assert r.ok is True
        pending = await store.list("pending")
        approved = await store.list("approved")
        assert len(pending) == 1 and pending[0].status == "pending"
        assert approved == [], "request_privilege must never yield an approved row"
        # RequestPrivilege exposes no approve/executing transition.
        assert not hasattr(RequestPrivilege, "approve")
    finally:
        await store.aclose()
