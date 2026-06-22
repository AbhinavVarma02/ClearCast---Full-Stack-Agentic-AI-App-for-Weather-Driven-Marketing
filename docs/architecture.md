# ClearCast Architecture

ClearCast uses four application layers plus an external weather provider. Each layer has one reason to change, which keeps the weather integration independently testable and makes the UI, model, and API provider replaceable.

## Four Layers

1. **Frontend — `frontend/app.py`.** Gradio validates campaign fields, packages them into one structured user request, and renders the Markdown response. It has no weather or agent logic.
2. **Agent — `agent/graph.py` and `agent/prompts.py`.** LangGraph owns conversation state, tool selection, looping, and the marketing-analysis instructions. It depends only on LangChain tools, not OpenWeatherMap.
3. **MCP client bridge — `agent/weather_client.py`.** The bridge starts the stdio server, discovers MCP tool metadata, translates JSON Schema to Pydantic models, and exposes `StructuredTool` instances to LangGraph. This protocol boundary allows another compatible MCP server to replace the current one.
4. **MCP server — `mcp_server/weather_server.py` and `weather_api.py`.** FastMCP exposes a thin set of documented tools. The raw API module alone handles HTTP, authentication, response parsing, and useful error messages, so it can be tested without MCP.

The agent does not need to know OpenWeatherMap endpoints, authentication, or response formats. It only sees documented tools.

## Tool-Calling Loop

```text
                         tool call requested
START -> chatbot --------------------------------> ToolNode
           ^                                          |
           |               tool result                |
           +------------------------------------------+
           |
           +---- no tool call -> final response -> END
```

At startup, the bridge lists tools from FastMCP and creates a validated LangChain tool for each schema. During a request, the chatbot decides which tool to use. `tools_condition` routes a tool call to `ToolNode`; after execution, the result returns to the chatbot. A typical path is geocoding, forecast, current conditions, and optionally air quality before the model writes its campaign analysis.

## State Flow

The graph state contains a `messages` list annotated with LangGraph's `add_messages` reducer. Nodes return only their new messages; the reducer appends or updates them instead of discarding the conversation. This preserves user requests, assistant tool calls, and tool results through every super-step.

`MemorySaver` checkpoints state under `configurable.thread_id`, so repeated invocations with the same identifier share history and different identifiers remain isolated. It is appropriate for the local demo; a persistent checkpointer such as `PostgresSaver` is the production replacement.

## Separation Benefits

- The MCP server can be launched and tested independently with `test_mcp.py`.
- Weather response-shape changes are isolated to `weather_api.py`.
- Prompt and campaign strategy can evolve without touching transport code.
- Gradio can be swapped for another client without changing tools or the graph.
- OpenWeatherMap could be replaced behind the same documented MCP contracts.
