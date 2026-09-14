"""Test cases for the Agent event handling cycle. See spec §4.10.

Covers: the mocked-LLM tool loop (speak tool delivers, no double-speak),
tool-iteration budget exhaustion (forced final call with tools=None), and
forced speak on user-initiated input when the model replies with plain text
and no tool call. A bonus test covers the unknown-tool error path.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ares.core.agent import Agent
from ares.core.channel import BaseChannel, ChannelType
from ares.core.event import Event, Priority
from ares.core.router import ResponseRouter
from ares.core.session import Session, SessionManager
from ares.core.tool import ToolRegistry
from ares.core.utils.ids import new_id
from ares.plugins.tools.core_tools import CORE_TOOLS


class FakeLLM:
    """Scripted LLM stand-in. Pops one message per `chat()` call and records
    the `tools` argument each call received, so tests can assert on it."""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, temperature=0.7) -> dict:
        self.calls.append(
            {"messages": messages, "tools": tools, "temperature": temperature}
        )
        if self.script:
            return self.script.pop(0)
        # Safety net if a test's script runs out unexpectedly.
        return {"role": "assistant", "content": "done"}


class RecordingChannel(BaseChannel):
    """A CONSOLE channel that just records delivered messages."""

    type = ChannelType.CONSOLE

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        self.messages.append(message)
        return True


class FakeTasks:
    """Minimal TaskStore stand-in: no open tasks, and simple create/close."""

    async def list_open(self, user_id: str) -> list:
        return []

    async def create(self, user_id: str, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(id="t1", **kwargs)

    async def close(self, task_id: str, resolution: str) -> SimpleNamespace:
        return SimpleNamespace(id=task_id, resolution=resolution)


def build_agent(llm: FakeLLM, max_tool_iterations: int = 10):
    """Build a real Agent wired to a RecordingChannel, real SessionManager,
    real ResponseRouter, and real ToolRegistry loaded with CORE_TOOLS."""
    sessions = SessionManager()
    router = ResponseRouter(sessions)
    channel = RecordingChannel()
    router.register(channel)

    registry = ToolRegistry()
    for tool in CORE_TOOLS:
        registry.register(tool)

    agent = Agent(
        llm=llm,
        registry=registry,
        sessions=sessions,
        tasks=FakeTasks(),
        memory=object(),
        router=router,
        services={},
        persona="You are ARES.",
        max_tool_iterations=max_tool_iterations,
    )
    return agent, channel, sessions


def make_cli_event(text: str) -> Event:
    """Build a user-initiated cli_input Event carrying the given text."""
    return Event(
        id=new_id(),
        source="cli",
        type="cli_input",
        payload={"text": text},
        priority=Priority.NORMAL,
    )


def make_tool_call_message(call_id: str, name: str, arguments: dict) -> dict:
    """Build an OAI-style assistant message containing a single tool call."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


async def test_tool_loop_speak_then_final_no_double_speak():
    """A speak tool call delivers the message directly; the subsequent plain
    final reply must NOT be force-spoken again (spoke already happened)."""
    speak_call = make_tool_call_message("c1", "speak", {"message": "hello there"})
    final_msg = {"role": "assistant", "content": "anything"}
    llm = FakeLLM([speak_call, final_msg])

    agent, channel, _sessions = build_agent(llm)
    evt = make_cli_event("hi")

    await agent.handle(evt)

    assert channel.messages == ["hello there"]


async def test_forced_speak_on_user_input_without_tool_call():
    """When the model replies with plain content (no tool call) to a
    user-initiated event, the agent must force-speak the final text."""
    llm = FakeLLM([{"role": "assistant", "content": "direct answer"}])

    agent, channel, _sessions = build_agent(llm)
    evt = make_cli_event("hi")

    await agent.handle(evt)

    assert channel.messages == ["direct answer"]


