"""Tests for Home Assistant source filtering pipeline, per spec §7.3."""
from __future__ import annotations

import time
from typing import Any

import pytest

from ares.core.event import Event, EventBus, Priority
from ares.core.session import SessionManager
from ares.plugins.sources.home_assistant import HAService, HomeAssistantSource


class FakeHAService:
    """Minimal mock HAService with snapshot_summary."""

    async def snapshot_summary(self) -> str:
        """Return a canned snapshot string."""
        return "snap"


def make_source(config: dict | None = None) -> HomeAssistantSource:
    """Build a HomeAssistantSource with real EventBus, SessionManager, and FakeHAService.

    Args:
        config: Configuration dict (defaults to spec §8 example for M6).

    Returns:
        Initialized HomeAssistantSource.
    """
    if config is None:
        config = {
            "allowed_domains": ["binary_sensor", "person", "alarm_control_panel"],
            "debounce_seconds": 5,
            "priority_rules": [
                {"match": "binary_sensor.smoke_*", "priority": "CRITICAL"},
                {"match": "alarm_control_panel.*", "priority": "CRITICAL"},
                {"match": "binary_sensor.front_door", "priority": "HIGH"},
            ],
        }

    bus = EventBus()
    sessions = SessionManager()
    service = FakeHAService()
    return HomeAssistantSource(bus, config, service, sessions)


def state_changed(entity_id: str, old: str | None, new: str) -> dict:
    """Build a HA state_changed event data dict.

    Args:
        entity_id: The entity identifier (e.g. 'binary_sensor.front_door').
        old: The old state string, or None if entity is new.
        new: The new state string.

    Returns:
        A dict matching HA state_changed event structure.
    """
    return {
        "entity_id": entity_id,
        "old_state": {"state": old} if old is not None else None,
        "new_state": {"state": new, "attributes": {"friendly_name": entity_id}},
    }


@pytest.mark.asyncio
async def test_domain_allow_list() -> None:
    """Test that only entities in allowed_domains or allowed_entities are emitted."""
    source = make_source()

    # light.kitchen (not in allowed_domains) should be dropped
    result = await source.process_state_changed(state_changed("light.kitchen", "off", "on"))
    assert result is False
    assert source.bus.qsize() == 0

    # binary_sensor.front_door (in allowed_domains) should be emitted
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.type == "state_change"
    assert event.payload["entity_id"] == "binary_sensor.front_door"

    # Test allowed_entities: switch.special not in domains but in allowed_entities
    config = {
        "allowed_domains": ["binary_sensor"],
        "allowed_entities": ["switch.special"],
        "debounce_seconds": 5,
        "priority_rules": [],
    }
    source = make_source(config)
    result = await source.process_state_changed(state_changed("switch.special", "off", "on"))
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.payload["entity_id"] == "switch.special"


@pytest.mark.asyncio
async def test_same_state_drop() -> None:
    """Test that state_changed events with identical old and new state are dropped."""
    source = make_source()

    # binary_sensor.front_door on→on (same state) should be dropped
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "on", "on")
    )
    assert result is False
    assert source.bus.qsize() == 0

    # binary_sensor.front_door off→on (different state) should be emitted
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1


@pytest.mark.asyncio
async def test_debounce() -> None:
    """Test debounce suppresses rapid re-emissions of the same entity."""
    source = make_source(
        {
            "allowed_domains": ["binary_sensor"],
            "debounce_seconds": 100,  # large debounce window
            "priority_rules": [],
        }
    )

    # First emission: binary_sensor.front_door off→on (should emit)
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1
    await source.bus.get()  # consume the event

    # Second emission immediately after: binary_sensor.front_door on→off
    # (different state, but within debounce window, should be dropped)
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "on", "off")
    )
    assert result is False
    assert source.bus.qsize() == 0

    # Test debounce expiry: manipulate _last_emit to the far past
    source._last_emit["binary_sensor.front_door"] = time.monotonic() - 1000
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1


