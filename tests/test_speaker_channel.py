"""Tests for presence-aware speaker delivery (spec §4.5).

Covers the SpeakerChannel (announce on an HA media_player only when someone is
home), the WebChannel presence gate (decline when the browser stopped polling),
and the router's SPEAKER-before-PUSH fallback ordering.
"""
from __future__ import annotations

import time

import pytest

from ares.core.channel import BaseChannel, ChannelType
from ares.core.router import ResponseRouter
from ares.core.session import Session, SessionManager
from ares.plugins.channels.speaker import SpeakerChannel
from ares.plugins.dashboard.channel import PRESENCE_GRACE_S, WebChannel


class FakeHA:
    """Duck-typed stand-in for HAService."""

    def __init__(self, states: dict[str, str], fail_state: bool = False) -> None:
        self.states = states
        self.fail_state = fail_state
        self.calls: list[tuple[str, str, str, dict]] = []
        self.call_raises = False

    async def get_state(self, entity_id: str) -> dict:
        if self.fail_state:
            raise RuntimeError("HA unreachable")
        return {"entity_id": entity_id, "state": self.states.get(entity_id, "unknown")}

    async def call_service(self, domain, service, entity_id, data) -> dict:
        if self.call_raises:
            raise RuntimeError("service failed")
        self.calls.append((domain, service, entity_id, data))
        return {}


def _session() -> Session:
    return Session(user_id="primary")


# --- SpeakerChannel -------------------------------------------------------


# Piper-style config (tts.speak with a tts engine entity + media_player in data),
# matching the live HA. The legacy *_say shape is exercised separately below.
def _piper(ha, presence):
    return SpeakerChannel(
        ha,
        presence,
        service_entity="tts.piper",
        service_data={"media_player_entity_id": "media_player.living_room_nest"},
        tts_domain="tts",
        tts_service="speak",
    )


@pytest.mark.asyncio
async def test_speaker_announces_when_home():
    ha = FakeHA({"person.raka": "home"})
    ch = _piper(ha, ["person.raka"])

    ok = await ch.deliver("primary", "dinner is ready", _session())

    assert ok is True
    assert ha.calls == [
        (
            "tts",
            "speak",
            "tts.piper",
            {
                "media_player_entity_id": "media_player.living_room_nest",
                "message": "dinner is ready",
            },
        )
    ]


@pytest.mark.asyncio
async def test_speaker_supports_legacy_say_shape():
    # *_say: service_entity IS the media_player, no service_data.
    ha = FakeHA({"person.raka": "home"})
    ch = SpeakerChannel(
        ha,
        ["person.raka"],
        service_entity="media_player.bedroom",
        tts_service="google_translate_say",
    )

    ok = await ch.deliver("primary", "hi", _session())

    assert ok is True
    assert ha.calls == [
        ("tts", "google_translate_say", "media_player.bedroom", {"message": "hi"})
    ]


@pytest.mark.asyncio
async def test_speaker_declines_when_away():
    ha = FakeHA({"person.raka": "not_home"})
    ch = _piper(ha, ["person.raka"])

    ok = await ch.deliver("primary", "hello", _session())

    assert ok is False
    assert ha.calls == []  # never announce to an empty house


@pytest.mark.asyncio
async def test_speaker_home_if_any_presence_entity_home():
    ha = FakeHA({"person.raka": "not_home", "person.nadya": "home"})
    ch = _piper(ha, ["person.raka", "person.nadya"])

    assert await ch.deliver("primary", "hi", _session()) is True


@pytest.mark.asyncio
async def test_speaker_declines_when_unconfigured():
    ha = FakeHA({"person.raka": "home"})
    ch = SpeakerChannel(ha, [], service_entity="")  # no target, no presence

    assert await ch.deliver("primary", "hi", _session()) is False
    assert ha.calls == []


@pytest.mark.asyncio
async def test_speaker_declines_when_ha_presence_unreachable():
    ha = FakeHA({"person.raka": "home"}, fail_state=True)
    ch = _piper(ha, ["person.raka"])

    # Can't confirm anyone is home -> decline (router falls to PUSH).
    assert await ch.deliver("primary", "hi", _session()) is False


