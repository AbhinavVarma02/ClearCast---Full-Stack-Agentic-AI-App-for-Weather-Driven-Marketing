---
title: ClearCast AI
emoji: 🌦️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# ClearCast — Full-Stack Agentic AI App for Weather-Driven Marketing

ClearCast turns live OpenWeatherMap forecasts into weather-timed campaign plans.
A LangGraph agent running GPT-4o-mini discovers weather tools over MCP and
gathers evidence. The model then drafts a structured plan, and deterministic
Python code checks every cited forecast observation, every weather number, and
every client rule before a human approves or rejects the plan.

**Live demo:** https://huggingface.co/spaces/abhinavvathadi/ClearCast-AI

This is a portfolio project. The two demo clients are **fictional**
configurations, not customer deployments, and nothing ClearCast produces is
evidence of campaign lift or ROI.

## What it does

* **Four campaign inputs**: location, business type, campaign goal, and tone,
  plus an optional client profile with editable hard constraints.
* **Agentic weather retrieval**: LangGraph `chatbot → ToolNode → chatbot`, with
  four MCP tools (`geocode_city`, `get_current_weather`, `get_forecast`,
  `get_air_quality`) discovered at runtime and converted from JSON Schema to
  Pydantic-backed `StructuredTool`s.
* **Evidence-grounded plans**: a validated `CampaignPlan` whose windows cite
  forecast observation IDs from *this* request. Python verifies times, values,
  units, staleness, and hedged-hypothesis wording, with up to two repair
  attempts. Plans that still fail are returned as **Validation Failed**.
* **Client-configurable rules**: Demo Client A (Coffee Shop) and Demo Client B
  (Outdoor Fitness Studio) are validated JSON configuration for one shared
  agent. Python enforces the hard rules; the model cannot override them.
* **Human-in-the-loop review**: Pending Review → Approved / Rejected. Approval is
  bound to a plan hash, and editing the ad copy invalidates it. JSON and
  Markdown export.
* **Node.js + TypeScript + Fastify gateway**: the typed public API in front of
  the Python orchestration service (validation, limits, request IDs, error
  mapping, readiness, response-contract checks).
* **Per-session isolation**: a server-generated session ID per browser session,
  mapped to its own LangGraph thread.

## Architecture

```text
Browser ─▶ Gradio UI (Python, :7860)
             │ HTTP + client token + x-request-id
             ▼
           Fastify gateway (Node.js/TypeScript, 127.0.0.1:8787)
             │ HTTP + internal token
             ▼
           FastAPI orchestrator (Python, 127.0.0.1:8001)
             ├─ LangGraph + GPT-4o-mini (per-session MemorySaver thread)
             │    └─ MCP-to-LangChain bridge ── stdio ──▶ FastMCP weather server ─▶ OpenWeatherMap
             ├─ Evidence ledger + hard-constraint checks + grounding validation
             └─ Review state (session-scoped, in memory)
```

| Component | Technology | Responsibility |
|---|---|---|
| UI | Gradio Blocks | Brief, client rules, plan tabs, review, exports |
| Gateway | Node.js 22, TypeScript (strict), Fastify 5, TypeBox, Ajv | Public contract, validation, 16 KiB body limit, rate limits, request IDs, error mapping, `/health`, `/ready` |
| Orchestrator | FastAPI, Pydantic | Internal API, session locks, error envelopes |
| Agent | LangGraph, LangChain, GPT-4o-mini | Tool-calling loop, structured drafting |
| Tool bridge | MCP Python SDK | Runtime discovery, persistent stdio session, schema conversion |
| Weather | FastMCP, httpx | Four tools; retries, 429 handling, units, evidence IDs, cache |
| Process manager | `deploy/launcher.py` | Start order, readiness, restarts, least-privilege env, clean shutdown |

Details: [docs/architecture.md](docs/architecture.md).

## Run locally

