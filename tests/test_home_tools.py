"""GetHomeState attribute selection: default set + per-call `attributes` override.

The default attribute list lives in code (`_DEFAULT_SNAPSHOT_ATTRS` on the
service); the LLM can override which attribute keys are shown per call via the
`attributes` tool argument, with `['*']` meaning "all attributes". No config
schema change is involved.
"""
from __future__ import annotations

from ares.core.tool import ToolContext
from ares.plugins.sources.home_assistant import HAService
from ares.plugins.tools.home_tools import GetHomeState


class _FakeHAService(HAService):
    """HAService with get_state/get_states served from an in-memory list."""

    def __init__(self, states):
        super().__init__("http://ha", "tok", ["climate"])
        self._states = states

    async def get_state(self, entity_id):
        return next((s for s in self._states if s.get("entity_id") == entity_id), None)

    async def get_states(self, domain=None):
        if domain is None:
            return self._states
        return [s for s in self._states if s.get("entity_id", "").startswith(domain + ".")]


def _ctx(svc):
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=None, registry=None, services={"home_assistant": svc},
    )


_ENTITY = {
    "entity_id": "climate.living",
    "state": "heat",
    "attributes": {
        "friendly_name": "Living Room",
        "temperature": 21,
        "hvac_action": "heating",  # NOT in the default set
        "battery_level": 80,
    },
}


async def test_default_attributes_shown():
    """With no override, the default keys show and non-default ones don't."""
    r = await GetHomeState().run(_ctx(_FakeHAService([_ENTITY])), entity_id="climate.living")
    assert r.ok
    assert "friendly_name: Living Room" in r.content
    assert "temperature: 21" in r.content
    assert "battery_level: 80" in r.content
    assert "hvac_action" not in r.content


async def test_attributes_override_selects_keys():
    """A per-call `attributes` list overrides the default set for that call."""
    r = await GetHomeState().run(
        _ctx(_FakeHAService([_ENTITY])), entity_id="climate.living", attributes=["hvac_action"]
    )
    assert r.ok
    assert "hvac_action: heating" in r.content
    assert "friendly_name" not in r.content and "battery_level" not in r.content


async def test_attributes_wildcard_shows_all():
    """`['*']` shows every attribute the entity has."""
    r = await GetHomeState().run(
        _ctx(_FakeHAService([_ENTITY])), entity_id="climate.living", attributes=["*"]
    )
    assert r.ok
    for k in ("friendly_name", "temperature", "hvac_action", "battery_level"):
        assert k in r.content


async def test_domain_override_applies_per_entity():
    """The override also applies in the domain-listing branch."""
    r = await GetHomeState().run(
        _ctx(_FakeHAService([_ENTITY])), domain="climate", attributes=["hvac_action"]
    )
    assert r.ok
    assert "hvac_action=heating" in r.content
    assert "temperature=" not in r.content
