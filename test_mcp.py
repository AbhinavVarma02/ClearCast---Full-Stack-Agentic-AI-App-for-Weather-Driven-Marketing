"""Standalone test for the MCP Weather Server.

Run this to verify the MCP server works before connecting the LangGraph agent.
Usage: python test_mcp.py
This script supports the README's "Test MCP Server Standalone" section.
"""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Server params follow the Week 6 subprocess-spawning pattern. Module mode ensures
# Python resolves imports correctly within the package.
SERVER_PARAMS = StdioServerParameters(
    command="python", args=["-m", "mcp_server.weather_server"]
)


async def test() -> None:
    """List all MCP tools and geocode Baltimore as an integration check."""
    print("Connecting to MCP Weather Server...")
    async with stdio_client(SERVER_PARAMS) as streams:
        async with ClientSession(*streams) as session:
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
