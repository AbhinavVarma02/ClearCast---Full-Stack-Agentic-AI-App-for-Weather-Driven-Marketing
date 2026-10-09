"""MCP-to-LangChain bridge for ClearCast weather tools.

Discovers MCP tools at runtime and exposes them as LangChain ``StructuredTool``
objects whose argument schemas are Pydantic models generated from each tool's
JSON Schema. :class:`MCPToolClient` keeps one long-lived stdio session (one MCP
subprocess) that concurrent requests share; the one-shot helpers remain for
scripts such as ``test_mcp.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import Field, create_model

from agent.errors import ToolServiceUnavailableError

logger = logging.getLogger("clearcast.mcp")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = ("geocode_city", "get_current_weather", "get_forecast", "get_air_quality")
# Least privilege: only the weather key and offline-mode settings reach the
# subprocess. The OpenAI key is not needed by the weather tools.
FORWARDED_ENV_VARS = (
    "OPENWEATHERMAP_API_KEY",
    "CLEARCAST_WEATHER_FIXTURES",
    "CLEARCAST_WEATHER_CACHE_TTL_SECONDS",
    "PYTHONPATH",
)
REQUEST_TIMEOUT_CODE = 408
_BROKEN_TRANSPORT_ERRORS = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    BrokenPipeError,
    ConnectionError,
)


def weather_server_params() -> StdioServerParameters:
    """Launch the weather server in module mode from the project root."""
    env = {name: value for name in FORWARDED_ENV_VARS if (value := os.getenv(name))}
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.weather_server"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def _errlog():
    """Return a real stream for subprocess stderr (pytest replaces sys.stderr)."""
    for stream in (sys.stderr, sys.__stderr__):
        try:
            stream.fileno()
            return stream
        except (AttributeError, OSError, ValueError):
            continue
    return sys.__stderr__


@asynccontextmanager
async def stdio_session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """Start the weather server subprocess and yield an initialised MCP session."""
    async with stdio_client(params, errlog=_errlog()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _result_text(tool_name: str, result: Any) -> str:
    """Join MCP text blocks, compacting JSON to save model context tokens."""
    content = "\n".join(block.text for block in result.content if hasattr(block, "text"))
    if result.isError:
        # Raised outside the MCP transport context so cleanup cannot wrap it
        # inside an exception group; ToolNode turns it into an error message.
        raise ToolException(content or f"MCP tool '{tool_name}' failed")
    try:
        return json.dumps(json.loads(content), separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        return content


class MCPToolClient:
    """One persistent MCP stdio session shared by all requests on an event loop.

    The stdio transport and ``ClientSession`` are async context managers that
    must be entered and exited by the same task, so a background task owns
    them for the session's lifetime. Callers share the session concurrently
    (MCP multiplexes requests by JSON-RPC id). A broken subprocess is restarted
    once per call; ``aclose`` shuts the subprocess down cleanly.
    """

    def __init__(
        self,
        params: StdioServerParameters | None = None,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[ClientSession]] | None = None,
        call_timeout_seconds: float = 60.0,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        params = params or weather_server_params()
        self._session_factory = session_factory or (lambda: stdio_session(params))
        self._call_timeout = call_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._error: BaseException | None = None
        self.connect_count = 0

    @property
    def connected(self) -> bool:
        return self._session is not None and self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            if not self.connected:
                await self._start_locked()

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _run(self, stop: asyncio.Event, ready: asyncio.Event) -> None:
        try:
            async with self._session_factory() as session:
                self._session = session
                ready.set()
                await stop.wait()
        except Exception as exc:  # includes ExceptionGroup raised by anyio task groups
            self._error = exc
            logger.warning(
                "mcp session ended with an error",
                extra={"event": "mcp.session.error", "error_type": type(exc).__name__},
            )
        finally:
            self._session = None
            ready.set()

    async def _start_locked(self) -> None:
        await self._close_locked()
        stop, ready = asyncio.Event(), asyncio.Event()
        self._stop, self._error = stop, None
        self._task = asyncio.create_task(self._run(stop, ready), name="clearcast-mcp-session")
        try:
            await asyncio.wait_for(ready.wait(), self._startup_timeout)
        except TimeoutError:
            await self._close_locked()
            raise ToolServiceUnavailableError("The weather tool service did not start in time.") from None
        if self._session is None:
            await self._close_locked()
            raise ToolServiceUnavailableError("The weather tool service could not be started.")
        self.connect_count += 1
        logger.info(
            "mcp session started",
            extra={"event": "mcp.session.start", "connect_count": self.connect_count},
        )

    async def _close_locked(self) -> None:
        task, stop = self._task, self._stop
        self._task = self._stop = None
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), 10)
        except Exception:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self._session = None
        logger.info("mcp session closed", extra={"event": "mcp.session.stop"})

    async def _ensure_session(self) -> ClientSession:
        if not self.connected:
            await self.start()
        session = self._session
        if session is None:
            raise ToolServiceUnavailableError("The weather tool service is not connected.")
        return session

    async def _reconnect(self, broken: ClientSession) -> None:
        async with self._lock:
            # Another caller may already have replaced the broken session.
            if self._session is broken or not self.connected:
                await self._start_locked()

    async def list_tools(self) -> list:
        session = await self._ensure_session()
        result = await asyncio.wait_for(session.list_tools(), self._call_timeout)
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call one tool on the shared session; reconnect once if the subprocess died."""
        for attempt in (1, 2):
            session = await self._ensure_session()
            try:
                result = await session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=self._call_timeout),
                )
            except McpError as exc:
                if exc.error.code == REQUEST_TIMEOUT_CODE:
                    raise ToolException(f"[tool_timeout] MCP tool '{tool_name}' did not respond in time") from None
                raise ToolException(f"[tool_protocol_error] MCP tool '{tool_name}' failed") from None
            except _BROKEN_TRANSPORT_ERRORS:
                if attempt == 2:
                    raise ToolServiceUnavailableError("The weather tool service stopped responding.") from None
                logger.warning(
                    "mcp transport broken; reconnecting",
                    extra={"event": "mcp.session.reconnect", "tool": tool_name},
                )
                await self._reconnect(session)
                continue
            return _result_text(tool_name, result)
        raise AssertionError("unreachable")  # pragma: no cover


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call one weather tool through a one-shot MCP session (scripts and sync callers)."""
    async with stdio_session(weather_server_params()) as session:
        result = await session.call_tool(tool_name, arguments)
    return _result_text(tool_name, result)


def _build_args_schema(tool: Any) -> type:
    """Convert an MCP tool's JSON Schema into a Pydantic model."""
    # Converting MCP's JSON Schema to a Pydantic model so StructuredTool can validate arguments
    schema = tool.inputSchema
    required = set(schema.get("required", []))
    type_map = {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    fields = {}
    for name, definition in schema.get("properties", {}).items():
        python_type = type_map.get(definition.get("type"), Any)
        description = definition.get("description", "")
        if name in required:
            fields[name] = (python_type, Field(..., description=description))
        else:
            # Keep plain defaults (not nullable unions) so OpenAI tool schemas stay simple.
            default = definition.get("default", None)
            fields[name] = (python_type, Field(default, description=description))

    model_name = "".join(part.title() for part in tool.name.split("_")) + "Args"
    return create_model(model_name, **fields)


def _make_tool_func(tool_name: str):
    """Create the synchronous callable required by StructuredTool (one-shot session)."""

    def call_tool(**kwargs) -> str:
        return asyncio.run(call_mcp_tool(tool_name, kwargs))

    call_tool.__name__ = tool_name
    return call_tool


def _make_tool_coroutine(tool_name: str, client: MCPToolClient | None):
    """Create the async callable LangGraph uses; it reuses the shared session."""

    async def call_tool(**kwargs) -> str:
        if client is None:
            return await call_mcp_tool(tool_name, kwargs)
        return await client.call_tool(tool_name, kwargs)

    call_tool.__name__ = tool_name
    return call_tool


async def get_langchain_tools(client: MCPToolClient | None = None) -> list[StructuredTool]:
    """Discover MCP tools and convert each one for LangGraph tool calling."""
    if client is not None:
        discovered = await client.list_tools()
    else:
        async with stdio_session(weather_server_params()) as session:
            discovered = (await session.list_tools()).tools

    names = {tool.name for tool in discovered}
    if missing := [name for name in EXPECTED_TOOLS if name not in names]:
        logger.warning(
            "expected MCP tools were not discovered",
            extra={"event": "mcp.discovery.incomplete", "missing_tools": missing},
        )

    # StructuredTool is more reliable than plain Tool because models send
    # structured dictionaries, not JSON strings, and args_schema validates them.
    return [
        StructuredTool(
            name=tool.name,
            description=tool.description or f"Call the {tool.name} MCP tool.",
            func=_make_tool_func(tool.name),
            coroutine=_make_tool_coroutine(tool.name, client),
            args_schema=_build_args_schema(tool),
        )
        for tool in discovered
    ]
