"""Graph construction, context selection, error handling, and race-free lazy initialisation."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException

from agent.errors import ConfigurationError, ServiceUnavailableError
from agent.graph import RECURSION_LIMIT, graph_config, handle_tool_error, select_context_messages, thread_id_for
from agent.runtime import AgentRuntime
from evaluation.fake_openai import ScriptedOpenAI


def test_session_threads_are_distinct_and_never_shared_defaults():
    assert thread_id_for("abc") != thread_id_for("abd")
    with pytest.raises(ValueError):
        thread_id_for("")
    config = graph_config("session-1234567890ab")
    assert config["configurable"]["thread_id"] == "session:session-1234567890ab"
    assert config["recursion_limit"] == RECURSION_LIMIT == 10


def test_context_keeps_earlier_answers_but_drops_earlier_tool_traffic():
    call = {"id": "c1", "name": "get_forecast", "args": {}}
    messages = [
        HumanMessage("brief one", id="t1"),
        AIMessage("", tool_calls=[call]),
        ToolMessage("{...}", tool_call_id="c1", name="get_forecast"),
        AIMessage("notes one"),
        HumanMessage("brief two", id="t2"),
        AIMessage("", tool_calls=[{**call, "id": "c2"}]),
        ToolMessage("{...}", tool_call_id="c2", name="get_forecast"),
    ]
    context = select_context_messages(messages)
    assert [m.content for m in context] == ["brief one", "notes one", "brief two", "", "{...}"]
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "c1" for m in context)


def test_tool_error_handler_returns_errors_but_reraises_bugs():
    assert handle_tool_error(ToolException("[provider_timeout] slow")) == "Error: [provider_timeout] slow"
    with pytest.raises(TypeError):
        handle_tool_error(TypeError("programming error"))


async def test_concurrent_first_requests_build_the_graph_once():
    calls = 0

    async def tools_factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return []

    fake = ScriptedOpenAI()
    runtime = AgentRuntime(
        llm_factory=fake.chat_model, drafter_llm_factory=fake.chat_model, tools_factory=tools_factory, required_env=()
    )
    graphs = await asyncio.gather(*(runtime.get_graph() for _ in range(5)))
    assert calls == 1
    assert all(graph is graphs[0] for graph in graphs)
    assert runtime.readiness()["ready"] is True


async def test_missing_configuration_is_reported_by_name_only(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENWEATHERMAP_API_KEY", raising=False)
    runtime = AgentRuntime()
    with pytest.raises(ConfigurationError) as caught:
        await runtime.get_graph()
    assert "OPENAI_API_KEY" in str(caught.value) and "OPENWEATHERMAP_API_KEY" in str(caught.value)
    readiness = runtime.readiness()
    assert readiness["ready"] is False and readiness["status"] == "misconfigured"
    assert readiness["missing_configuration"] == ["OPENAI_API_KEY", "OPENWEATHERMAP_API_KEY"]


async def test_fixture_mode_does_not_require_a_weather_key(monkeypatch):
    monkeypatch.setenv("CLEARCAST_WEATHER_FIXTURES", "baseline_mild")
    assert AgentRuntime().missing_configuration() == ["OPENAI_API_KEY"]


async def test_initialisation_failure_is_friendly_and_retried_after_cooldown():
    attempts = 0

    async def flaky_tools():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("subprocess exploded with secret details")
        return []

    fake = ScriptedOpenAI()
    runtime = AgentRuntime(
        llm_factory=fake.chat_model,
        drafter_llm_factory=fake.chat_model,
        tools_factory=flaky_tools,
        required_env=(),
        retry_cooldown_seconds=0.05,
    )
    with pytest.raises(ServiceUnavailableError) as caught:
        await runtime.get_graph()
    assert "secret" not in str(caught.value)
    assert runtime.readiness()["status"] == "failed"
    with pytest.raises(ServiceUnavailableError, match="restarting"):
        await runtime.get_graph()  # inside the cooldown window
    await asyncio.sleep(0.06)
    assert await runtime.get_graph() is not None
    assert runtime.readiness()["status"] == "ready"
