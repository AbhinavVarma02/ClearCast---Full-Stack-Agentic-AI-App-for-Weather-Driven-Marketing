# ClearCast Project Report

## 1. Project Overview

ClearCast is a weather-aware campaign planning application. I built it to answer a practical marketing question: not just *what will the weather be?*, but *when should a business run a campaign, what should it say, and what could go wrong?*

The project combines a Gradio interface, a LangGraph agent, an MCP weather server, and live OpenWeatherMap data. The language model used by the current implementation is `gpt-4o-mini`. Each layer has a focused job, so the user interface does not need to understand weather APIs and the agent does not need to know provider-specific endpoints or response formats.

[Screenshot: Cursor project folder structure]

The screenshot should show the main `frontend`, `agent`, `mcp_server`, and `docs` folders, along with the root test, Docker, requirements, and README files. Local environment files and secret values should not be visible.

## 2. What ClearCast Does

The user enters a location, business type, campaign goal, and preferred ad-copy tone. ClearCast then:

1. resolves the location to latitude and longitude;
2. retrieves the five-day forecast and current conditions;
3. retrieves air-quality data when outdoor activity is relevant;
4. interprets the conditions from a marketing perspective; and
5. returns recommended campaign windows, reasoning, ad copy, risks, and a forecast table.

The important part is the interpretation step. A forecast alone is easy to find. ClearCast connects conditions such as rain, heat, wind, or a clear weekend with likely customer behavior for the specific business in the brief.

[Screenshot: ClearCast home screen]

The screenshot should show the campaign brief form, the empty report panel, example campaigns, and the four-step workflow on the page.

[Screenshot: Generated campaign strategy output]

The screenshot should show a completed report with campaign windows, weather-driven reasoning, two or three ad-copy options, risk notes, and the forecast summary table. The location and campaign brief should remain visible if possible.

## 3. Architecture

The application is split into four main layers:

```text
User
  -> Gradio frontend
  -> LangGraph agent
  -> MCP client bridge
  -> FastMCP weather server
  -> OpenWeatherMap API
```

- **Frontend (`frontend/app.py`)** validates the form, builds a structured request, invokes the graph, and renders Markdown.
- **Agent (`agent/graph.py` and `agent/prompts.py`)** manages message state, model calls, tool selection, and the final marketing format.
- **MCP bridge (`agent/weather_client.py`)** starts the local stdio server, discovers its tools, converts their JSON Schemas into Pydantic argument models, and exposes them as LangChain `StructuredTool` objects.
- **Weather service (`mcp_server/weather_server.py` and `weather_api.py`)** defines the MCP contracts and handles OpenWeatherMap requests, validation, response cleanup, and provider errors.

This separation keeps provider details out of the graph. It also means the interface, model, or weather provider can be changed without rewriting the whole application.

## 4. MCP Server and Weather Tools

The FastMCP server exposes four tools over stdio:

| Tool | Purpose |
|---|---|
| `geocode_city` | Converts a city and optional country code into coordinates. |
| `get_current_weather` | Returns current temperature, feels-like temperature, humidity, wind, conditions, and cloud cover. |
| `get_forecast` | Returns a five-day forecast in three-hour blocks, including temperature, rain probability, rain volume, wind, and conditions. |
| `get_air_quality` | Returns AQI plus PM2.5, PM10, and ozone readings. |

The raw API layer trims OpenWeatherMap responses to fields that are useful to the campaign analysis. This reduces unnecessary context sent back to the model and gives the agent a more stable data shape.

The bridge discovers tool names, descriptions, and input schemas at startup rather than maintaining a second hard-coded tool list. Each tool call currently opens a fresh stdio connection and starts the weather server as a Python subprocess. That is simple and appropriate for a local demo, although a persistent session would reduce overhead in production.

[Screenshot: MCP test showing four tools]

The screenshot should show `test_mcp.py` connecting to the server, discovering all four tool names, and completing the Baltimore geocoding check. Only use a screenshot from an actual successful run.

## 5. LangGraph Agent Flow

The graph uses a small tool-calling loop:

```text
START -> chatbot -> tools -> chatbot
             |                 |
             +---- final ------+-> END
```

At graph construction time, the MCP tools are discovered and bound to `gpt-4o-mini`. For each request, the chatbot receives a system prompt plus the accumulated message history. If the model requests a tool, LangGraph routes the call to `ToolNode` and returns the result to the chatbot. When the model responds without another tool call, the graph ends and the final Markdown is returned to Gradio.

The `messages` state uses LangGraph's `add_messages` reducer, so user messages, model tool calls, and tool results remain available through the loop. `MemorySaver` checkpoints this state by `thread_id`. This is useful for the local demonstration, but it is in-memory only and does not survive a restart.

The system prompt asks the agent to geocode first, reuse the returned coordinates, retrieve forecast and current conditions, and call air quality only when outdoor activity is relevant. It also requires five consistent output sections: campaign windows, weather reasoning, suggested copy, risk notes, and a forecast summary.

[Screenshot: Cursor Agent prompt history]

The screenshot should show the real development prompts and revisions used for the MCP server, graph, frontend, or documentation. The current `docs/prompts.md` file is a template, so the screenshot should only be included when the actual history is available.

## 6. Frontend Experience

The frontend is a Gradio Blocks application with a responsive, dark campaign-dashboard design. The brief collects:

- city, state, or country;
- business type;
- campaign goal; and
- a Friendly, Urgent, Playful, or Premium tone.

Location and business type are required. If the campaign goal is blank, the request uses “General awareness.” The response panel renders the agent's Markdown directly, including tables. Four example briefs are included to make the app easy to demonstrate.

