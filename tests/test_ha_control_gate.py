"""C3 (PATCH-3): outbound HA control safety gate.

The inbound event allow-list does NOT constrain outbound `control_device`; this
gate does. Doors/alarm-disarm must be refused for LLM-initiated control so a
poisoned memory note or crafted message cannot open the house.
"""
from __future__ import annotations

from ares.core.tool import ToolContext
from ares.plugins.sources.home_assistant import HAService, DEFAULT_BLOCKED_CONTROLS
from ares.plugins.tools.home_tools import ControlDevice


class _RecordingHAService(HAService):
    """HAService that records call_service instead of hitting HA."""

    def __init__(self, blocked_controls=None):
        super().__init__("http://ha", "tok", ["light"], blocked_controls)
        self.calls: list[tuple] = []

    async def call_service(self, domain, service, entity_id, data):
        self.calls.append((domain, service, entity_id))
        return {"ok": True}


def _ctx(svc):
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=None, registry=None, services={"home_assistant": svc},
    )


def test_default_denies_unlock_and_disarm():
    assert "lock.unlock" in DEFAULT_BLOCKED_CONTROLS
    svc = _RecordingHAService()
    assert svc.control_allowed("light", "turn_on") is True
    assert svc.control_allowed("lock", "unlock") is False
    assert svc.control_allowed("alarm_control_panel", "alarm_disarm") is False


async def test_control_device_refuses_unlock_without_calling_service():
    svc = _RecordingHAService()
    tool = ControlDevice()
    r = await tool.run(_ctx(svc), entity_id="lock.front_door", action="unlock")
    assert r.ok is False
    assert "safety-gated" in r.content.lower() or "refused" in r.content.lower()
    assert svc.calls == [], "call_service must NOT be invoked for a blocked action"


async def test_control_device_refuses_alarm_disarm():
    svc = _RecordingHAService()
    tool = ControlDevice()
    r = await tool.run(_ctx(svc), entity_id="alarm_control_panel.home", action="alarm_disarm")
    assert r.ok is False
    assert svc.calls == []


async def test_control_device_allows_safe_action():
    svc = _RecordingHAService()
    tool = ControlDevice()
    r = await tool.run(_ctx(svc), entity_id="light.kitchen", action="turn_on")
    assert r.ok is True
    assert svc.calls == [("light", "turn_on", "light.kitchen")]


async def test_operator_override_can_allow_unlock():
    # Empty denylist = allow all (operator's explicit choice).
    svc = _RecordingHAService(blocked_controls=[])
    tool = ControlDevice()
    r = await tool.run(_ctx(svc), entity_id="lock.front_door", action="unlock")
    assert r.ok is True
    assert svc.calls == [("lock", "unlock", "lock.front_door")]
