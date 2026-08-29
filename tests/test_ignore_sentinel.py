"""The IGNORE sentinel is enforced in code, not just asked for in RULES.

Before this, `grep -rn IGNORE ares/` returned exactly one hit — the instruction
in prompt.py. The 46-day trace shows the three consequences: the sentinel spoken
aloud (3x, once on a live SIP call), a user-initiated turn answered IGNORE being
delivered verbatim, and 529 finals that said IGNORE with prose in front of them
going unrecognised.
"""
from __future__ import annotations

from ares.core.prompt import is_ignore
from ares.core.tool import ToolContext
from ares.plugins.tools.core_tools import Speak


# ---- recognition -----------------------------------------------------------

def test_the_forms_the_model_actually_emits():
    """Every one of these appears verbatim in the live trace."""
    assert is_ignore("IGNORE")
    assert is_ignore("IGNORE\n\nKnown pattern — ExtantSquire769 is the Xbox sensor.")
    assert is_ignore("IGNORE — known flickering Xbox integration sensor, same pattern.")
    assert is_ignore("Raka is home now. Both are in. Noted.\n\nIGNORE")
    assert is_ignore("Both sensors restored. Transient outage resolved.\n\nIGNORE")


def test_real_replies_are_never_swallowed():
    for real in (
        "Sure, I turned the living room off.",
        "IGNORE means to disregard something.",
        "It's 21 degrees in the bedroom.",
        "",
    ):
        assert not is_ignore(real), real


def test_trailing_punctuation_and_case_tolerated():
    assert is_ignore("ignore.")
    assert is_ignore("  IGNORE!  ")


# ---- the speak tool --------------------------------------------------------

class _RecordingRouter:
    def __init__(self):
        self.spoken = []

    async def speak(self, user_id, message):
        self.spoken.append(message)


def _ctx(router):
    return ToolContext(
        user_id="primary", event=None, session=None, router=router,
        memory=None, tasks=None, registry=None, services={},
    )


async def test_speak_refuses_the_sentinel():
    """Caught in the trace with a live SIP call as the active channel."""
    router = _RecordingRouter()
    r = await Speak().run(_ctx(router), message="IGNORE")
    assert r.ok is False
    assert router.spoken == [], "the sentinel must never reach a channel"


async def test_speak_still_delivers_real_words():
    router = _RecordingRouter()
    r = await Speak().run(_ctx(router), message="Welcome home, Raka.")
    assert r.ok is True
    assert router.spoken == ["Welcome home, Raka."]


# ---- the agent's step-8 delivery fallback -----------------------------------

from tests.test_agent import FakeLLM, build_agent, make_cli_event  # noqa: E402


async def test_user_initiated_ignore_is_not_delivered():
    """Step 8 only checked whether `speak` ran, so it spoke the sentinel."""
    llm = FakeLLM([{"role": "assistant", "content": "IGNORE"}])
    agent, channel, _ = build_agent(llm)
    await agent.handle(make_cli_event("hello"))
    assert channel.messages == [], "the sentinel must not be delivered"


async def test_user_initiated_prose_is_still_delivered():
    """The fallback must keep working for real answers."""
    llm = FakeLLM([{"role": "assistant", "content": "It's 21 degrees."}])
    agent, channel, _ = build_agent(llm)
    await agent.handle(make_cli_event("how warm is it"))
    assert channel.messages == ["It's 21 degrees."]


async def test_trailing_sentinel_after_prose_is_not_delivered():
    """529 finals in the trace had this exact shape."""
    llm = FakeLLM([{"role": "assistant", "content": "Raka is home. Noted.\n\nIGNORE"}])
    agent, channel, _ = build_agent(llm)
    await agent.handle(make_cli_event("anything up?"))
    assert channel.messages == []
