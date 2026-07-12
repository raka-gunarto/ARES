"""The Agent — event handling cycle per spec §4.10."""
from __future__ import annotations

import json
import typing
from datetime import datetime

if typing.TYPE_CHECKING:
    from ares.core.memory.base import BaseMemory
    from ares.core.tasks.store import TaskStore

from ares.core.event import Event
from ares.core.llm.client import LLMClient
from ares.core.prompt import build_system_prompt
from ares.core.router import ResponseRouter
from ares.core.session import SessionManager
from ares.core.tool import ToolContext, ToolRegistry, ToolResult
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

USER_INITIATED_TYPES = {
    "speech",
    "sip_message",
    "cli_input",
    "call_speech",
    "web_message",
}


class Agent:
    """Coordinates LLM calls, tool execution, and reply delivery for one event."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        sessions: SessionManager,
        tasks: TaskStore,
        memory: BaseMemory,
        router: ResponseRouter,
        services: dict[str, object],
        persona: str,
        max_tool_iterations: int = 10,
    ) -> None:
        """Store collaborators needed to process events."""
        self.llm = llm
        self.registry = registry
        self.sessions = sessions
        self.tasks = tasks
        self.memory = memory
        self.router = router
        self.services = services
        self.persona = persona
        self.max_tool_iterations = max_tool_iterations

    async def handle(self, event: Event) -> None:
        """Process a single event end-to-end per spec §4.10."""
        user_initiated = False
        try:
            # STEP 1: session was already touched by the Dispatcher per §4.6.
            session = self.sessions.get(event.user_id)

            # STEP 2
            open_tasks = await self.tasks.list_open(event.user_id)

            # STEP 3
            system = build_system_prompt(
                self.persona, datetime.now().astimezone(), session, open_tasks
            )

            # STEP 4
            user_initiated = event.type in USER_INITIATED_TYPES
            if user_initiated:
                event_text = (
                    event.payload.get("text") or event.payload.get("transcript") or ""
                )
            else:
                payload_json = json.dumps(event.payload, separators=(",", ":"), default=str)
                event_text = f"[EVENT source={event.source} type={event.type}]\n{payload_json}"

            # STEP 5
            messages = [system] + list(session.history) + [
                {"role": "user", "content": event_text}
            ]

            # STEP 6
            active = self.registry.core_tools()
            active_names = {t.name for t in active}
            iterations = 0
            spoke = False
            notified = False
            final_text = ""

            # STEP 7 — the loop
            while True:
                reply = await self.llm.chat(messages, self.registry.to_oai_schema(active))
                tool_calls = reply.get("tool_calls")

                if not tool_calls:
                    final_text = reply.get("content") or ""
                    break

                messages.append(reply)

                for tc in tool_calls:
                    name = tc["function"]["name"]

                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except Exception:
                        args = None

                    if args is None:
                        result = ToolResult(False, "error: could not parse arguments")
                    else:
                        tool = self.registry.get(name)
                        if tool is None:
                            result = ToolResult(False, f"error: unknown tool {name}")
                        else:
                            ctx = ToolContext(
                                user_id=event.user_id,
                                event=event,
                                session=session,
                                router=self.router,
                                memory=self.memory,
                                tasks=self.tasks,
                                registry=self.registry,
                                services=self.services,
                            )
                            try:
                                result = await tool.run(ctx, **args)
                            except Exception as e:
                                result = ToolResult(False, f"error: {e}")

                    if name == "speak" and result.ok:
                        spoke = True
                    if name == "send_notification" and result.ok:
                        notified = True

                    if name == "search_tools" and args is not None:
                        for t in self.registry.search(args.get("query", "")):
                            if t.name not in active_names:
                                active.append(t)
                                active_names.add(t.name)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": result.content,
                        }
                    )

                iterations += 1

                if iterations >= self.max_tool_iterations:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Tool budget exhausted. Respond now without tools.",
                        }
                    )
                    final_reply = await self.llm.chat(messages, tools=None)
                    final_text = final_reply.get("content") or ""
                    break

            # STEP 8
            if user_initiated and not spoke:
                await self.router.speak(event.user_id, final_text)
            elif not user_initiated and not spoke and not notified:
                log.info("agent chose silence for event %s", event.id)

            # STEP 9
            self.sessions.append_history(event.user_id, "user", event_text)
            self.sessions.append_history(
                event.user_id, "assistant", final_text or "<acted via tools>"
            )

        except Exception:
            log.exception("agent.handle failed for event %s", event.id)
            if user_initiated:
                try:
                    await self.router.speak(
                        event.user_id, "Something went wrong handling that."
                    )
                except Exception:
                    pass
