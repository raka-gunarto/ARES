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
