# ClearCast — Full-Stack Agentic AI App for Weather-Driven Marketing

ClearCast turns live OpenWeatherMap conditions and forecasts into actionable marketing campaign recommendations. A LangGraph agent discovers weather tools through MCP, selects the data it needs, and recommends campaign windows, business-specific reasoning, ad copy, and risk mitigations rather than merely repeating a forecast.

## Architecture

```text
User -> Gradio Frontend -> LangGraph Agent -> MCP Client Bridge -> MCP Server (FastMCP) -> OpenWeatherMap API
```

## Tech Stack

| Component | Technology | Purpose |
|---|---|---|
| Frontend | Gradio Blocks | Campaign input form and Markdown results |
| Agent | LangGraph + LangChain | Stateful LLM/tool-calling loop |
| Model | OpenAI GPT-4o-mini | Weather-to-marketing analysis |
| Tool bridge | MCP Python SDK + StructuredTool | MCP discovery and LangChain schema conversion |
| Weather service | FastMCP + Requests | Documented tools and raw API calls |
| Data source | OpenWeatherMap | Geocoding, weather, forecast, and air quality |
| Configuration | python-dotenv | Local environment variable loading |

## Prerequisites

- Python 3.11+
- [OpenWeatherMap API key](https://home.openweathermap.org/users/sign_up) (free tier)
- [OpenAI API key](https://platform.openai.com/api-keys)
- [LangSmith API key](https://smith.langchain.com/) (optional, for tracing)

## Setup

1. Clone the repository and enter its directory.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`, then replace the placeholders with your API keys.
4. Check configuration metadata without printing key values:

   ```bash
   python safe_env_check.py
   ```

5. Test the MCP server independently:

   ```bash
   python test_mcp.py
   ```

6. Run the full application:

   ```bash
   python -m frontend.app
   ```

## Test MCP Server Standalone

The MCP layer can be tested before starting LangGraph or Gradio. `test_mcp.py` launches `mcp_server.weather_server` in module mode over stdio, initializes an MCP client session, lists all four tools, and calls `geocode_city` with Baltimore.

```bash
python test_mcp.py
```

With a valid OpenWeatherMap key, expected output resembles:

```text
Connecting to MCP Weather Server...

Found 4 tools:
  - geocode_city: Convert a city name to geographic coordinates...
  - get_current_weather: Get current weather conditions...
  - get_forecast: Get a 5-day weather forecast...
  - get_air_quality: Get air quality index...

Testing geocode_city('Baltimore')...
Result: {"lat": 39.2904, "lon": -76.6122, ...}

All MCP server tests passed!
```

## Project Structure

```text
.
├── mcp_server/
│   ├── __init__.py          # Weather MCP package
│   ├── weather_api.py       # Raw OpenWeatherMap HTTP and response cleaning
│   └── weather_server.py    # Four FastMCP weather tools
├── agent/
│   ├── __init__.py          # Agent package
│   ├── weather_client.py    # MCP discovery and StructuredTool conversion
│   ├── prompts.py           # Marketing analyst system prompt
│   └── graph.py             # LangGraph tool-calling loop and memory
├── frontend/
│   ├── __init__.py          # Frontend package
│   └── app.py               # Gradio Blocks application
├── docs/
│   ├── architecture.md      # Layers, state flow, and design decisions
│   ├── prompts.md           # Agentic IDE prompt log template
│   └── deployment.md        # Local and production deployment options
├── .env.example             # Safe configuration template
├── docker-compose.yml       # Deployment discussion configuration
├── requirements.txt         # Python dependencies
├── test_mcp.py              # Standalone MCP integration test
└── README.md                # Setup and project guide
```

## How It Works

The agent begins by asking MCP for its available tools and converts their JSON Schemas into Pydantic-backed LangChain `StructuredTool` objects. For a campaign request, GPT-4o-mini first geocodes the location and then loops through current weather, forecast, and—when relevant—air quality calls. LangGraph's `ToolNode` runs each request and returns the results to the model. The loop ends only when the model produces the requested marketing sections. `MemorySaver` keeps each `thread_id`'s conversation state between invocations.

## Deployment

Locally, the MCP server uses stdio and is spawned as a Python subprocess by the agent. In production, the agent and MCP server can remain together in one container so stdio still works, or the MCP server can move to an HTTP transport for independent deployment and scaling. See [docs/deployment.md](docs/deployment.md) and `docker-compose.yml` for the tradeoffs and an illustrative service layout.
