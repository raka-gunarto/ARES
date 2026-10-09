"""LLM client: prompt caching for Anthropic models and usage capture (spec §4.3)."""
from __future__ import annotations

import json

import httpx

from ares.core.llm.client import LLMClient

USAGE = {"prompt_tokens": 12000, "completion_tokens": 40, "cost": 0.0012,
         "prompt_tokens_details": {"cached_tokens": 11000, "cache_write_tokens": 900}}


async def call(model, body=None):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body or {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": USAGE})

    llm = LLMClient("https://llm.invalid/v1", "k", model)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        reply = await llm.chat([{"role": "user", "content": "hi"}])
    finally:
        await llm.aclose()
    return llm, seen[0], reply


async def test_anthropic_models_ask_for_caching():
    for model in ("anthropic/claude-haiku-5.5", "~anthropic/claude-sonnet-latest"):
        _, sent, _ = await call(model)
        assert sent["cache_control"] == {"type": "ephemeral"}


async def test_other_models_send_no_cache_control():
    _, sent, _ = await call("deepseek/deepseek-v3.2")
    assert "cache_control" not in sent


async def test_usage_is_kept_off_the_reply():
    llm, _, reply = await call("anthropic/claude-haiku-5.5")
    # The agent appends the reply verbatim to the conversation it sends back.
    assert reply == {"role": "assistant", "content": "ok"}
    assert llm.last_usage == {"prompt": 12000, "cached": 11000, "cache_write": 900,
                              "completion": 40, "cost": 0.0012}


async def test_missing_usage_is_none():
    llm, _, _ = await call("m", {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    assert llm.last_usage is None
