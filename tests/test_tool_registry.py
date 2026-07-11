from __future__ import annotations

import pytest

from ares.core.tool import BaseTool, ToolResult, ToolRegistry


# Concrete test tools
class SpeakTool(BaseTool):
    """Core tool for speaking to the user."""

    name = "speak"
    description = "Speak a message to the user"
    keywords = ("talk", "say", "message")
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    core = True

    async def run(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content="")


class MemoryGrepTool(BaseTool):
    """Search memory for remembered information."""

    name = "memory_grep"
    description = "Search memory for patterns"
    keywords = ("memory", "recall", "remember", "history")
    parameters = {"type": "object", "properties": {"pattern": {"type": "string"}}}
    core = False

    async def run(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content="")


class GetWeatherTool(BaseTool):
    """Get weather forecast."""

    name = "get_weather"
    description = "Get current weather and forecast"
    keywords = ("weather", "rain", "forecast", "umbrella")
    parameters = {"type": "object", "properties": {"location": {"type": "string"}}}
    core = False

    async def run(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content="")


class ControlDeviceTool(BaseTool):
    """Control home devices."""

    name = "control_device"
    description = "Control a home device"
    keywords = ("home", "control", "light", "switch")
    parameters = {"type": "object", "properties": {"device": {"type": "string"}}}
    core = False

    async def run(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content="")


class CalendarTool(BaseTool):
    """Manage calendar events."""

    name = "calendar"
    description = "Read or add calendar events"
    keywords = ("event", "schedule", "meeting", "time")
    parameters = {"type": "object", "properties": {"action": {"type": "string"}}}
    core = False

    async def run(self, ctx, **kwargs) -> ToolResult:
        return ToolResult(ok=True, content="")


# Test suite
def test_core_vs_non_core():
    """Test that core_tools() returns ONLY core tools."""
    registry = ToolRegistry()

    speak = SpeakTool()
    memory = MemoryGrepTool()
    weather = GetWeatherTool()

    registry.register(speak)
    registry.register(memory)
    registry.register(weather)

    core_tools = registry.core_tools()
    assert len(core_tools) == 1
    assert core_tools[0].name == "speak"
    assert core_tools[0].core is True


def test_search_excludes_core_tools():
    """Test that search() never returns core tools even if keywords match."""
    registry = ToolRegistry()

    speak = SpeakTool()
    memory = MemoryGrepTool()

    registry.register(speak)
    registry.register(memory)

    # Query "talk" matches speak's keywords, but speak is core so should not appear
    results = registry.search("talk say message")
    assert len(results) == 0

    # But searching for memory should return memory_grep
    results = registry.search("remember history")
    assert len(results) == 1
    assert results[0].name == "memory_grep"


def test_search_scoring_single_match():
    """Test that search scores by matching query tokens with keywords."""
    registry = ToolRegistry()

    memory = MemoryGrepTool()
    registry.register(memory)

    # Query "what do you remember about my history"
    # Tokens >= 3 chars: ["what", "remember", "about", "history"]
    # Keywords: ["memory", "recall", "remember", "history"]
    # Intersection: ["remember", "history"] -> score = 2
    results = registry.search("what do you remember about my history")
    assert len(results) == 1
    assert results[0].name == "memory_grep"


def test_search_scoring_multiple_tools():
    """Test that tools with higher scores appear first."""
    registry = ToolRegistry()

    weather = GetWeatherTool()
    device = ControlDeviceTool()

    registry.register(weather)
    registry.register(device)

    # Query: "umbrella weather forecast"
    # Tokens >= 3 chars: ["umbrella", "weather", "forecast"]
    # get_weather keywords: ["weather", "rain", "forecast", "umbrella"]
    #   Intersection: ["umbrella", "weather", "forecast"] -> score = 3
    # control_device keywords: ["home", "control", "light", "switch"]
    #   Intersection: [] -> score = 0
    results = registry.search("umbrella weather forecast")
    assert len(results) == 1
    assert results[0].name == "get_weather"


