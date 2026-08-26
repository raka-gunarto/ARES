"""Tests for the improved Home Assistant tools (get_home_state / control_device).

Covers the two fixes grounded in the live-trace failures:
  * control_device can pass NAMED service fields via `data` (so set_hvac_mode
    with {'hvac_mode':'off'} actually reaches HA), while `value` stays as a
    back-compat convenience;
  * get_home_state surfaces a single entity's full attributes by default, with
    an `attributes` override (and ['*'] wildcard).
"""
from __future__ import annotations

from ares.core.tool import ToolContext
from ares.plugins.sources.home_assistant import HAService
from ares.plugins.tools.home_tools import ControlDevice, GetHomeState, _fmt_attr_value


class _FakeHAService(HAService):
    """HAService with the network methods stubbed out."""

    def __init__(self, states: dict | None = None):
        super().__init__("http://ha", "tok", ["climate"], blocked_controls=[])
        self._states = states or {}
        self.calls: list[tuple] = []

    async def get_state(self, entity_id: str) -> dict:
        return self._states.get(entity_id)

    async def get_states(self, domain):
        out = [
            {"entity_id": eid, **obj}
            for eid, obj in self._states.items()
            if not domain or eid.startswith(f"{domain}.")
        ]
        return out

    async def call_service(self, domain, service, entity_id, data):
        self.calls.append((domain, service, entity_id, dict(data)))
        # Mimic HA returning the changed entity's new state.
        return [{"entity_id": entity_id, "state": data.get("hvac_mode", "on")}]


def _ctx(svc):
    return ToolContext(
        user_id="primary", event=None, session=None, router=None,
        memory=None, tasks=None, registry=None, services={"home_assistant": svc},
    )


_CLIMATE = {
    "climate.living_room": {
        "state": "cool",
        "attributes": {
            "friendly_name": "Living Room",
            "hvac_modes": ["heat", "cool", "off"],
            "current_temperature": 24.6,
            "temperature": 21.0,
            "fan_mode": "auto",
            "supported_features": 9,
        },
    }
}


# ---- control_device: the turn-off fix --------------------------------------

async def test_data_passthrough_sends_named_field():
    svc = _FakeHAService()
    r = await ControlDevice().run(
        _ctx(svc),
        entity_id="climate.living_room",
        action="set_hvac_mode",
        data={"hvac_mode": "off"},
    )
    assert r.ok is True
    # The named field reaches HA verbatim — this is what 400'd before.
    assert svc.calls == [("climate", "set_hvac_mode", "climate.living_room", {"hvac_mode": "off"})]
    # And the result reports the resulting state for confirmation.
    assert "state is now 'off'" in r.content


async def test_value_backcompat_set_temperature():
    svc = _FakeHAService()
    await ControlDevice().run(
        _ctx(svc), entity_id="climate.living_room", action="set_temperature", value="21"
    )
    assert svc.calls[0][3] == {"temperature": "21"}


async def test_value_legacy_other_action():
    svc = _FakeHAService()
    await ControlDevice().run(
        _ctx(svc), entity_id="climate.living_room", action="set_thing", value="x"
    )
    assert svc.calls[0][3] == {"value": "x"}


async def test_data_takes_precedence_over_value():
    svc = _FakeHAService()
    await ControlDevice().run(
        _ctx(svc),
        entity_id="climate.living_room",
        action="set_temperature",
        data={"temperature": 19},
        value="21",
    )
    # data's explicit field wins; value does not clobber it.
    assert svc.calls[0][3] == {"temperature": 19}


# ---- get_home_state: attribute visibility ----------------------------------

async def test_single_entity_shows_all_attributes_by_default():
    svc = _FakeHAService(_CLIMATE)
    r = await GetHomeState().run(_ctx(svc), entity_id="climate.living_room")
    assert r.ok is True
    # The attributes the model needs to know how to turn it off are now visible.
    assert "hvac_modes:" in r.content and "off" in r.content
    assert "supported_features: 9" in r.content
    assert "current_temperature: 24.6" in r.content


async def test_attributes_override_selects_keys():
    svc = _FakeHAService(_CLIMATE)
    r = await GetHomeState().run(
        _ctx(svc), entity_id="climate.living_room", attributes=["current_temperature"]
    )
    assert "current_temperature: 24.6" in r.content
    assert "hvac_modes" not in r.content


async def test_wildcard_shows_everything():
    svc = _FakeHAService(_CLIMATE)
    r = await GetHomeState().run(
        _ctx(svc), entity_id="climate.living_room", attributes=["*"]
    )
    assert "fan_mode: auto" in r.content


async def test_domain_listing_is_state_only_by_default():
    svc = _FakeHAService(_CLIMATE)
    r = await GetHomeState().run(_ctx(svc), domain="climate")
    assert "climate.living_room: cool" in r.content
    assert "hvac_modes" not in r.content  # compact unless attributes requested


async def test_domain_listing_includes_requested_attributes():
    svc = _FakeHAService(_CLIMATE)
    r = await GetHomeState().run(
        _ctx(svc), domain="climate", attributes=["current_temperature"]
    )
    assert "current_temperature=24.6" in r.content


def test_fmt_attr_value_truncates():
    long = list(range(1000))
    out = _fmt_attr_value(long, limit=50)
    assert out.endswith("…") and len(out) == 51


# ---- error reporting (the 63 empty "Home error: " failures) ----------------

import httpx  # noqa: E402

from ares.plugins.sources.home_assistant import READ_TIMEOUT, SERVICE_TIMEOUT  # noqa: E402
from ares.plugins.tools.home_tools import _describe_error  # noqa: E402


def test_timeout_never_renders_empty() -> None:
    """httpx timeouts stringify to '' — they must not reach the model bare."""
    for exc in (
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.PoolTimeout(""),
        httpx.WriteTimeout(""),
    ):
        msg = _describe_error(exc)
        assert msg.strip()
        assert "timed out" in msg


def test_timeout_message_warns_command_may_have_applied() -> None:
    """A timed-out service call is not proof it did not happen."""
    msg = _describe_error(httpx.ReadTimeout(""))
    assert "may still have been applied" in msg
    assert "get_home_state" in msg


def test_empty_non_timeout_error_still_names_the_type() -> None:
    assert _describe_error(httpx.ConnectError("")) == "ConnectError (no detail)"


def test_ordinary_error_text_passes_through() -> None:
    assert _describe_error(ValueError("bad entity")) == "bad entity"


async def test_control_device_reports_timeout_usefully() -> None:
    """End-to-end: a timing-out service call yields an actionable message."""

    class _TimeoutHA(_FakeHAService):
        async def call_service(self, domain, service, entity_id, data):
            raise httpx.ReadTimeout("")

    svc = _TimeoutHA(_CLIMATE)
    result = await ControlDevice().run(
        _ctx(svc), entity_id="climate.living_room", action="set_hvac_mode",
        data={"hvac_mode": "off"},
    )
    assert not result.ok
    assert result.content != "Home error: "
    assert "timed out" in result.content


def test_service_timeout_exceeds_read_timeout() -> None:
    """Service calls actuate hardware; they must get more room than reads."""
    assert SERVICE_TIMEOUT > READ_TIMEOUT
