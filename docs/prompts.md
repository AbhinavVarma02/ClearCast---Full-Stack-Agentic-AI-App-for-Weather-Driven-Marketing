## ClearCast Cursor Build Prompts — Clean Developer Version

### Prompt 1 — Start the project

Create a new Python project called ClearCast. It should be a weather-driven marketing app using OpenWeatherMap, an MCP server, a LangGraph agent, and a Gradio frontend.
Set up the basic folder structure and empty starter files.

---

### Prompt 2 — Add project files

Add `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, and `docker-compose.yml`.
Make sure `.env` is ignored and `.env.example` only has placeholder keys.

---

### Prompt 3 — Create the folder structure

Use this structure:

```text
mcp_server/
  __init__.py
  weather_api.py
  weather_server.py

agent/
  __init__.py
  weather_client.py
  prompts.py
  graph.py

frontend/
  __init__.py
  app.py

docs/
  architecture.md
  evaluation.md
  cost_notes.md
  deployment.md
  prompts.md
```

Keep the files clean and minimal for now.

---

### Prompt 4 — Build the weather API wrapper

Implement `mcp_server/weather_api.py` using OpenWeatherMap.
Add functions for geocoding, current weather, 5-day forecast, and air quality. Load `.env` from the project root using `override=True`, but never print secrets.

---

### Prompt 5 — Add safe API key handling

Improve `weather_api.py` with safe validation for missing keys, placeholder keys, and bad API responses.
Do not expose the key, environment variables, or full request URLs in errors.

---

### Prompt 6 — Build the MCP server

Implement `mcp_server/weather_server.py`.
Expose four MCP tools: `geocode_city`, `get_current_weather`, `get_forecast`, and `get_air_quality`.

---

### Prompt 7 — Add lightweight MCP test

Implement `test_mcp.py`.
It should start the MCP server, list the four tools, and test only `geocode_city("Baltimore")` to keep API usage low.

---

### Prompt 8 — Add safe environment check

Create `safe_env_check.py`.
It should check whether keys exist, whether they look like placeholders, key length, duplicate lines, and tracing status without printing actual keys.

---

### Prompt 9 — Build MCP client bridge

Implement `agent/weather_client.py`.
Connect to the MCP server using `python -m mcp_server.weather_server` and expose the MCP tools to the LangGraph agent.

---

### Prompt 10 — Create the agent prompt

Implement `agent/prompts.py`.
The agent should generate campaign windows, weather reasoning, ad copy, risk notes, and a forecast summary.

---

### Prompt 11 — Add tool-use guardrails

Update the agent prompt so the tool order is geocode, forecast, current weather, then optional air quality.
Tell the agent to reuse coordinates and avoid repeating tool calls.

---

### Prompt 12 — Build the LangGraph agent

Implement `agent/graph.py`.
Use ChatOpenAI, connect the MCP weather tools, run the tool-calling graph, and return the final campaign strategy as text.

---

### Prompt 13 — Add loop protection

Add a recursion limit of 10 to the LangGraph invocation.
Make sure one frontend request creates only one graph call and no automatic repeated calls.

---

### Prompt 14 — Build the Gradio frontend

Implement `frontend/app.py`.
Add inputs for location, business type, campaign goal, and tone, then show the generated campaign strategy.

---

### Prompt 15 — Upgrade the UI

Make the Gradio app look like a premium SaaS dashboard.
Use a dark polished layout with a hero section, input card, report card, how-it-works strip, architecture strip, and examples table.

---

### Prompt 16 — Write the README

Write a clean README with overview, architecture, setup steps, environment variables, run commands, safety notes, and loop/cost safeguards.

---
### Prompt 17 — Final review

Review the whole project without opening `.env`.
Check folder structure, imports, MCP tools, LangGraph loop limit, secret safety, frontend wiring, and documentation. Then give me the manual test commands.


### Prompt 18 

I have corrected few errors, now improve the gradio interface to premium looking. 