Prerequisites: Python 3.13, Node.js 22+, an
[OpenAI API key](https://platform.openai.com/api-keys), and an
[OpenWeatherMap key](https://home.openweathermap.org/users/sign_up) (free tier).

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
npm --prefix gateway ci && npm --prefix gateway run build
cp .env.example .env                                # then add your keys
python safe_env_check.py                            # prints key metadata, never values
python app.py                                       # orchestrator + gateway + UI
```

Open http://127.0.0.1:7860. `python app.py` runs `deploy/launcher.py`, which
generates per-boot internal tokens and passes provider secrets only to the
orchestrator process.

**Offline demo (no keys, synthetic weather):**

```bash
python -m evaluation.fake_openai_server --port 8099 &
CLEARCAST_WEATHER_FIXTURES=baseline_mild OPENAI_API_KEY=sk-offline-fixture-not-a-real-key \
  OPENAI_BASE_URL=http://127.0.0.1:8099/v1 python app.py
```

Plans made this way are labelled `OFFLINE FIXTURE DATA`.

**Docker (same image as the Hugging Face Space):**

```bash
OPENAI_API_KEY=... OPENWEATHERMAP_API_KEY=... docker compose up --build
```

**Optional live MCP check** (calls OpenWeatherMap with your key):

```bash
python test_mcp.py
```

## API (gateway)

| Method and path | Purpose |
|---|---|
| `POST /v1/campaign-plans` | Generate a validated plan: `{session_id, brief, client_id?, constraints?}` |
| `POST /v1/campaign-plans/{request_id}/review` | Approve or reject: `{session_id, decision, plan_hash, note?}` |
| `POST /v1/campaign-plans/{request_id}/revisions` | Edit ad copy (invalidates approval): `{session_id, base_plan_hash, ad_copy}` |
| `GET /v1/schemas` | Request schemas (TypeBox) and the response contract (from Pydantic) |
| `GET /health`, `GET /ready` | Liveness; readiness including the orchestrator's MCP tool discovery |

All `/v1` routes require the trusted UI's client token. Errors use one envelope:
`{"error": {"code", "message", "request_id", "retryable", "details"}}`.
Contracts live in [`contracts/`](contracts/).

## Testing and evaluation

```bash
python -m pytest                      # 132 offline tests (mocked MCP/LangGraph/providers)
python -m pytest -m integration       # 8 multi-process tests (Node gateway, launcher, MCP subprocess)
npm --prefix gateway test             # 57 gateway tests
python -m evaluation.run_eval         # 45 offline scenarios with independent rechecks
```

The offline evaluation recorded 45/45 scenarios passing. Every recommended
window (32) matched the raw fixture evidence, and every constrained window (20)
satisfied its client's rules when rechecked independently; invalid outputs were
rejected in 100% of the designed cases. These are deterministic software tests
with scripted model outputs. They are **not** human evaluations of
recommendation quality, and they are not evidence of campaign lift.
See [docs/evaluation.md](docs/evaluation.md).

CI (`.github/workflows/ci.yml`) runs lint, formatting, contract drift, the
secret scan, tests, the evaluation, gateway typecheck/lint/tests, the
integration suite, and a Docker build plus container smoke test, all without
provider credentials.

## Deployment

The Hugging Face Space runs this repository's `Dockerfile` (Docker SDK,
`app_port: 7860`). Only Gradio listens publicly; the gateway and orchestrator
bind to 127.0.0.1 inside the container. Required Space secrets:
`OPENAI_API_KEY`, `OPENWEATHERMAP_API_KEY`. Optional: `LANGCHAIN_API_KEY` with
`LANGCHAIN_TRACING_V2=true`. See [docs/deployment.md](docs/deployment.md) for
the deployment record and verification.

## Project structure

```text
agent/            LangGraph graph, prompts, MCP bridge, evidence, validation, review, service
agent/config/     Validated client profiles (fictional demo clients)
api/              Internal FastAPI service and contract export
contracts/        JSON Schemas generated from Pydantic + shared contract fixtures
deploy/           Single-container process supervisor
evaluation/       Scripted model, harness, scenarios, evaluation runner, results
frontend/         Gradio UI, theme, gateway client
gateway/          Node.js + TypeScript + Fastify gateway (src, tests)
mcp_server/       FastMCP weather server, OpenWeatherMap client, offline fixtures
scripts/          Secret scan and deployment smoke test
tests/            pytest suites (unit, component, integration)
```

## Limitations

* Session memory and review state are in-process: they are lost on restart and
  are not shared across replicas. This is not durable or multi-tenant storage.
* There is no user authentication. Sessions are unguessable IDs per browser
  session, not user accounts.
* Local times use the single UTC offset OpenWeatherMap reports; a DST change
  inside the 5-day window can shift times by an hour.
* AQI limits can be verified only where OpenWeatherMap provides an AQI forecast
  (about 4 days); later blocks are ineligible for AQI-limited clients.
* Marketing hypotheses are untested model text. No human evaluation of
  recommendation usefulness has been performed.
* Cost is not estimated unless current per-token prices are configured.
