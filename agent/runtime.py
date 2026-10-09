"""Agent runtime: safe, lazy, race-free graph initialisation and readiness.

The compiled graph is built once per process and reused. Initialisation is
guarded by an asyncio lock so concurrent first requests cannot build it
twice, does not call the LLM or weather providers (it only starts the MCP
subprocess and lists tools), and reports a friendly service-unavailable error
instead of crashing the process when it fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable

from langgraph.checkpoint.memory import MemorySaver

from agent.errors import ClearCastError, ConfigurationError, ServiceUnavailableError
from agent.graph import build_graph_from_tools
from agent.llm import DRAFTING_TEMPERATURE, PlanDrafter, create_chat_model, model_name
from agent.weather_client import MCPToolClient, get_langchain_tools

logger = logging.getLogger("clearcast.runtime")

FIXTURE_ENV_VAR = "CLEARCAST_WEATHER_FIXTURES"


def default_required_env() -> tuple[str, ...]:
    required = ["OPENAI_API_KEY"]
    if not (os.getenv(FIXTURE_ENV_VAR) or "").strip():
        required.append("OPENWEATHERMAP_API_KEY")
    return tuple(required)


class AgentRuntime:
    def __init__(
        self,
        *,
        llm_factory: Callable[[], object] | None = None,
        drafter_llm_factory: Callable[[], object] | None = None,
        tools_factory: Callable[[], Awaitable[list]] | None = None,
        mcp_client: MCPToolClient | None = None,
        checkpointer=None,
        required_env: tuple[str, ...] | None = None,
        retry_cooldown_seconds: float = 15.0,
    ) -> None:
        self._llm_factory = llm_factory or create_chat_model
        self._drafter_llm_factory = drafter_llm_factory or (lambda: create_chat_model(temperature=DRAFTING_TEMPERATURE))
        self._tools_factory = tools_factory
        self._required_env = default_required_env() if required_env is None else required_env
        self._cooldown = retry_cooldown_seconds
        self.checkpointer = checkpointer or MemorySaver()
        self.mcp_client: MCPToolClient | None = mcp_client
        self.model = model_name()
        self.drafter: PlanDrafter | None = None
        self.tool_names: list[str] = []
        self.status = "not_started"
        self.detail = "The agent has not been initialised yet."
        self._graph = None
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()

    def missing_configuration(self) -> list[str]:
        return [name for name in self._required_env if not (os.getenv(name) or "").strip()]

    def readiness(self) -> dict:
        return {
            "ready": self._graph is not None,
            "status": self.status,
            "detail": self.detail,
            "model": self.model,
            "tools": list(self.tool_names),
            "missing_configuration": self.missing_configuration(),
        }

    async def _discover_tools(self) -> list:
        if self._tools_factory is not None:
            return await self._tools_factory()
        if self.mcp_client is None:
            self.mcp_client = MCPToolClient()
        await self.mcp_client.start()
        return await get_langchain_tools(self.mcp_client)

    async def get_graph(self):
        if self._graph is not None:
            return self._graph
        async with self._lock:
            if self._graph is not None:
                return self._graph
            missing = self.missing_configuration()
            if missing:
                self.status = "misconfigured"
                self.detail = f"Missing required configuration: {', '.join(missing)}."
                raise ConfigurationError(
                    f"ClearCast is missing required configuration: {', '.join(missing)}. "
                    "Add these secrets to the host environment and restart."
                )
            if self._failed_at is not None and time.monotonic() - self._failed_at < self._cooldown:
                raise ServiceUnavailableError("The agent is restarting after an error. Try again shortly.")
            self.status, self.detail = "initializing", "Starting the MCP weather tools."
            started = time.perf_counter()
            try:
                tools = await self._discover_tools()
                graph = build_graph_from_tools(tools, self._llm_factory(), checkpointer=self.checkpointer)
                self.drafter = PlanDrafter(self._drafter_llm_factory())
            except Exception as exc:
                self._failed_at = time.monotonic()
                self.status, self.detail = "failed", "Agent initialisation failed; it will be retried."
                logger.error(
                    "agent initialisation failed",
                    extra={"event": "runtime.init.failed", "error_type": type(exc).__name__},
                )
                if isinstance(exc, ClearCastError):
                    raise
                raise ServiceUnavailableError("The agent could not be initialised. Try again shortly.") from None
            self._graph, self._failed_at = graph, None
            self.tool_names = [tool.name for tool in tools]
            self.status, self.detail = "ready", "Agent ready."
            logger.info(
                "agent initialised",
                extra={
                    "event": "runtime.init.ready",
                    "tools": self.tool_names,
                    "model": self.model,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return graph

    async def warm_up(self) -> None:
        """Best-effort startup initialisation; failures stay visible via readiness."""
        with contextlib.suppress(ClearCastError):
            await self.get_graph()

    async def aclose(self) -> None:
        if self.mcp_client is not None:
            await self.mcp_client.aclose()
