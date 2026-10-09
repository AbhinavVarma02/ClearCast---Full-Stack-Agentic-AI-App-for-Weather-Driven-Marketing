# ClearCast Architecture

ClearCast turns a campaign brief plus a client's rules into a weather-grounded,
human-reviewed campaign plan. One agent serves every client: client differences
are validated configuration, not copies of the agent.

## Request flow

```text
Browser
  │  Gradio session (server-generated session id in gr.State)
  ▼
Gradio UI (Python)                    frontend/app.py            public :7860
  │  HTTP, client token, x-request-id
  ▼
Fastify gateway (Node.js + TypeScript) gateway/src/app.ts         127.0.0.1:8787
  │  TypeBox validation, body limit, rate limits, contract check
  │  HTTP, internal token, x-request-id
  ▼
FastAPI orchestrator (Python)          api/main.py                127.0.0.1:8001
  │  Pydantic validation, per-session lock
  ▼
CampaignPlanningService                agent/service.py
  ├─ LangGraph: chatbot → ToolNode → chatbot (GPT-4o-mini)  agent/graph.py
  │     └─ MCP-to-LangChain bridge (persistent stdio session)  agent/weather_client.py
  │           └─ FastMCP weather server subprocess             mcp_server/weather_server.py
  │                 └─ OpenWeatherMap client (retries, cache)   mcp_server/weather_api.py
  ├─ Evidence ledger from this request's tool outputs        agent/evidence.py
  ├─ Hard-constraint evaluation per forecast block           agent/validation.py
  ├─ Structured drafting (strict JSON schema) + bounded repair  agent/llm.py
  ├─ Deterministic grounding validation                      agent/validation.py
  └─ Review state + session-scoped plan store                agent/review.py
```

## Responsibilities

| Layer | Owns | Does not own |
|---|---|---|
| Gradio UI | Form, client selector, constraint editing, rendering, review buttons, exports, session id | Any LLM, weather, or validation logic |
| Node.js gateway | Public typed contract (`POST /v1/campaign-plans`, review, revisions), payload validation, 16 KiB body limit, per-session and global rate limits, in-flight cap, request-id propagation, upstream error mapping, `/health` and `/ready`, response-contract validation, `/v1/schemas` | Campaign logic, providers, approval rules |
| Python orchestrator | LangGraph execution, MCP discovery and calls, OpenWeatherMap, OpenAI, evidence ledger, constraint checks, grounding validation, repair loop, approval eligibility, plan store, Markdown report | Public rate limiting |

The gateway and the Python service validate the same request contract
independently. `contracts/fixtures/plan_requests.json` is run against both
(vitest and pytest) so the TypeBox and Pydantic schemas cannot drift silently.
Response schemas are generated from Pydantic (`python -m api.export_contracts`)
and the gateway rejects upstream responses that violate them (HTTP 502).

## Agent loop (unchanged topology)

```text
START → chatbot ──tool call──▶ ToolNode
          ▲                       │
          └────── tool result ────┘
          │
          └── no tool call → END
```

* Tools are discovered from the MCP server at startup and converted from JSON
  Schema into Pydantic-backed `StructuredTool`s.
* Each Gradio session maps to its own LangGraph thread (`session:<id>`), so
  `MemorySaver` checkpoints never mix users. Memory is process-local and is
  lost on restart; idle sessions are pruned after six hours.
* `recursion_limit=10` bounds LangGraph super-steps (chatbot and tool nodes),
  not the exact number of tool calls. If the bound is hit, evidence gathered so
  far is kept and the plan reports `step_limit_reached`.
* Earlier turns' tool payloads are not re-sent to the model: a new brief must
  retrieve its own evidence.

## Evidence and grounding

The model never supplies evidence. After the graph finishes, Python rebuilds an
evidence ledger from the ToolMessages recorded for *this request*. Every
forecast block has a deterministic ID derived from the provider timestamp
(`fc-20300404T1200Z`), UTC and local times (the UTC offset OpenWeatherMap reports
for the resolved location, not the server clock), explicit units, fetch time,
and cache age.

The model then drafts a `CampaignDraft` with OpenAI strict JSON-schema output.
Deterministic checks then verify:

* cited IDs exist in this request's forecast evidence and are consecutive;
* the window lies inside the cited blocks, starts after the request time, and
  is at least one hour long;
* claimed temperature, precipitation (in percent, not a fraction), wind, and AQI
  match the cited evidence within small rounding tolerances;
* numbers in free text (°F, mph, % chance of rain) exist in the evidence;
* hypotheses are hedged and no text claims KPIs, ROI, or guaranteed effects;
* the evidence is not stale and was fetched for the resolved coordinates.

Failures are sent back to the model as feedback, at most twice. A plan that
still fails is returned as **Validation Failed** with its reasons and without
recommended windows.

## Client configuration

Profiles live in `agent/config/client_profiles.json` and are validated by
Pydantic on load. Two fictional demo clients are included:

| Client | Hard constraints (Python-enforced) | Soft preferences (model context) |
|---|---|---|
| Demo Client A, Coffee Shop | Local hours 06-11; no Sunday 06-08 (store opens at 8) | Cool or rainy mornings for warm drinks |
| Demo Client B, Outdoor Fitness | Hours 06-09 and 17-20; 45-88 °F; precipitation ≤ 30 %; wind ≤ 15 mph; AQI ≤ 2 | Dry, mild, low-wind sessions |

Every forecast block is evaluated against the hard constraints before
drafting. A block with a missing field that a constraint needs is ineligible
("cannot be verified"). If no block is eligible the plan fails without calling
the drafting model. A proposed window that violates a constraint is moved to
`rejected_windows`; the model cannot override that decision.

## Human review

```text
Draft → Validation Failed
      → Pending Review → Approved
                       → Rejected
Approved/Rejected --ad-copy revision--> Pending Review (new revision, new hash)
```

Approval binds to the exact `plan_hash` the reviewer saw. Any revision changes
the hash and invalidates the approval; approving with an old hash returns
`409 stale_plan`. Plans are stored per session; another session receives
`404 plan_not_found`. Exports (JSON and Markdown) carry the status in their
filename and content. ClearCast never publishes ads.

## Reliability

* One persistent MCP stdio subprocess, shared concurrently, reconnected once on
  transport failure, shut down cleanly with the API.
* Blocking provider I/O runs in worker threads inside the async MCP handlers.
* OpenWeatherMap: 15 s read / 5 s connect timeouts, three attempts with
  exponential backoff and jitter, `Retry-After` honoured (capped at 5 s) for HTTP 429.
  401/404/400 fail fast; invalid JSON and missing fields are categorised errors.
* A 10-minute cache (24 h for geocoding) keyed by endpoint and rounded
  coordinates. Cached responses keep their original fetch time and report their
  age. It is useful when the same city is planned for several clients.
* OpenAI: 45 s timeout, two SDK retries, categorised errors (`model_timeout`,
  `model_rate_limited`, `model_auth_error`, ...), 150 s overall agent deadline.
* Graph initialisation is lazy, lock-protected, makes no provider calls, and
  reports `misconfigured` or `failed` through readiness instead of crashing.

## Observability

Structured JSON logs carry request ID, hashed session reference, client,
model, durations, MCP tool call counts, tool errors, provider error
categories, cache hits, repair attempts, validation and review status, and
provider-reported token counts. Prompts, campaign text, and secrets are not
logged; httpx/httpcore are capped at WARNING because they would print request
URLs. Cost is calculated only when per-token prices are configured. Optional
LangSmith tracing is enabled only when requested and a key exists. This is
application-level diagnostics, not production monitoring.
