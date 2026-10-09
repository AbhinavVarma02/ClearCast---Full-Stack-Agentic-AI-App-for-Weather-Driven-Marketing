"""Shared fixtures. Every default test runs offline: no OpenAI or OpenWeatherMap calls."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from evaluation.fake_openai import ScriptedOpenAI
from evaluation.harness import build_service, install_fixture_weather
from mcp_server import weather_api


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove real credentials and tracing so tests can never reach providers."""
    for name in list(os.environ):
        if name.startswith(("OPENAI_", "LANGCHAIN_", "LANGSMITH_")) or name in {
            "OPENWEATHERMAP_API_KEY",
            "CLEARCAST_WEATHER_FIXTURES",
        }:
            monkeypatch.delenv(name, raising=False)
    yield
    weather_api.set_client(None)


@pytest.fixture
def weather():
    """Install synthetic weather for the in-process MCP server; returns the transport."""

    def install(spec: str = "baseline_mild"):
        return install_fixture_weather(spec)

    return install


@pytest.fixture
async def make_service():
    """Build services on the offline harness and close their MCP sessions afterwards."""
    services = []

    def factory(fake: ScriptedOpenAI | None = None, **kwargs):
        service = build_service(fake or ScriptedOpenAI(), **kwargs)
        services.append(service)
        return service

    yield factory
    for service in services:
        await service.aclose()
