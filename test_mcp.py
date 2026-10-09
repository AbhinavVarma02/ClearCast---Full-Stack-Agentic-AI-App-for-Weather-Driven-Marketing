"""Standalone test for the MCP Weather Server.

Run this to verify the MCP server works before connecting the LangGraph agent.
Usage: python test_mcp.py
This script supports the README's "Test MCP Server Standalone" section.

This is an optional LIVE check: it calls OpenWeatherMap with your key, so it is
not part of the default (offline) test suite or CI.
"""

import asyncio

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from agent.weather_client import weather_server_params

# Server params follow the Week 6 subprocess-spawning pattern. Module mode ensures
# Python resolves imports correctly within the package; the shared helper also
# uses this interpreter, the project root, and forwards only the weather key.
SERVER_PARAMS = weather_server_params()


async def test() -> None:
    """List all MCP tools and geocode Baltimore as an integration check."""
    print("Connecting to MCP Weather Server...")
    async with stdio_client(SERVER_PARAMS) as streams, ClientSession(*streams) as session:
        await session.initialize()

        # Listing tools validates the server even before an external API call.
        tools = await session.list_tools()
        print(f"\nFound {len(tools.tools)} tools:")
        for tool in tools.tools:
            print(f"  - {tool.name}: {tool.description[:80]}...")

        print("\nTesting geocode_city('Baltimore')...")
        result = await session.call_tool("geocode_city", {"city": "Baltimore"})
        if result.isError:
            raise RuntimeError(result.content[0].text)
        print(f"Result: {result.content[0].text}")

        print("\nAll MCP server tests passed!")


if __name__ == "__main__":
    asyncio.run(test())
