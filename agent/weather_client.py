"""MCP-to-LangChain bridge for ClearCast weather tools.

adapted to discover MCP tools and expose them as LangChain StructuredTool objects.
"""

import asyncio
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, create_model

# Module mode lets the subprocess resolve package imports from the project root.
WEATHER_SERVER_PARAMS = StdioServerParameters(
    command="python", args=["-m", "mcp_server.weather_server"]
)


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call one weather tool through MCP and return its text content."""
    # Each call opens a fresh connection. Fine for a demo; production would use persistent connections.
    async with stdio_client(WEATHER_SERVER_PARAMS) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    content = "\n".join(
        block.text for block in result.content if hasattr(block, "text")
    )
    # Raise after the MCP context closes so transport cleanup cannot wrap this
    # useful tool error inside an exception group.
    if result.isError:
        raise RuntimeError(content or f"MCP tool '{tool_name}' failed")
    return content


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
    }
    fields = {}
    for name, definition in schema.get("properties", {}).items():
        python_type = type_map.get(definition.get("type"), Any)
        description = definition.get("description", "")
        if name in required:
            fields[name] = (python_type, Field(..., description=description))
        else:
            fields[name] = (python_type | None, Field(None, description=description))

    model_name = "".join(part.title() for part in tool.name.split("_")) + "Args"
    return create_model(model_name, **fields)


def _make_tool_func(tool_name: str):
    """Create the synchronous callable required by StructuredTool."""
    # StructuredTool expects a sync function; we wrap the async MCP call
    def call_tool(**kwargs) -> str:
        return asyncio.run(call_mcp_tool(tool_name, kwargs))

    call_tool.__name__ = tool_name
    return call_tool


async def get_langchain_tools() -> list[StructuredTool]:
    """Discover MCP tools and convert each one for LangGraph tool calling."""
    async with stdio_client(WEATHER_SERVER_PARAMS) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            discovered = await session.list_tools()

    # StructuredTool is more reliable than plain Tool because models send
    # structured dictionaries, not JSON strings, and args_schema validates them.
    tools = [
        StructuredTool(
            name=tool.name,
            description=tool.description or f"Call the {tool.name} MCP tool.",
            func=_make_tool_func(tool.name),
            args_schema=_build_args_schema(tool),
        )
        for tool in discovered.tools
    ]
    # These tools can be passed directly to llm.bind_tools() in the LangGraph agent
    return tools