def test_search_scoring_tie_by_name():
    """Test that tools with equal scores are sorted alphabetically by name."""
    registry = ToolRegistry()

    weather = GetWeatherTool()
    device = ControlDeviceTool()

    registry.register(weather)
    registry.register(device)

    # Query "home light control"
    # Tokens: ["home", "light", "control"]
    # get_weather keywords: ["weather", "rain", "forecast", "umbrella"]
    #   Intersection: [] -> score = 0
    # control_device keywords: ["home", "control", "light", "switch"]
    #   Intersection: ["home", "light", "control"] -> score = 3

    # Now register calendar and make it match too
    calendar = CalendarTool()
    registry.register(calendar)

    # Create a scenario where two tools have the same score
    # Query "time event" -> both calendar and control_device might match
    # calendar: ["event", "schedule", "meeting", "time"] + "calendar"
    # control_device: ["home", "control", "light", "switch"] + "control_device"
    # Query tokens: ["time", "event"]
    # calendar tokens: ["event", "schedule", "meeting", "time", "calendar"]
    #   Intersection: ["event", "time"] -> score = 2
    # control_device tokens: ["home", "control", "light", "switch", "control", "device"]
    #   Intersection: [] -> score = 0

    # Let's create a clearer tie scenario:
    # Query "cal eve" (3+ chars tokenized: "cal" is 3 chars, "eve" is 3 chars)
    # Hmm, that won't work well. Let me reconsider.

    # Actually, let's use a query that matches both calendar and device on 1 token each
    # Query "calendar device"
    # calendar: keywords ["event", ...], name "calendar"
    #   Tokens from query that match: "calendar" -> score = 1
    # control_device: name "control_device"
    #   "control_device" tokenizes to ["control", "device"]
    #   Tokens from query: "device" -> score = 1

    results = registry.search("calendar device")
    # Both have score 1, so alphabetically: calendar < control_device
    assert len(results) == 2
    assert results[0].name == "calendar"
    assert results[1].name == "control_device"


def test_search_no_matches():
    """Test that search returns empty list when no tools match."""
    registry = ToolRegistry()

    weather = GetWeatherTool()
    registry.register(weather)

    results = registry.search("zzz nothing matches")
    assert results == []


def test_search_short_token_rule():
    """Test that short tokens (< 3 chars) in query are dropped."""
    registry = ToolRegistry()

    memory = MemoryGrepTool()
    registry.register(memory)

    # Query "an ok hi memory"
    # Short tokens dropped: "an" (2 chars), "ok" (2 chars), "hi" (2 chars)
    # Remaining tokens: ["memory"]
    # memory_grep keywords: ["memory", "recall", "remember", "history"]
    #   Intersection: ["memory"] -> score = 1
    results = registry.search("an ok hi memory")
    assert len(results) == 1
    assert results[0].name == "memory_grep"

    # Query "an ok hi"
    # All short tokens dropped, no query tokens remain
    # score = 0 for all tools
    results = registry.search("an ok hi")
    assert results == []


def test_search_limit():
    """Test that limit parameter restricts results."""
    registry = ToolRegistry()

    memory = MemoryGrepTool()
    weather = GetWeatherTool()
    device = ControlDeviceTool()

    registry.register(memory)
    registry.register(weather)
    registry.register(device)

    # Query "memory weather control" matches all three
    # memory_grep: ["memory"] -> score = 1
    # get_weather: ["weather"] -> score = 1
    # control_device: ["control"] -> score = 1
    # All tied at score 1, sorted alphabetically
    results = registry.search("memory weather control", limit=1)
    assert len(results) == 1
    assert results[0].name == "control_device"  # alphabetically first

    results = registry.search("memory weather control", limit=2)
    assert len(results) == 2
    assert results[0].name == "control_device"
    assert results[1].name == "get_weather"


def test_to_oai_schema():
    """Test conversion of tools to OpenAI function schema."""
    registry = ToolRegistry()

    weather = GetWeatherTool()
    registry.register(weather)

    schema = registry.to_oai_schema([weather])

    assert len(schema) == 1
    assert schema[0]["type"] == "function"
    assert "function" in schema[0]

    func = schema[0]["function"]
    assert func["name"] == "get_weather"
    assert func["description"] == "Get current weather and forecast"
    assert func["parameters"] == {
        "type": "object",
        "properties": {"location": {"type": "string"}},
    }


def test_to_oai_schema_multiple_tools():
    """Test OpenAI schema conversion for multiple tools."""
    registry = ToolRegistry()

    speak = SpeakTool()
    weather = GetWeatherTool()
    memory = MemoryGrepTool()

    registry.register(speak)
    registry.register(weather)
    registry.register(memory)

    schema = registry.to_oai_schema([speak, weather, memory])

    assert len(schema) == 3

    # Check first tool (speak)
    assert schema[0]["function"]["name"] == "speak"
    assert schema[0]["function"]["description"] == "Speak a message to the user"

    # Check second tool (get_weather)
    assert schema[1]["function"]["name"] == "get_weather"

    # Check third tool (memory_grep)
    assert schema[2]["function"]["name"] == "memory_grep"


def test_to_oai_schema_preserves_parameters():
    """Test that parameters dict is passed through unchanged to schema."""
    registry = ToolRegistry()

    weather = GetWeatherTool()
    registry.register(weather)

    schema = registry.to_oai_schema([weather])
    params = schema[0]["function"]["parameters"]

    # Should be the exact same dict structure as defined on the tool
    assert params == weather.parameters
    assert params["type"] == "object"
    assert "properties" in params
    assert "location" in params["properties"]
