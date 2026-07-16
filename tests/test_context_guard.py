"""Tests for the agent context guard (§4.10): token estimation, fit_context,
and tool-result capping. These protect the system prompt from being squeezed
out and keep a single call under the model's context window.
"""
from __future__ import annotations

from ares.core.agent import (
    _cap_content,
    estimate_tokens,
    fit_context,
    message_tokens,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_estimate_tokens_scales_and_zero():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_message_tokens_counts_tool_calls():
    m = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "x", "arguments": "a" * 400}}],
    }
    # overhead(4) + name(1) + args(100)
    assert message_tokens(m) == 4 + 1 + 100


def test_under_budget_keeps_everything():
    msgs = [_msg("system", "s"), _msg("user", "hi"), _msg("assistant", "yo")]
    out = fit_context(msgs, budget=10_000)
    assert out == msgs


def test_system_prompt_is_never_dropped_and_recent_kept():
    system = _msg("system", "s" * 40)                 # 14 tokens
    rest = [_msg("user", "x" * 400) for _ in range(5)]  # 104 tokens each
    out = fit_context([system] + rest, budget=250)
    # system + the two most-recent messages fit (14 + 104 + 104 = 222 <= 250).
    assert out[0] is system
    assert len(out) == 3
    assert out[1:] == rest[-2:]


def test_never_leaves_leading_orphan_tool_message():
    system = _msg("system", "s" * 4)
    a_tc = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "", "arguments": "a" * 400}}],
    }  # 104 tokens
    t1 = _msg("tool", "t" * 400)   # 104
    u2 = _msg("user", "u" * 40)    # 14
    a2 = _msg("assistant", "a" * 40)  # 14
    out = fit_context([system, a_tc, t1, u2, a2], budget=200)
    # a_tc is trimmed; t1 would lead as an orphan tool result -> stripped.
    assert out[0] is system
    assert out[1]["role"] != "tool"
    assert out == [system, u2, a2]


def test_keeps_last_message_even_if_over_budget():
    system = _msg("system", "s")
    huge = _msg("user", "x" * 400)
    out = fit_context([system, huge], budget=10)
    assert out == [system, huge]


def test_empty_and_single_message():
    assert fit_context([], 100) == []
    solo = [_msg("system", "s")]
    assert fit_context(solo, 1) == solo


def test_cap_content_truncates_with_marker():
    capped = _cap_content("a" * 1000, char_cap=100)
    assert capped.startswith("a" * 100)
    assert "truncated: 900 chars omitted" in capped


def test_cap_content_leaves_short_untouched():
    assert _cap_content("short", char_cap=100) == "short"
