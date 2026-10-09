# Testing and offline evaluation

All default checks run without OpenAI or OpenWeatherMap credentials. Providers
are replaced by:

* **Synthetic weather** (`mcp_server/fixtures.py`): OpenWeatherMap-shaped
  payloads for named patterns (mild, rainy mornings, clear and warm, heat wave,
  cold snap, windy, storm week, poor air, missing fields) plus injected failures
  (503, 429, timeouts, invalid JSON, empty geocoding, auth errors). The real
  `weather_api` client parses them, so retries, units, IDs, and caching are
  exercised.
* **Scripted model** (`evaluation/fake_openai.py`): answers real Chat
  Completions HTTP requests made by `langchain-openai`, so tool binding,
  tool-call parsing, strict JSON-schema output, and token-usage metadata use
  production code. Behaviour per scenario is scripted (grounded drafts, invented
  IDs, wrong values, malformed JSON, KPI claims, provider errors, ...).
* **Fixed clock** (2030-04-04 01:30 UTC), so no result depends on today's date.

## Commands

```bash
python -m pytest                                  # unit + component + eval gate (offline)
python -m pytest -m integration                   # needs Node.js and a built gateway
python -m evaluation.run_eval                     # writes evaluation/results/latest.{json,md}
python -m api.export_contracts --check            # Pydantic -> JSON Schema drift check
python scripts/check_secrets.py                   # secret scan
npm --prefix gateway run typecheck && npm --prefix gateway run lint && npm --prefix gateway test
python scripts/smoke_test.py --url http://127.0.0.1:7860 --expect-fixture   # against a running stack
```

## Results recorded for this revision

Measured locally on Windows 11, Python 3.13.2, Node.js 24.14, and confirmed by
GitHub Actions on Ubuntu (Python 3.13, Node.js 22) for commit `109adb5`:

| Suite | Result |
|---|---|
| Ruff lint and format check | clean |
| Contract drift check | up to date |
| Secret scan | passed |
| `pytest` (default, offline) | 132 passed |
| `pytest -m integration` | 8 passed |
| Gateway `vitest` | 57 passed (2 files) |
| Gateway `tsc --noEmit` and ESLint (`strictTypeChecked`) | clean |
| Offline evaluation | 45 scenarios, 45 passed |
| Docker image build and container smoke test (offline fixtures) | passed |

## Evaluation suite

`evaluation/scenarios.py` defines 39 plan scenarios and 6 workflow scenarios.

| Category | Scenarios |
|---|---|
| Coffee-shop campaigns (rain, clear, cold, poor air, out-of-hours repair) | 5 |
| Outdoor-fitness campaigns (mild, rain, heat, cold, wind, poor air, missing fields, repair) | 8 |
| General brief (storm week) | 1 |
| Conflicting constraints (unsatisfiable, min > max) | 2 |
| Invalid model output (window times, elapsed window, invented IDs, value mismatch, unit confusion, invented numbers, KPI claims, unhedged hypotheses; whole-plan and single-window corruption) | 11 |
| Malformed structured output | 3 |
| OpenWeatherMap failures (503, 429, timeout, invalid JSON, unknown location) | 5 |
| Model failures (HTTP 500, timeout, skipped tools, recursion limit) | 4 |
| Sessions (interleaved isolation, cross-session review) | 2 |
| Review (stale approval, rejection, failed-plan approval) | 3 |
| Cache reuse across clients | 1 |

### Measured metrics (`evaluation/results/latest.md`)

| Metric | Definition | Result |
|---|---|---|
| Scenario pass rate | Scenarios meeting every expectation | 45/45 (100%) |
| Schema-validity rate | Returned plans that round-trip through `CampaignPlanResponse` | 100% |
| Forecast-grounding consistency | Recommended windows whose IDs, times, and claimed values match the **raw** fixture payloads, recomputed independently of `agent/validation.py` | 32/32 windows (100%) |
| Hard-constraint compliance | Recommended windows for constrained clients that satisfy every rule when rechecked independently on raw data | 20/20 windows (100%) |
| Invalid-output rejection rate | Scenarios with persistently invalid model output where that output is never recommended (plan fails, or the corrupted window is rejected while verified windows remain) | 100% |
| Repair success rate | Scenarios invalid on the first attempt that are valid after one repair | 100% |
| Session-isolation correctness | Session scenarios passing | 100% |
| API contract correctness | Shared request fixtures where Pydantic matches the expected verdict (TypeBox is checked by vitest) | 23/23 (100%) |

### What these numbers do and do not show

They show that, under controlled inputs, the software rejects ungrounded or
rule-breaking output, keeps sessions apart, enforces review rules, and honours
its API contract. Because model behaviour is scripted, they are **not**:

* a measure of how useful real GPT-4o-mini recommendations are (no human
  evaluation was performed);
* evidence of campaign lift, revenue, or ROI;
* a general accuracy figure for any language model;
* a latency or throughput benchmark (durations in the reports come from mocked
  providers and are not representative).

The live deployment check (`scripts/smoke_test.py --expect-live`) is a separate
smoke test, recorded in `docs/deployment.md`.
