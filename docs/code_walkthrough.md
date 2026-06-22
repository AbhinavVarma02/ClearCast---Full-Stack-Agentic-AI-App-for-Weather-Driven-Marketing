# ClearCast Code Walkthrough

ClearCast follows one clear path from input to output. The user enters campaign details in Gradio, and the frontend sends one structured request to the LangGraph agent. The agent decides which MCP weather tools it needs. The MCP server calls OpenWeatherMap through a small API wrapper, and the wrapper cleans the weather response before returning it. LangGraph uses that data to generate campaign timing, reasoning, ad copy, and risk notes, and Gradio displays the finished strategy as Markdown.

## File-by-File Walkthrough

### `frontend/app.py`

This is the user-facing application and the best place to begin the demo. It builds the Gradio form, validates the location and business type, combines the form fields into a structured campaign request, and displays the returned Markdown. It exists to keep interface concerns separate from weather and agent logic. The button's click handler calls `process_request`, which invokes the already-built LangGraph graph once and passes the result into the report panel.

### `agent/graph.py`

This file coordinates the agent. It discovers the MCP tools, binds them to `gpt-4o-mini`, stores the conversation in a LangGraph message state, and connects a chatbot node to a `ToolNode`. `tools_condition` decides whether the model needs another tool call or is ready to finish. This file exists so tool selection and state flow are controlled in one place. It receives the frontend request, calls tools supplied by `weather_client.py`, and returns the last agent message to Gradio. The invocation has a recursion limit of 10.

### `agent/prompts.py`

This file contains the system prompt that defines ClearCast as a weather-driven marketing planner. It tells the model which tool order to use, when air quality is relevant, how to reuse coordinates, and which sections must appear in the final answer. Keeping this prompt separate makes the campaign behavior easier to review and improve without changing the graph. `graph.py` adds this system prompt before each model call, and the model then uses it to choose MCP tools and format the final strategy.

### `agent/weather_client.py`

This is the bridge between MCP and LangGraph. It starts `mcp_server.weather_server` as a Python subprocess over stdio, asks the server which tools are available, converts each MCP JSON Schema into a Pydantic argument model, and wraps the result as a LangChain `StructuredTool`. It exists so the graph can work with standard LangChain tools without knowing MCP transport details. The graph receives the converted tools from this file, and each tool wrapper sends the selected call to the MCP server.

### `mcp_server/weather_server.py`

This file defines the FastMCP server and exposes four tools: `geocode_city`, `get_current_weather`, `get_forecast`, and `get_air_quality`. The docstrings describe when each tool should be used and what arguments it expects. It exists as a clean protocol boundary between the agent and the weather provider. Calls arriving from `weather_client.py` are passed to the matching function in `weather_api.py`.

### `mcp_server/weather_api.py`

This file is the only layer that deals directly with OpenWeatherMap endpoints and response formats. It loads the OpenWeatherMap credential from the project root, validates that it is present and does not look like placeholder text, makes HTTP requests with a timeout, and converts large provider responses into smaller dictionaries for the agent. It exists to isolate authentication, network errors, and provider-specific data cleanup from MCP. Its cleaned geocoding, current weather, forecast, and air-quality results return through the MCP server to the agent.

### `safe_env_check.py`

This is a local configuration diagnostic. It checks safe metadata for the OpenWeatherMap setting, including whether the project environment file exists, whether the setting is present, its length, placeholder status, surrounding spaces, and duplicate assignment lines. It exists to help diagnose configuration without printing the credential itself. It runs independently from the application and should be shown before live testing. The build prompt also requested tracing status, but the current code does not report that; this can be verified in the code.

### `test_mcp.py`

This is a focused integration check for the MCP layer. It starts the weather server over stdio, initializes an MCP client session, lists the discovered tools, and calls `geocode_city` with Baltimore. It exists so the server and its tool contracts can be checked before adding LangGraph and Gradio to the debugging path. A successful run demonstrates that the MCP bridge can see the four tools and that at least the geocoding path reaches OpenWeatherMap.

### `requirements.txt`

This file lists the Python packages needed by the app, including Gradio, LangGraph, LangChain, OpenAI support, MCP, Pydantic, dotenv, HTTP support, and pytest. It exists to make local and container setup repeatable. The Dockerfile installs this list before copying the rest of the project, and local users install the same dependencies before running the tests or frontend.

### `.env.example`

This is the safe configuration template. It documents the setting names needed for OpenWeatherMap and OpenAI, along with optional LangSmith tracing settings, without serving as the live secret file. It exists to show a developer what must be configured locally. The developer copies it to `.env`, supplies real values privately, and the application loads that project-root configuration at runtime.

### `.gitignore`

This file keeps local configuration and generated development files out of version control. It explicitly ignores `.env`, virtual environments, Python caches, pytest output, coverage output, logs, and screenshot assets. It exists to reduce the chance of committing secrets or machine-specific files. The tracked source and documentation can therefore be shared without including the live local configuration.

### `.dockerignore`

This file controls what is excluded from the Docker build context. It leaves out `.env`, Git metadata, virtual environments, caches, IDE folders, logs, build output, and the local knowledge-base folder. It exists because `Dockerfile` uses `COPY . .`; without a careful ignore file, local-only material could be copied into the image. The result is a smaller and safer container build context.

