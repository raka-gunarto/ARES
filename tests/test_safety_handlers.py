"""Fire/intrusion handlers (§7.7): every alert path actually runs, no LLM."""
from __future__ import annotations

from ares.core.event import Event, Priority
from ares.plugins.critical.safety import FireHandler, IntruderHandler


class Router:
    def __init__(self):
        self.notified = []

    async def notify(self, user_id, message):
        self.notified.append((user_id, message))


class Tasks:
    def __init__(self):
        self.created = []

    async def create(self, user_id, type_, **kw):
        self.created.append((user_id, type_, kw))


class Sip:
    user_uris = {"primary": "sip:raka@example"}

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def call_and_speak(self, uri, text, listen):
        if self.fail:
            raise RuntimeError("line busy")
        self.calls.append((uri, text, listen))


def _smoke(state="on"):
    return Event(id="e1", source="home_assistant", type="state_change",
                 payload={"entity_id": "binary_sensor.smoke_kitchen", "new": {"state": state}},
                 priority=Priority.HIGH)


def test_matching_is_by_glob_and_state():
    h = FireHandler(["binary_sensor.smoke_*"], Tasks(), {})
    assert h.matches(_smoke("on")) and h.matches(_smoke("triggered"))
    assert not h.matches(_smoke("off"))
    assert not IntruderHandler(["alarm_control_panel.*"], Tasks(), {}).matches(_smoke())


async def test_away_user_gets_push_announcement_call_and_task():
    announced, sip, tasks, router = [], Sip(), Tasks(), Router()

    async def announce(phrase):
        announced.append(phrase)
        return True

    async def nobody_home():
        return False

    h = FireHandler(["binary_sensor.smoke_*"], tasks,
                    {"sip": sip, "announcers": [announce], "presence": nobody_home})
    await h.handle(_smoke(), router)
    assert router.notified == [("primary", FireHandler.phrase)]
    assert announced == [FireHandler.phrase]
    assert sip.calls == [("sip:raka@example", FireHandler.phrase, False)]
    assert tasks.created[0][1] == "monitoring"


async def test_no_call_when_someone_is_home():
    sip = Sip()

    async def home():
        return True

    await FireHandler(["*"], Tasks(), {"sip": sip, "presence": home}).handle(_smoke(), Router())
    assert sip.calls == []


async def test_one_failing_path_does_not_stop_the_others():
    tasks = Tasks()

    async def broken(phrase):
        raise RuntimeError("speaker offline")

    h = IntruderHandler(["*"], tasks, {"sip": Sip(fail=True), "announcers": [broken]})
    await h.handle(_smoke(), Router())
    assert tasks.created, "the monitoring task must still be created"
