# ClearCast Technical Evidence Report (v2, October 2026)

This report supersedes the July 2026 read-only review
(`ClearCast_Technical_Evidence_Report.md`) for current-state claims. That
review's historical findings are preserved in section 2.

**Evidence labels**

* **Implemented**: present in code on GitHub branch `feature/fde-upgrade`.
* **Tested**: verified by a named automated test or command.
* **Deployed**: verified on the Hugging Face Space `abhinavvathadi/ClearCast-AI`.
* **Not verified**: not established by available evidence.

## 1. Executive summary

ClearCast is a portfolio application that turns live OpenWeatherMap forecasts
into weather-timed campaign plans. A LangGraph + GPT-4o-mini agent gathers
evidence through four MCP weather tools. The model drafts a structured plan,
and deterministic Python validation checks every cited observation, number,
and client rule. A human then approves or rejects the plan. A Node.js +
TypeScript + Fastify gateway fronts an internal FastAPI orchestration service,
and everything ships as one Docker container on Hugging Face Spaces.

Maturity: a working, tested, deployed portfolio system. It is **not**
production-grade multi-tenant software (in-memory state, no authentication,
single replica), and nothing in it measures campaign lift or ROI.

## 2. Historical baseline (July 2026 review, preserved)

* GitHub `main` (`b354cbb`) built the LangGraph agent at import time, launched
  Gradio with `inbrowser=True`, and called `invoke_graph(...)` with the default
  `thread_id="1"`, so all users shared one conversation thread.
* The Space history (local repository, remotes `space` and `space2`) was 15
  commits ahead of GitHub. It added root `app.py`, `progress.md`, lazy graph
  initialisation, safe error display, and MCP subprocess secret forwarding.
  `progress.md` records a successful deployed Baltimore coffee-shop request in
  July 2026 (Space revision `bd0f16d`, model `gpt-5.5` at that time).
* Final output was unconstrained Markdown, with no structured validation, no
  automated tests or CI, no retries, a new MCP subprocess per tool call,
  synchronous `requests` inside async MCP handlers, and the default `ToolNode`
  error handler (provider failures aborted the request).
* `docker-compose.yml` was illustrative and not runnable.

## 3. Current evidence ledger

| Claim | Evidence | Label |
|---|---|---|
| Four campaign inputs (location, business type, goal, tone) preserved, plus client profile and constraints | `frontend/app.py`; `tests/test_frontend.py` | Implemented, Tested, Deployed |
| GPT-4o-mini tool calling through LangGraph `chatbot → ToolNode → chatbot` | `agent/graph.py`, `agent/llm.py` (`DEFAULT_MODEL`); Space log `"model": "gpt-4o-mini"` | Implemented, Tested, Deployed |
| Runtime MCP discovery of 4 tools with JSON Schema → Pydantic `StructuredTool` | `agent/weather_client.py`; `tests/test_mcp_bridge.py`; Space log `runtime.init.ready` | Implemented, Tested, Deployed |
| Per-session isolation (server-generated session id → own LangGraph thread) | `frontend/app.py::new_session_id`, `agent/graph.py::thread_id_for`; `test_service.py::test_two_sessions_with_interleaved_requests_never_share_history`; full-stack test with two `gradio_client` sessions; live smoke test (distinct `session_ref`) | Implemented, Tested, Deployed |
| `recursion_limit=10` bounds graph super-steps (not exactly 10 tool calls) | `agent/graph.py`; `test_service.py::test_recursion_limit_keeps_gathered_state_and_reports_it` | Implemented, Tested |
| Lazy, lock-protected initialisation; no provider calls at startup; readiness | `agent/runtime.py`; `test_runtime_and_graph.py`; Space readiness "Agent ready" | Implemented, Tested, Deployed |
| Structured `CampaignPlan` with evidence ledger, validation report, review state | `agent/schemas.py`, `agent/evidence.py`, `agent/service.py`; contract in `contracts/` | Implemented, Tested, Deployed |
| Deterministic grounding (IDs, contiguity, window inside evidence, future start, per-block values with units, free-text numbers, staleness, coordinates, hedged hypotheses, no KPI/ROI claims) | `agent/validation.py`, `agent/evidence.py`; `tests/test_validation.py` (26 tests), `tests/test_evidence.py` | Implemented, Tested |
| Bounded repair (2 attempts); failing plans returned as Validation Failed; failing windows rejected individually | `agent/service.py`; `test_service.py`; evaluation scenarios | Implemented, Tested, Deployed (live runs used 0–2 repairs) |
| Two fictional demo clients as validated configuration; Python-enforced hard constraints | `agent/config/client_profiles.json`, `agent/clients.py`, `agent/validation.py::assess_blocks`; evaluation hard-constraint recheck | Implemented, Tested, Deployed |
| Human review states, hash-bound approval, revision invalidates approval, cross-session protection, JSON/Markdown export | `agent/review.py`, `agent/service.py`; `test_service.py::test_review_lifecycle_and_stale_approval`; live approve + export | Implemented, Tested, Deployed |
| Node.js + TypeScript + Fastify gateway (validation, 16 KiB body limit, rate limits, in-flight cap, request ids, error mapping, `/health`, `/ready`, response-contract validation) | `gateway/src/`; 57 vitest tests; `tests/integration/test_gateway_python.py`; Space log | Implemented, Tested, Deployed |
| Gradio uses the gateway for generation; internal services bound to loopback | `frontend/gateway_client.py`, `deploy/launcher.py`; container socket table; public URL returns 404 for internal routes | Implemented, Tested, Deployed |
| Persistent MCP stdio session, reconnect, timeouts, non-blocking handlers | `agent/weather_client.py`, `mcp_server/weather_server.py`; `test_mcp_bridge.py` (real subprocess reused, `connect_count == 1`) | Implemented, Tested, Deployed |
| OpenWeatherMap retries with backoff, 429 `Retry-After`, categorised errors, no key leakage | `mcp_server/weather_api.py`; `tests/test_weather_api.py` | Implemented, Tested |
| Weather cache keeps original fetch time and age | `weather_api.py::_TTLCache`; `test_weather_api.py::test_cache_reuses_responses_without_hiding_their_age`; evaluation `cache_reuse_across_clients` | Implemented, Tested |
| Structured logs and per-request diagnostics (tool counts, provider categories, repair reasons, reported tokens) | `agent/observability.py`; Diagnostics tab; Space logs | Implemented, Tested, Deployed |
| Model cost estimate | Calculated only when prices are configured; not configured on the Space | Implemented; **Not verified** as a cost figure |
| LangSmith tracing | Enabled only with flag + key; the Space has a key but no flag | Implemented; **Not verified** in production |
| GitHub Actions CI without provider credentials (lint, format, contracts, secrets, tests, eval, gateway, integration, Docker smoke) | `.github/workflows/ci.yml`; run 37874302019 on `109adb5`: 4/4 jobs succeeded | Implemented, Tested |
| Docker single-container deployment with process supervisor | `Dockerfile`, `deploy/launcher.py`; local container smoke test; Space RUNNING | Implemented, Tested, Deployed |

