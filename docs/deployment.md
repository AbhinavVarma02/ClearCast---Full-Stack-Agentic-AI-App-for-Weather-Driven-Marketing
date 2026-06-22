# ClearCast Deployment Notes

## Local Development

The agent starts `python -m mcp_server.weather_server` as a child process and communicates over MCP's stdio transport. This is simple, private to the host, and requires no separate server port. Run the application with `python -m frontend.app`.

## Production Option A: One Application Container

Package the frontend, LangGraph agent, MCP bridge, and MCP server in the same Docker image or process environment. The agent can continue spawning the server over stdio. This is the smallest operational change and a sensible first deployment, although the agent and MCP layer must scale together.

## Production Option B: Separate MCP Service

Convert FastMCP from stdio to a supported SSE or streamable HTTP transport, deploy it as an independently reachable service, and replace the stdio client with a network MCP client. This adds authentication, service discovery, timeouts, and network observability concerns, but it allows weather tooling to scale or deploy separately and serve multiple agents.

## Secrets and Configuration

Do not copy `.env` into an image. Inject `OPENWEATHERMAP_API_KEY`, `OPENAI_API_KEY`, and optional LangSmith values from AWS Secrets Manager, a cloud-provider secret store, or an orchestrator-managed secret. Apply least privilege and rotate credentials independently.

## Persistent Memory

`MemorySaver` is process-local and disappears on restart. Replace it with LangGraph's `PostgresSaver` for durable, shared checkpoints. The graph topology stays unchanged; only checkpointer construction and its database lifecycle need to change.

## Monitoring

Enable LangSmith to inspect graph and tool traces, request latency, failures, token usage, and model cost. Complement it with application logs and service metrics for MCP subprocess failures, OpenWeatherMap status codes, request duration, and Gradio availability. Avoid logging API keys or full sensitive user inputs.

## Compose Reference

The root [`docker-compose.yml`](../docker-compose.yml) illustrates the requested agent-backend and frontend split for deployment discussion. It is intentionally not a complete runnable deployment: production packaging needs Dockerfiles and, if the frontend remains separate, an HTTP API between it and the backend. For the current stdio implementation, any process that directly invokes the graph must share an environment with the MCP server package.