@pytest.mark.asyncio
async def test_priority_rules_mapping() -> None:
    """Test that priority_rules patterns map entities to correct event priorities."""
    source = make_source()

    # binary_sensor.smoke_kitchen matches "binary_sensor.smoke_*" → CRITICAL
    result = await source.process_state_changed(
        state_changed("binary_sensor.smoke_kitchen", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.priority == Priority.CRITICAL

    # alarm_control_panel.home matches "alarm_control_panel.*" → CRITICAL
    result = await source.process_state_changed(
        state_changed("alarm_control_panel.home", "disarmed", "triggered")
    )
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.priority == Priority.CRITICAL

    # binary_sensor.front_door matches "binary_sensor.front_door" → HIGH
    result = await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.priority == Priority.HIGH

    # binary_sensor.hallway_motion (no matching rule) → NORMAL (default)
    result = await source.process_state_changed(
        state_changed("binary_sensor.hallway_motion", "off", "on")
    )
    assert result is True
    assert source.bus.qsize() == 1
    event = await source.bus.get()
    assert event.priority == Priority.NORMAL


@pytest.mark.asyncio
async def test_blocked_entities_glob() -> None:
    """blocked_entities globs drop events even inside an allowed domain."""
    config = {
        "allowed_domains": ["binary_sensor", "person"],
        "blocked_entities": ["binary_sensor.extantsquire769*"],
        "debounce_seconds": 0,
        "priority_rules": [],
    }
    source = make_source(config)

    # The base entity and every derivative are silenced.
    for entity in (
        "binary_sensor.extantsquire769",
        "binary_sensor.extantsquire769_subscribed_to_xbox_game_pass",
    ):
        assert await source.process_state_changed(
            state_changed(entity, "off", "unavailable")
        ) is False
    assert source.bus.qsize() == 0

    # A sibling in the same domain still gets through.
    assert await source.process_state_changed(
        state_changed("binary_sensor.front_door", "off", "on")
    ) is True
    assert source.bus.qsize() == 1


@pytest.mark.asyncio
async def test_blocked_entities_beats_allowed_entities() -> None:
    """The deny-list wins over an explicit allowed_entities entry."""
    config = {
        "allowed_domains": [],
        "allowed_entities": ["switch.special"],
        "blocked_entities": ["switch.*"],
        "debounce_seconds": 0,
        "priority_rules": [],
    }
    source = make_source(config)
    assert await source.process_state_changed(
        state_changed("switch.special", "off", "on")
    ) is False
    assert source.bus.qsize() == 0


# ---- availability churn (the generic fix for the Xbox saga) -----------------

async def test_unavailable_transitions_are_dropped_by_default():
    """1,490 of 1,730 traced events were one integration flapping like this."""
    src_ = make_source()
    assert await src_.process_state_changed(
        state_changed("binary_sensor.xbox", "off", "unavailable")
    ) is False
    assert await src_.process_state_changed(
        state_changed("binary_sensor.xbox", "unavailable", "off")
    ) is False


async def test_unknown_is_treated_the_same():
    src_ = make_source()
    assert await src_.process_state_changed(
        state_changed("binary_sensor.xbox", "on", "unknown")
    ) is False


async def test_real_state_changes_still_emit():
    src_ = make_source()
    assert await src_.process_state_changed(
        state_changed("binary_sensor.kitchen_motion", "off", "on")
    ) is True


async def test_high_priority_entities_are_exempt():
    """A smoke detector going offline IS worth knowing about."""
    src_ = make_source()
    assert await src_.process_state_changed(
        state_changed("binary_sensor.smoke_kitchen", "off", "unavailable")
    ) is True


async def test_opt_in_restores_the_old_behaviour():
    src_ = make_source()
    src_.emit_unavailable = True
    assert await src_.process_state_changed(
        state_changed("binary_sensor.xbox", "off", "unavailable")
    ) is True


async def test_new_entity_appearing_with_a_real_state_still_emits():
    """old_state=None is a genuinely new entity, not availability churn."""
    src_ = make_source()
    assert await src_.process_state_changed(
        state_changed("binary_sensor.kitchen_motion", None, "on")
    ) is True
