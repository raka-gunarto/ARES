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
from ares.core.prompt import RULES_REMINDER, build_system_prompt, is_ignore
from ares.core.router import ResponseRouter
from ares.core.session import SessionManager
from ares.core.tool import ToolContext, ToolRegistry, ToolResult
from ares.core.trace import NullTracer, Tracer
from ares.core.utils.logging import get_logger

log = get_logger(__name__)

USER_INITIATED_TYPES = {
    "speech",
    "sip_message",
    "cli_input",
    "call_speech",
    "web_message",
}

# --- Context guard (§4.10) ---------------------------------------------------
# Cheap, dependency-free token estimate (spec §12 forbids a tokenizer dep): ~4
# chars/token, rounded up so the guard errs toward keeping headroom.
_CHARS_PER_TOKEN = 4
_MSG_OVERHEAD_TOKENS = 4  # per-message role/formatting overhead
# Tokens reserved for the model's own output when `max_tokens` isn't configured.
_DEFAULT_OUTPUT_RESERVE = 4096
# Never let the fitted input budget collapse below this (keeps the guard sane if
# context_window is misconfigured tiny).
_MIN_INPUT_BUDGET = 2048
# Reinject the RULES reminder every N tool iterations during a long loop.
_REMINDER_EVERY = 20


def estimate_tokens(text: str) -> int:
    """Rough token count for a string (ceil of chars / 4)."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def message_tokens(msg: dict) -> int:
    """Rough token count for one OAI message dict (content + tool_calls)."""
    total = _MSG_OVERHEAD_TOKENS
    content = msg.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        total += estimate_tokens(fn.get("name", "")) + estimate_tokens(fn.get("arguments") or "")
    return total


def fit_context(messages: list[dict], budget: int) -> list[dict]:
    """Trim `messages` to ~`budget` estimated input tokens for one LLM call.

    Invariants: messages[0] (the system prompt) is ALWAYS kept — the guard drops
    from the oldest non-system messages and keeps the most recent ones, so the
    system prompt and its RULES block can never be squeezed out on our side. The
    returned suffix never begins with an orphan `tool` message (a tool result
    whose assistant `tool_calls` turn was trimmed), which would violate the OAI
    contract. At least the most recent message is always kept, even if the two of
    them exceed `budget` (nothing safe to drop).
    """
    if len(messages) <= 1:
        return list(messages)
    system = messages[0]
    rest = messages[1:]
    used = message_tokens(system)
    kept_rev: list[dict] = []
    for m in reversed(rest):
        t = message_tokens(m)
        if kept_rev and used + t > budget:
            break
        used += t
        kept_rev.append(m)
    kept = list(reversed(kept_rev))
    # Drop leading orphan tool results (their assistant turn was trimmed away).
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return [system] + kept


def sanitize_tool_name(name: str) -> str:
    """Recover a tool name from a malformed function-call name.

    Some providers leak their own control tokens into the `name` field. The
    live trace has one call arriving as
    `get_home_state>\n<|DSML|>parameter name="attributes"...`, which resolved to
    no tool and cost the cycle. Truncating at the first character that cannot
    appear in a tool name recovers the intended call.
    """
    out = []
    for ch in (name or ""):
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out)


def _cap_content(text: str, char_cap: int) -> str:
    """Truncate a tool result so a single dump can't dominate the context."""
    if char_cap <= 0 or len(text) <= char_cap:
        return text
    return text[:char_cap] + f"\n…[truncated: {len(text) - char_cap} chars omitted]"


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
        context_window: int = 128000,
        max_output_tokens: int | None = None,
        tracer: Tracer | None = None,
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
        self.context_window = context_window
        # Reserve room for the model's reply out of the window (§4.10).
        self._output_reserve = max_output_tokens or _DEFAULT_OUTPUT_RESERVE
        # Cap any single tool result at ~1/8 of the window so one dump can't
        # crowd out everything else before fit_context even runs.
        self._tool_result_char_cap = max(8000, (context_window // 8) * _CHARS_PER_TOKEN)
        self.tracer = tracer or NullTracer()

    def _input_budget(self, tools: list[dict] | None) -> int:
        """Estimated input-token budget for one call, after reserving output and
        the tool-schema tokens (which also count against the window)."""
        budget = self.context_window - self._output_reserve
        if tools:
            budget -= estimate_tokens(json.dumps(tools))
        return max(budget, _MIN_INPUT_BUDGET)

    async def _chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        """Fit `messages` to the input budget (system prompt always kept), then
        call the LLM. Trimming is recomputed each call from the full list."""
        return await self.llm.chat(fit_context(messages, self._input_budget(tools)), tools)

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

            self.tracer.emit(
                "event",
                event_id=event.id,
                source=event.source,
                type=event.type,
                user_id=event.user_id,
                priority=getattr(event.priority, "name", str(event.priority)),
                room=event.room,
                channel=str(session.active_channel),
                user_initiated=user_initiated,
                history_len=len(session.history),
                text=event_text,
            )

            # STEP 6
            active = self.registry.core_tools()
            active_names = {t.name for t in active}
            iterations = 0
            spoke = False
            notified = False
            final_text = ""

            # STEP 7 — the loop
            while True:
                reply = await self._chat(messages, self.registry.to_oai_schema(active))
                tool_calls = reply.get("tool_calls")

                self.tracer.emit(
                    "reply",
                    event_id=event.id,
                    content=reply.get("content") or "",
                    thinking=reply.get("reasoning_content") or reply.get("reasoning") or "",
                    tool_calls=[
                        {
                            "name": tc.get("function", {}).get("name"),
                            "arguments": tc.get("function", {}).get("arguments"),
                        }
                        for tc in (tool_calls or [])
                    ],
                )

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
                            # Retry once against a cleaned name before giving up.
                            cleaned = sanitize_tool_name(name)
                            if cleaned and cleaned != name:
                                tool = self.registry.get(cleaned)
                                if tool is not None:
                                    log.warning(
                                        "recovered malformed tool name %r -> %r",
                                        name,
                                        cleaned,
                                    )
                                    name = cleaned
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

                    self.tracer.emit(
                        "tool",
                        event_id=event.id,
                        name=name,
                        arguments=args,
                        ok=result.ok,
                        result=result.content,
                    )

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
                            "content": _cap_content(result.content, self._tool_result_char_cap),
                        }
                    )

                iterations += 1

                # Reinject the trust-boundary reminder during long loops, so the
                # injection defense stays salient after many tool outputs (§4.10).
                if iterations % _REMINDER_EVERY == 0:
                    messages.append({"role": "system", "content": RULES_REMINDER})

                if iterations >= self.max_tool_iterations:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Tool budget exhausted. Respond now without tools.",
                        }
                    )
                    messages.append({"role": "system", "content": RULES_REMINDER})
                    final_reply = await self._chat(messages, tools=None)
                    final_text = final_reply.get("content") or ""
                    self.tracer.emit(
                        "reply",
                        event_id=event.id,
                        content=final_text,
                        thinking=final_reply.get("reasoning_content")
                        or final_reply.get("reasoning") or "",
                        tool_calls=[],
                        forced_final=True,
                    )
                    break

            # STEP 8
            # A final answer of IGNORE is the model declining to act (RULES). It
            # is a control token, never a reply: the step-8 fallback used to
            # deliver it verbatim to the user on any user-initiated turn.
            ignored = is_ignore(final_text)
            if user_initiated and not spoke and not ignored:
                await self.router.speak(event.user_id, final_text)
            elif not spoke and not notified:
                log.info("agent chose silence for event %s", event.id)

            self.tracer.emit(
                "final",
                event_id=event.id,
                text=final_text,
                spoke=spoke,
                notified=notified,
                ignored=ignored,
                channel=str(session.active_channel),
            )

            # STEP 9
            self.sessions.append_history(event.user_id, "user", event_text)
            self.sessions.append_history(
                event.user_id, "assistant", final_text or "<acted via tools>"
            )

        except Exception as e:
            log.exception("agent.handle failed for event %s", event.id)
            self.tracer.emit("error", event_id=event.id, error=repr(e))
            if user_initiated:
                try:
                    await self.router.speak(
                        event.user_id, "Something went wrong handling that."
                    )
                except Exception:
                    pass