## 4. Tests and evaluation

| Suite | Result |
|---|---|
| `python -m pytest` (offline) | 132 passed |
| `python -m pytest -m integration` | 8 passed (real Node gateway, launcher, MCP stdio subprocess) |
| Gateway `vitest` | 57 passed; `tsc --noEmit` and ESLint `strictTypeChecked` clean |
| `python -m evaluation.run_eval` | 45/45 scenarios; grounding consistency 32/32 windows and hard-constraint compliance 20/20 windows (independent recheck on raw fixture payloads); invalid-output rejection, repair success, session isolation, schema validity, and API contract correctness all 100% |

These are deterministic tests with synthetic weather and scripted model
outputs. They are **not** human evaluations of recommendation usefulness,
campaign-lift evidence, or general LLM accuracy (see `docs/evaluation.md`).

## 5. Deployment evidence

See `docs/deployment.md` for the full record. In summary: the Space switched
from the Gradio SDK to the Docker SDK, revisions `79c4c46` → `1c80e59` →
`8997ce6` built and ran, and the final live smoke test produced validated
coffee-shop and outdoor-fitness plans from live OpenWeatherMap data. It
confirmed distinct sessions, approval and export, safe error handling, and no
credential-shaped strings in responses or logs.

The first live run exposed a real model-reliability problem: GPT-4o-mini
mis-aggregated temperatures across blocks, so every outdoor-fitness attempt
failed validation. Because the validator rejected those plans instead of
showing them, the fix was a design change (per-block value restatement and
window-level rejection), not a loosened check.

## 6. Security and privacy

* **Tested**: API keys never appear in provider errors, tracebacks, or logs
  (`test_weather_api.py`, `test_contracts_and_security.py`); the MCP subprocess
  receives only the weather key; logs carry no campaign text; a repository
  secret scan runs in CI. GitHub push protection also flagged one synthetic
  test value, which was replaced rather than bypassed.
* **Deployed**: only the Gradio port is public; the gateway and orchestrator use
  per-boot random tokens and loopback binding.
* **Not verified / not implemented**: user authentication, authorisation,
  audit trails, data retention controls, prompt-injection defences beyond
  deterministic output validation, and dependency vulnerability scanning.

## 7. Known gaps

* In-memory `MemorySaver` checkpoints and review state; lost on restart or Space
  sleep; single replica.
* Fixed UTC offset per forecast (DST edge within five days not handled).
* AQI constraints are verifiable only within the AQI forecast horizon (about 4 days).
* No human evaluation of recommendation quality; no campaign performance data.
* Live behaviour varies by run: repair counts and window counts differ between requests.
* GitHub `main` is not updated until the `feature/fde-upgrade` branch is merged.

## 8. Resume bullets (verified facts only)

* **AI engineering**: Built ClearCast, a LangGraph + GPT-4o-mini agent that
  retrieves live OpenWeatherMap data through four runtime-discovered MCP tools
  and drafts strict-JSON-schema campaign plans. Deterministic Python
  validation checks every cited forecast observation, weather value, and
  client rule, with bounded repair. 45 offline evaluation scenarios plus
  132 unit and 8 integration tests run in GitHub Actions without provider
  credentials.
* **Forward deployment / client integration**: Shipped ClearCast as a
  single-container Hugging Face Docker Space with a Node.js/TypeScript Fastify
  gateway (typed contract, rate limits, request-ID propagation, response-
  contract validation) in front of a FastAPI orchestrator. Fictional client
  profiles are validated configuration for one agent, with per-session
  isolation and hash-bound human approval, verified end to end on the live
  deployment.
