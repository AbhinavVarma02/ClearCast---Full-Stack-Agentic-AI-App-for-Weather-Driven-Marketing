"""Offline harness: real service, graph, MCP server, and validation; fake providers.

* Weather: ``FixtureTransport`` synthetic OpenWeatherMap payloads, parsed by the
  real ``weather_api`` client.
* MCP: the real FastMCP weather server over MCP's in-memory transport (same
  protocol and tool schemas as the stdio subprocess, without a subprocess).
* Model: ``ScriptedOpenAI`` behind the real ``langchain-openai`` ChatOpenAI.
* Clock: fixed, so results never depend on the current date.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mcp.shared.memory import create_connected_server_and_client_session

from agent.runtime import AgentRuntime
from agent.service import CampaignPlanningService
from agent.weather_client import MCPToolClient
from evaluation.fake_openai import ScriptedOpenAI
from mcp_server import weather_api, weather_server
from mcp_server.fixtures import FixtureTransport

# Thursday 2030-04-04 01:30 UTC (Wednesday 21:30 at the fixtures' UTC-4 offset).
FIXED_NOW = datetime(2030, 4, 4, 1, 30, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_NOW


def install_fixture_weather(spec: str, clock=fixed_clock) -> FixtureTransport:
    """Point the process-wide weather client at synthetic payloads (no network)."""
    transport = FixtureTransport(spec, clock=clock)
    weather_api.set_client(
        weather_api.OpenWeatherMapClient(
            transport=transport,
            clock=clock,
            sleep=lambda _seconds: None,
            source=f"fixture:{spec}",
            require_api_key=False,
        )
    )
    return transport


def in_memory_mcp_client() -> MCPToolClient:
    return MCPToolClient(
        session_factory=lambda: create_connected_server_and_client_session(weather_server.mcp._mcp_server)
    )


def build_service(fake: ScriptedOpenAI, *, clock=fixed_clock, max_repairs: int = 2) -> CampaignPlanningService:
    runtime = AgentRuntime(
        llm_factory=fake.chat_model,
        drafter_llm_factory=lambda: fake.chat_model(temperature=0.2),
        mcp_client=in_memory_mcp_client(),
        required_env=(),
    )
    return CampaignPlanningService(runtime, clock=clock, max_repair_attempts=max_repairs)