The page also explains the workflow and displays the technology path from OpenWeatherMap through MCP and LangGraph to Gradio. On request failure, the UI returns the exception type and a short configuration message rather than displaying a detailed exception that might contain sensitive information.

## 7. Safety and Secret Handling

Secrets are loaded from the project-root `.env` file at runtime and are not placed in prompts or tool results. The repository's `.gitignore` excludes `.env`, and `.dockerignore` also excludes it so a local secret file is not copied by the Docker build context.

The weather wrapper validates that the OpenWeatherMap key exists and does not look like placeholder text. Network errors are converted to a generic message because request exception strings can contain a full URL, including the key passed to OpenWeatherMap. The frontend also avoids echoing raw exception details.

`safe_env_check.py` is a local diagnostic that reports metadata such as presence, length, whitespace, duplicate assignments, and placeholder status. It is designed not to print the key value. It should still be treated as a local administrative check rather than exposed as a public endpoint.

[Screenshot: safe_env_check.py output]

The screenshot should show only the diagnostic labels and safe metadata. Review it before submission to confirm that no credential value, terminal history, or unrelated environment variable is visible.

For deployment, credentials should be injected through the hosting platform or a managed secret store. They should not be baked into an image, committed to Git, included in screenshots, or written to application logs.

## 8. Loop and Cost Controls

The project includes several controls to keep the agent bounded:

- `recursion_limit` is set to 10 for each graph invocation, preventing an open-ended chatbot/tool loop.
- The system prompt tells the model to call each tool no more than once per request unless that tool returns an explicit error.
- Air quality is conditional instead of being fetched for every campaign.
- Weather responses are reduced to relevant fields before they enter model context.
- `gpt-4o-mini` is used for the reasoning and tool-selection loop.

These are sensible demo safeguards, but they are not a complete production budget system. There is no per-user quota, token ceiling, cost ledger, response cache, model timeout, or application-level rate limiter yet. The prompt-level “call once” rule is also guidance rather than a hard counter. Production work should enforce tool-call budgets in code and track tokens, latency, and cost through tracing and metrics.

## 9. Testing and Verification

The repository contains `test_mcp.py`, a standalone integration check that starts the MCP server over stdio, initializes a client session, lists the available tools, and calls `geocode_city` for Baltimore. It is useful for isolating the weather tool layer before involving LangGraph or Gradio.

For this documentation pass, all Python files in `agent`, `frontend`, and `mcp_server`, plus the two root scripts, completed Python bytecode compilation without errors. The project virtual environment also collected one pytest test from `test_mcp.py`. The live integration call was not run as part of this pass, so this report does not claim a successful OpenWeatherMap response or end-to-end model result.

Before submission or deployment, the practical verification sequence is:

1. run `python safe_env_check.py` locally and confirm that only safe metadata appears;
2. run `python test_mcp.py` and confirm that all four tools are listed and geocoding succeeds;
3. run `python -m frontend.app` and submit at least one brief;
4. confirm the report contains every required section and uses real forecast data;
5. test empty required fields and an invalid location;
6. confirm errors do not reveal request URLs, keys, or raw environment values; and
7. test the responsive layout at desktop and narrow widths.

## 10. Deployment Plan

The cleanest first deployment is a single application container containing Gradio, the agent, MCP bridge, and MCP server. This preserves the current stdio communication model and avoids introducing a network boundary between the agent and its tools. The root `Dockerfile` follows this direction and exposes Gradio on port 7860.

The current `docker-compose.yml` is a discussion scaffold, not a complete runnable split deployment. Its separate frontend service would need its own build definition and an HTTP API to reach the agent backend. For the present code, the frontend invokes the graph directly, so the frontend and agent must share the same Python environment.

A production rollout should:

1. keep the first release in one container;
2. inject API credentials from the platform's secret manager;
3. add a health check, structured logs, request timeouts, and resource limits;
4. replace `MemorySaver` with a durable checkpointer such as PostgreSQL if conversation history is required;
5. set a unique thread identifier per user or session; and
6. add LangSmith or equivalent tracing for graph steps, tool latency, errors, tokens, and cost.

If tool traffic grows independently, the MCP server can later move to streamable HTTP and become a separate authenticated service. That change would require network access controls, service discovery, retry policy, and observability around the new boundary.

## 11. Limitations and Future Improvements

The current build is a strong local demonstration, with several clear next steps:

- **Live dependencies:** Useful output requires both OpenWeatherMap and OpenAI to be available and correctly configured.
- **Short forecast horizon:** OpenWeatherMap's endpoint provides five days in three-hour blocks, so ClearCast is not a long-range campaign planner.
- **In-memory state:** `MemorySaver` loses history on restart and is not shared across replicas.
- **Shared default thread:** `invoke_graph` defaults to thread ID `"1"`; a multi-user deployment needs unique session IDs to prevent histories from mixing.
- **Prompt-enforced tool budget:** The one-call rule should be backed by code-level counters.
- **Subprocess overhead:** A fresh MCP stdio connection is opened for each tool call. Reusing a client session would improve latency.
- **Limited automated coverage:** The repository has an MCP integration check, but it does not yet include unit tests for response cleaning, schema conversion, graph routing, frontend validation, or secret-safe error handling.
- **No fallback provider or cache:** Provider outages and repeated identical requests currently have no fallback path.
- **Model variability:** Campaign recommendations are generated text and should be reviewed by a person before media spend is committed.

Future versions could add provider response mocks for deterministic tests, persistent MCP sessions, hard usage budgets, cached forecast data, user/session management, saved campaign reports, downloadable exports, and comparison of multiple locations or campaign windows. The same MCP boundary also leaves room for additional tools such as severe-weather alerts, historical conditions, local events, or first-party campaign performance data.