### `Dockerfile`

This file describes the current single-image deployment. It starts from Python 3.11 slim, installs `requirements.txt`, copies the project, exposes port 7860, and launches `python -m frontend.app`. It exists to package the frontend, agent, MCP bridge, and MCP server in the same environment, which fits the current stdio design. When the frontend starts in the container, it can build the graph and spawn the MCP subprocess from the same installed codebase.

### `docker-compose.yml`

This file sketches a future split between an agent backend and a frontend service. It also documents that the current MCP server uses stdio and must share an environment with the code invoking it. It exists mainly for deployment discussion. The present application does not expose an HTTP backend, and there is no separate frontend Docker build in the project, so this two-service layout is not complete or runnable as written; the single-image Dockerfile is closer to the implemented architecture.

### `README.md`

This is the main setup and project guide. It explains the purpose, architecture, technology choices, prerequisites, local commands, MCP test, project structure, agent flow, and deployment direction. It exists as the first reference for someone running or reviewing ClearCast. From the README, a developer is directed to the standalone MCP test, the frontend launch command, and the more detailed documents in `docs`.

### `docs/architecture.md`

This document explains why the frontend, agent, MCP bridge, and weather service are separate layers. It also describes the LangGraph loop, the message reducer, `MemorySaver`, and the benefits of the boundaries. It exists to provide the design reasoning that would be too detailed for the README. During the video, it can support the transition from showing the folder structure to tracing a request through the code.

### `docs/deployment.md`

This document compares local stdio operation, a single application container, and a future independent MCP service using a network transport. It also covers secret injection, persistent memory, and monitoring. It exists to distinguish the working local architecture from possible production designs. It connects the current Dockerfile to the recommended first deployment and explains what must change before the MCP layer can scale separately.

### `docs/prompts.md`

This file records the build prompts in development order, from project structure and weather functions through MCP, LangGraph, safety controls, UI work, documentation, and final review. It exists to show how the project was built in small, reviewable stages rather than as one large change. It is also useful in the video as evidence that the final file structure follows the planned architecture. One prompt asks `safe_env_check.py` to include tracing status, while the current script only checks OpenWeatherMap metadata, so that difference should be acknowledged rather than demonstrated as completed behavior.

## How to Explain the Flow in the Video

1. **Start with the frontend.** Open `frontend/app.py`, point out the four inputs, and show that the button is connected to `process_request`. Explain that the function validates the brief, creates one structured message, and makes one graph invocation.
2. **Move to LangGraph.** Open `agent/graph.py` and explain the message state, chatbot node, `ToolNode`, and conditional loop. Mention that the model can call tools until it has enough information, with a recursion limit of 10.
3. **Explain the MCP client bridge.** Open `agent/weather_client.py` and show how it starts the stdio server, discovers tool definitions, creates validated argument schemas, and turns MCP tools into LangChain tools.
4. **Explain the MCP server.** Open `mcp_server/weather_server.py` and briefly show the four decorated tool functions. Emphasize that this is the contract the agent sees.
5. **Explain the OpenWeatherMap wrapper.** Open `mcp_server/weather_api.py` and show that provider calls, credential validation, HTTP errors, timeouts, and response cleanup are kept in this one layer.
6. **Show safety checks.** Show `.gitignore`, `.dockerignore`, `.env.example`, and `safe_env_check.py`. Explain the controls without opening the live `.env` file or displaying credential values.
7. **Show tests.** Run `test_mcp.py` and explain that it validates MCP tool discovery and one lightweight geocoding call before the full model and frontend are involved. Only describe a passing result if the command actually passes during recording.
8. **Show the deployment plan.** Finish with the root Dockerfile and `docs/deployment.md`. Explain that one container matches the code today, while splitting the MCP server or frontend requires additional transport and API work.

## Important Code Points to Mention

- `.env` is ignored and is not exposed in source control or the Docker build context.
- The OpenWeatherMap key is loaded from the project root and validated without being printed.
- MCP separates the external weather API from the agent.
- LangGraph controls tool calling and routes tool results back to the model.
- The graph recursion limit is set to 10.
- Each frontend button click triggers one `process_request` call and one graph invocation.
- `test_mcp.py` verifies the MCP layer before the full app is tested.
- `safe_env_check.py` checks configuration metadata without printing secrets.

## Checks Before Recording

- Run the MCP test with valid local configuration and capture the output only if all four tools appear and geocoding succeeds.
- Run one complete campaign request and confirm the live response contains every section required by `agent/prompts.py`.
- Keep the `.env` file, terminal history, and all credential values outside the recording frame.
- Do not say that `safe_env_check.py` reports tracing status unless that feature is added; the current script checks only OpenWeatherMap metadata.
- Present the Compose file as a future deployment sketch. The current two-service definition needs a frontend build file and a network API between the frontend and agent before it can run as shown.
- Mention that `MemorySaver` is temporary, process-local storage and that the default thread ID is fixed at `"1"`; both should be changed for a multi-user deployment.
- The project currently has one focused MCP integration test. Broader unit, graph, frontend, and end-to-end tests remain future work.