async def test_budget_exhaustion_forces_final_call_without_tools():
    """If the model keeps calling tools forever, the agent must terminate
    after max_tool_iterations, issuing one final forced call with tools=None."""
    tool_call_msg = make_tool_call_message("cX", "get_active_tasks", {})
    # Provide plenty of scripted tool-call responses so FakeLLM always
    # returns a tool call, never falls through to the "done" safety net.
    llm = FakeLLM([tool_call_msg] * 10)

    max_iterations = 3
    agent, channel, _sessions = build_agent(llm, max_tool_iterations=max_iterations)
    evt = make_cli_event("hi")

    # Guard against a hang if the budget logic is broken.
    await asyncio.wait_for(agent.handle(evt), timeout=5)

    assert len(llm.calls) == max_iterations + 1
    assert llm.calls[-1]["tools"] is None


async def test_unknown_tool_call_does_not_raise_and_still_force_speaks():
    """A tool call naming a non-existent tool must not raise; the loop
    continues and the agent still force-speaks the eventual final reply."""
    unknown_call = make_tool_call_message("c9", "does_not_exist", {})
    final_msg = {"role": "assistant", "content": "fallback answer"}
    llm = FakeLLM([unknown_call, final_msg])

    agent, channel, _sessions = build_agent(llm)
    evt = make_cli_event("hi")

    await agent.handle(evt)  # must not raise

    assert channel.messages == ["fallback answer"]


# ---- malformed tool names ---------------------------------------------------

def test_sanitize_recovers_a_leaked_control_token():
    """One live call arrived with the provider's DSML token glued to the name."""
    from ares.core.agent import sanitize_tool_name

    assert sanitize_tool_name('get_home_state>\n<DSML|parameter name="x"') == "get_home_state"
    assert sanitize_tool_name("speak") == "speak"
    assert sanitize_tool_name("<garbage") == ""


async def test_agent_recovers_a_malformed_tool_name():
    llm = FakeLLM(
        [
            make_tool_call_message("c1", 'speak>\n<DSML|parameter', {"message": "hi"}),
            {"role": "assistant", "content": "done"},
        ]
    )
    agent, channel, _ = build_agent(llm)
    await agent.handle(make_cli_event("say hi"))
    assert channel.messages == ["hi"], "the recovered call should have run"


async def test_rules_reminder_is_reinjected_during_long_tool_loops():
    """Every 20 tool iterations the RULES reminder is appended again (§4.10)."""
    from ares.core.prompt import RULES_REMINDER

    llm = FakeLLM([make_tool_call_message("cX", "get_active_tasks", {})] * 30)
    agent, _channel, _ = build_agent(llm, max_tool_iterations=25)
    await asyncio.wait_for(agent.handle(make_cli_event("hi")), timeout=5)

    def reminders(call):
        return sum(
            1 for m in call["messages"]
            if m.get("role") == "system" and m.get("content") == RULES_REMINDER
        )

    assert reminders(llm.calls[19]) == 0  # the 20th call precedes iteration 20
    assert reminders(llm.calls[20]) == 1
    assert reminders(llm.calls[-1]) == 2  # plus the forced-final reminder


def _nudges(call):
    from ares.core.prompt import PROGRESS_NUDGE

    return sum(
        1 for m in call["messages"]
        if m.get("role") == "user" and m.get("content") == PROGRESS_NUDGE
    )


async def test_long_user_request_is_nudged_for_a_progress_update():
    """After 4 tool rounds without `speak` the agent asks for an update (§4.10)."""
    work = make_tool_call_message("cX", "get_active_tasks", {})
    llm = FakeLLM([work] * 9 + [{"role": "assistant", "content": "done"}])
    agent, _channel, _ = build_agent(llm, max_tool_iterations=20)
    await asyncio.wait_for(agent.handle(make_cli_event("do a long thing")), timeout=5)

    assert _nudges(llm.calls[3]) == 0  # the 4th call precedes round 4
    assert _nudges(llm.calls[4]) == 1
    assert _nudges(llm.calls[8]) == 2  # counter restarts after each nudge


