"""Tests for the SIP comms tools, focused on `end_call` (spec §6.1).

`end_call` is the one tool that tears down its own delivery channel, so the
ordering it guarantees — farewell spoken in full, *then* hangup, then the
session pointed away from the dead call — is what these cover.
"""
from __future__ import annotations

import pytest

from ares.core.channel import ChannelType
from ares.core.session import Session
from ares.core.tool import ToolContext
from ares.plugins.tools.comms_tools import COMMS_TOOLS, EndCall


class _FakeSIP:
    """Stands in for SIPService without pjsua2."""

    def __init__(self, active: bool = True) -> None:
        self._active = active
        self.log: list[tuple] = []
        self.user_uris = {"primary": "sip:me@host"}

    def has_active_call(self) -> bool:
        return self._active

    async def speak_into_call(self, text: str) -> bool:
        self.log.append(("speak", text))
        return True

    async def hangup(self) -> bool:
        self.log.append(("hangup",))
        was_active, self._active = self._active, False
        return was_active


def _ctx(svc):
    session = Session(user_id="primary")
    session.active_channel = ChannelType.SIP_CALL
    return ToolContext(
        user_id="primary", event=None, session=session, router=None,
        memory=None, tasks=None, registry=None, services={"sip": svc},
    )


@pytest.mark.asyncio
async def test_farewell_is_spoken_before_hangup() -> None:
    svc = _FakeSIP()
    result = await EndCall().run(_ctx(svc), farewell="Goodbye.")
    assert result.ok
    assert svc.log == [("speak", "Goodbye."), ("hangup",)]


@pytest.mark.asyncio
async def test_hangup_without_farewell() -> None:
    svc = _FakeSIP()
    result = await EndCall().run(_ctx(svc))
    assert result.ok
    assert svc.log == [("hangup",)]
    assert not svc.has_active_call()


@pytest.mark.asyncio
async def test_blank_farewell_is_not_spoken() -> None:
    svc = _FakeSIP()
    await EndCall().run(_ctx(svc), farewell="   ")
    assert svc.log == [("hangup",)]


@pytest.mark.asyncio
async def test_session_stops_targeting_the_dead_call() -> None:
    """Otherwise the final assistant turn fails over to PUSH after hangup."""
    svc = _FakeSIP()
    ctx = _ctx(svc)
    await EndCall().run(ctx, farewell="Bye.")
    assert ctx.session.active_channel is not ChannelType.SIP_CALL


@pytest.mark.asyncio
async def test_no_active_call_is_refused() -> None:
    svc = _FakeSIP(active=False)
    result = await EndCall().run(_ctx(svc), farewell="Bye.")
    assert not result.ok
    assert svc.log == []  # nothing spoken into a call that is not there


@pytest.mark.asyncio
async def test_sip_not_configured() -> None:
    ctx = ToolContext(
        user_id="primary", event=None, session=Session(user_id="primary"),
        router=None, memory=None, tasks=None, registry=None, services={},
    )
    result = await EndCall().run(ctx)
    assert not result.ok


def test_end_call_is_registered() -> None:
    assert "end_call" in {t.name for t in COMMS_TOOLS}
