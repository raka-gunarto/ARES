"""AddCalendarEvent's iCalendar formatting (offsets, local time, escaping)."""
from datetime import datetime, timezone

from ares.plugins.tools.time_tools import ical_text, ical_utc


def test_explicit_offsets_convert_to_utc():
    assert ical_utc("2026-07-15T14:00:00+07:00") == "20260715T070000Z"
    assert ical_utc("2026-07-15T14:00:00Z") == "20260715T140000Z"


def test_a_naive_time_is_local_not_utc():
    local = datetime(2026, 7, 15, 14, 0).astimezone()
    assert ical_utc("2026-07-15T14:00:00") == local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def test_text_cannot_inject_properties():
    escaped = ical_text("Lunch\r\nATTENDEE:mailto:eve@example.com; a, b\\")
    assert "\n" not in escaped and "\r" not in escaped
    assert escaped == r"Lunch\nATTENDEE:mailto:eve@example.com\; a\, b\\"


def test_garbage_times_raise_value_error():
    import pytest

    with pytest.raises(ValueError):
        ical_utc("next tuesday")
