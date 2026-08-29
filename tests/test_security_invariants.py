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


# --- Subagent boundary (spec §20.2) -----------------------------------------

def test_subagent_allowlist_never_grants_a_tool_that_acts():
    """The whole safety argument for background runs rests on this list.

    A subagent runs unattended with fetched web pages as its main input. If it
    could speak, notify, call, control the home, write memory or escalate, a
    poisoned page would have a path to all of those with nobody in the
    conversation to notice.
    """
    from ares.core.subagent_run import SUBAGENT_ALLOWED_TOOLS

    for forbidden in (
        "speak",
        "send_notification",
        "place_call",
        "end_call",
        "send_sip_message",
        "control_device",
        "memory_write",
        "memory_delete",
        "create_task",
        "close_task",
        "update_task",
        "request_privilege",
        "open_pr",
        "run_shell",
        "spawn_subagent",
        "cancel_subagent",
    ):
        assert forbidden not in SUBAGENT_ALLOWED_TOOLS, forbidden


def test_every_registered_tool_is_classified():
    """A tool added later must be denied to subagents until someone decides.

    This is what makes the allowlist an allowlist: if a new tool appears in
    neither the allow nor the forbid set, this test fails and forces the call
    to be made deliberately rather than defaulting to 'granted'.
    """
    from ares.core.subagent_run import SUBAGENT_ALLOWED_TOOLS, SUBAGENT_FORBIDDEN_TOOLS
    from ares.plugins.tools.comms_tools import COMMS_TOOLS
    from ares.plugins.tools.core_tools import CORE_TOOLS
    from ares.plugins.tools.home_tools import HOME_TOOLS
    from ares.plugins.tools.memory_tools import MEMORY_TOOLS
    from ares.plugins.tools.subagent_tools import SUBAGENT_TOOLS
    from ares.plugins.tools.task_tools import TASK_TOOLS

    known = SUBAGENT_ALLOWED_TOOLS | SUBAGENT_FORBIDDEN_TOOLS
    everything = (
        *CORE_TOOLS, *MEMORY_TOOLS, *TASK_TOOLS, *HOME_TOOLS,
        *COMMS_TOOLS, *SUBAGENT_TOOLS,
    )
    unclassified = sorted({t.name for t in everything} - known)
    assert not unclassified, (
        f"unclassified tools: {unclassified}. Add each to SUBAGENT_ALLOWED_TOOLS "
        "or SUBAGENT_FORBIDDEN_TOOLS in ares/core/subagent_run.py."
    )


def test_subagent_prompt_carries_the_frozen_rules_verbatim():
    """A run's input is entirely untrusted, so it needs RULES more, not less."""
    from datetime import datetime, timezone

    from ares.core.prompt import RULES, build_subagent_prompt

    msg = build_subagent_prompt("find something out", datetime.now(timezone.utc))
    assert RULES in msg["content"], "RULES must be reproduced verbatim"
    assert msg["content"].endswith(RULES), "RULES must come last, after context"


def test_report_progress_is_not_reachable_from_the_main_agent():
    """It belongs to a run, is built per run, and must not be registered."""
    from ares.plugins.tools.subagent_tools import SUBAGENT_TOOLS

    assert "report_progress" not in {t.name for t in SUBAGENT_TOOLS}


def test_core_subagents_never_imports_plugins():
    """Spec §0 rule 14: core never imports from ares.plugins."""
    from pathlib import Path

    for module in ("subagents.py", "subagent_run.py"):
        source = Path("ares/core") / module
        assert "ares.plugins" not in source.read_text(), module