async def test_speaking_resets_the_progress_nudge():
    work = make_tool_call_message("cX", "get_active_tasks", {})
    update = make_tool_call_message("cS", "speak", {"message": "still looking"})
    llm = FakeLLM([work, work, work, update, work, work, work,
                   {"role": "assistant", "content": "done"}])
    agent, channel, _ = build_agent(llm, max_tool_iterations=20)
    await asyncio.wait_for(agent.handle(make_cli_event("do a long thing")), timeout=5)

    assert all(_nudges(call) == 0 for call in llm.calls)
    assert channel.messages[0] == "still looking"


async def test_an_update_written_as_text_after_a_nudge_is_delivered():
    """Live, the model answered the nudge in text beside its tool call, not via speak."""
    work = make_tool_call_message("cX", "get_active_tasks", {})
    narrated = dict(make_tool_call_message("cY", "get_active_tasks", {}),
                    content="Three of five files read so far.")
    later = dict(make_tool_call_message("cZ", "get_active_tasks", {}), content="Let me check.")
    llm = FakeLLM([work] * 4 + [narrated, later, {"role": "assistant", "content": "All done: five files."}])
    agent, channel, _ = build_agent(llm, max_tool_iterations=20)
    await asyncio.wait_for(agent.handle(make_cli_event("do a long thing")), timeout=5)

    # Only the reply answering the nudge is spoken; other narration stays internal.
    assert channel.messages == ["Three of five files read so far.", "All done: five files."]


async def test_a_spoken_update_never_swallows_a_shorter_final_answer():
    work = make_tool_call_message("cX", "get_active_tasks", {})
    update = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "cS", "type": "function",
         "function": {"name": "speak", "arguments": json.dumps({"message": "Still going: checked four sources, two left."})}},
        {"id": "cW", "type": "function", "function": {"name": "get_active_tasks", "arguments": "{}"}},
    ]}
    llm = FakeLLM([work] * 4 + [update, {"role": "assistant", "content": "Tomorrow is dry, 19C."}])
    agent, channel, _ = build_agent(llm, max_tool_iterations=20)
    await asyncio.wait_for(agent.handle(make_cli_event("forecast?")), timeout=5)

    assert channel.messages == ["Still going: checked four sources, two left.", "Tomorrow is dry, 19C."]


async def test_ambient_events_are_never_nudged():
    work = make_tool_call_message("cX", "get_active_tasks", {})
    llm = FakeLLM([work] * 6 + [{"role": "assistant", "content": "IGNORE"}])
    agent, _channel, _ = build_agent(llm, max_tool_iterations=20)
    evt = Event(id=new_id(), source="home_assistant", type="state_change",
                payload={"entity_id": "light.x"}, priority=Priority.NORMAL, user_id="primary")
    await asyncio.wait_for(agent.handle(evt), timeout=5)

    assert all(_nudges(call) == 0 for call in llm.calls)


# ---- LLM client retries -------------------------------------------------------

async def test_llm_client_retries_a_rate_limit_then_succeeds(monkeypatch):
    import httpx

    from ares.core.llm import client as llm_client

    replies = [
        httpx.Response(429, headers={"retry-after": "0"}, text="slow down"),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
    ]
    seen = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    llm = llm_client.LLMClient("https://llm.invalid/v1", "k", "m", max_retries=2)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(llm_client.asyncio, "sleep", fake_sleep)
    try:
        assert (await llm.chat([{"role": "user", "content": "hi"}]))["content"] == "ok"
    finally:
        await llm.aclose()
    assert len(seen) == 2 and sleeps == [0.0]


def test_retry_after_is_capped():
    import httpx

    from ares.core.llm.client import MAX_RETRY_AFTER_S, _retry_after_s

    assert _retry_after_s(httpx.Response(429, headers={"retry-after": "3600"}), 2.0) == MAX_RETRY_AFTER_S
    assert _retry_after_s(httpx.Response(429), 2.0) == 2.0