@pytest.mark.asyncio
async def test_speaker_declines_when_announce_fails():
    ha = FakeHA({"person.raka": "home"})
    ha.call_raises = True
    ch = _piper(ha, ["person.raka"])

    assert await ch.deliver("primary", "hi", _session()) is False


@pytest.mark.asyncio
async def test_speaker_passes_language_when_set():
    ha = FakeHA({"person.raka": "home"})
    ch = SpeakerChannel(
        ha, ["person.raka"], service_entity="media_player.bedroom",
        tts_service="google_translate_say", language="en",
    )

    await ch.deliver("primary", "hi", _session())

    assert ha.calls[0][3] == {"message": "hi", "language": "en"}


# --- WebChannel presence gate ---------------------------------------------


@pytest.mark.asyncio
async def test_web_declines_without_a_poll():
    web = WebChannel()
    # No browser has ever polled -> no listener.
    assert await web.deliver("primary", "hi", _session()) is False
    assert web.outbox("primary").empty()


@pytest.mark.asyncio
async def test_web_delivers_while_a_poll_is_connected():
    web = WebChannel()
    web.poll_started("primary")  # a long-poll is in flight
    assert await web.deliver("primary", "hi", _session()) is True
    assert web.outbox("primary").get_nowait() == "hi"


@pytest.mark.asyncio
async def test_web_delivers_within_grace_between_polls():
    web = WebChannel()
    web.poll_started("primary")
    web.poll_finished("primary")  # poll just closed; grace window is open
    assert await web.deliver("primary", "hi", _session()) is True


@pytest.mark.asyncio
async def test_web_declines_after_last_poll_closed_and_grace_elapsed():
    # The 08:31 out-of-house case: the tab is gone, no poll is connected.
    web = WebChannel()
    web.poll_started("primary")
    web.poll_finished("primary")
    web._last_poll["primary"] = time.monotonic() - (PRESENCE_GRACE_S + 1)
    assert web._waiters.get("primary", 0) == 0
    assert await web.deliver("primary", "hi", _session()) is False
    assert web.outbox("primary").empty()  # not queued -> router falls to push


@pytest.mark.asyncio
async def test_web_present_while_any_poll_in_flight_even_if_one_closed():
    web = WebChannel()
    web.poll_started("primary")
    web.poll_started("primary")
    web.poll_finished("primary")  # one closed, one still connected
    assert await web.deliver("primary", "hi", _session()) is True


# --- Router fallback ordering ---------------------------------------------


class _Recorder(BaseChannel):
    def __init__(self, ctype: ChannelType, succeed: bool) -> None:
        self.type = ctype
        self.succeed = succeed
        self.delivered: list[str] = []

    async def deliver(self, user_id, message, session) -> bool:
        self.delivered.append(message)
        return self.succeed


@pytest.mark.asyncio
async def test_router_speaker_before_push_when_home():
    sessions = SessionManager()
    router = ResponseRouter(sessions)
    web = _Recorder(ChannelType.WEB, succeed=False)  # abandoned session
    speaker = _Recorder(ChannelType.SPEAKER, succeed=True)  # someone home
    push = _Recorder(ChannelType.PUSH, succeed=True)
    for c in (web, speaker, push):
        router.register(c)
    sessions.touch("primary", ChannelType.WEB, None)

    await router.speak("primary", "msg")

    assert speaker.delivered == ["msg"]
    assert push.delivered == []  # stopped after speaker succeeded


@pytest.mark.asyncio
async def test_router_speaker_declines_falls_to_push():
    sessions = SessionManager()
    router = ResponseRouter(sessions)
    web = _Recorder(ChannelType.WEB, succeed=False)
    speaker = _Recorder(ChannelType.SPEAKER, succeed=False)  # nobody home
    push = _Recorder(ChannelType.PUSH, succeed=True)
    for c in (web, speaker, push):
        router.register(c)
    sessions.touch("primary", ChannelType.WEB, None)

    await router.speak("primary", "msg")

    assert speaker.delivered == ["msg"]
    assert push.delivered == ["msg"]
