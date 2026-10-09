"""MCP-to-LangChain bridge: discovery, schema conversion, session lifecycle, and errors."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from langchain_core.tools import ToolException
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from agent.weather_client import (
    EXPECTED_TOOLS,
    MCPToolClient,
    _build_args_schema,
    get_langchain_tools,
    weather_server_params,
)
from evaluation.harness import in_memory_mcp_client
from tests.helpers import PROJECT_ROOT


def test_json_schema_is_converted_to_pydantic_args():
    tool = SimpleNamespace(
        name="geocode_city",
        inputSchema={
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "country_code": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 1},
                "tags": {"type": "array"},
            },
            "required": ["city"],
        },
    )
    model = _build_args_schema(tool)
    assert model.__name__ == "GeocodeCityArgs"
    parsed = model(city="Baltimore")
    assert parsed.country_code == "" and parsed.limit == 1
    with pytest.raises(ValueError):
        model()  # city is required
    schema = model.model_json_schema()
    # Optional arguments keep plain defaults rather than nullable unions (OpenAI compatibility).
    country = schema["properties"]["country_code"]
    assert country["type"] == "string" and country["default"] == ""
    assert "anyOf" not in country


async def test_discovers_four_tools_over_mcp_and_returns_compact_json(weather):
    weather("baseline_mild")
    client = in_memory_mcp_client()
    try:
        tools = await get_langchain_tools(client)
        assert [tool.name for tool in tools] == list(EXPECTED_TOOLS)
        geocode = next(tool for tool in tools if tool.name == "geocode_city")
        text = await geocode.ainvoke({"city": "Baltimore, MD"})
        assert "\n" not in text  # MCP's indented JSON is compacted to save tokens
        assert json.loads(text)["name"] == "Baltimore"
    finally:
        await client.aclose()


async def test_provider_errors_cross_the_mcp_boundary_with_their_category(weather):
    weather("baseline_mild:owm_down")
    client = in_memory_mcp_client()
    try:
        with pytest.raises(ToolException) as caught:
            await client.call_tool("get_forecast", {"lat": 1.0, "lon": 2.0})
        assert "[provider_unavailable]" in str(caught.value)
    finally:
        await client.aclose()


def test_subprocess_receives_only_weather_settings(monkeypatch):
    monkeypatch.setenv("OPENWEATHERMAP_API_KEY", "owm-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-not-forward")
    params = weather_server_params()
    assert params.env == {"OPENWEATHERMAP_API_KEY": "owm-test"}
    assert params.args == ["-m", "mcp_server.weather_server"]
    assert params.cwd == str(PROJECT_ROOT)


async def test_stdio_subprocess_is_reused_concurrently_and_shut_down(monkeypatch):
    """Real FastMCP subprocess over stdio, offline via synthetic weather fixtures."""
    monkeypatch.setenv("CLEARCAST_WEATHER_FIXTURES", "baseline_mild")
    client = MCPToolClient()
    try:
        tools = await get_langchain_tools(client)
        assert {tool.name for tool in tools} == set(EXPECTED_TOOLS)
        geo = json.loads(await client.call_tool("geocode_city", {"city": "Austin, TX"}))
        calls = [
            client.call_tool(name, {"lat": geo["lat"], "lon": geo["lon"]})
            for name in ("get_forecast", "get_current_weather", "get_air_quality") * 3
        ]
        results = await asyncio.gather(*calls)
        assert all(json.loads(result)["source"] == "fixture:baseline_mild" for result in results)
        assert client.connect_count == 1  # one subprocess served every call
    finally:
        await client.aclose()
    assert not client.connected


class _FlakySession:
    def __init__(self, fail_first: bool, timeout: bool = False) -> None:
        self.fail_first = fail_first
        self.timeout = timeout
        self.calls = 0

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.calls += 1
        if self.timeout:
            raise McpError(ErrorData(code=408, message="timed out"))
        if self.fail_first and self.calls == 1:
            raise anyio.ClosedResourceError
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text='{"ok": true}')])


def _factory(sessions: list):
    @asynccontextmanager
    async def factory():
        session = sessions.pop(0)
        yield session

    return factory


async def test_broken_session_is_reconnected_once():
    first, second = _FlakySession(fail_first=True), _FlakySession(fail_first=False)
    client = MCPToolClient(session_factory=_factory([first, second]))
    try:
        assert json.loads(await client.call_tool("get_forecast", {})) == {"ok": True}
        assert client.connect_count == 2
        assert first.calls == 1 and second.calls == 1
    finally:
        await client.aclose()


async def test_tool_timeouts_are_reported_as_tool_errors():
    client = MCPToolClient(session_factory=_factory([_FlakySession(fail_first=False, timeout=True)]))
    try:
        with pytest.raises(ToolException, match=r"\[tool_timeout\]"):
            await client.call_tool("get_forecast", {})
    finally:
        await client.aclose()


async def test_startup_failure_is_service_unavailable():
    @asynccontextmanager
    async def broken():
        raise OSError("cannot spawn")
        yield  # pragma: no cover

    from agent.errors import ToolServiceUnavailableError

    client = MCPToolClient(session_factory=broken)
    with pytest.raises(ToolServiceUnavailableError):
        await client.start()
