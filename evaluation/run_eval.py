"""Run the offline evaluation suite and write measured metrics.

Usage: python -m evaluation.run_eval [--output-dir evaluation/results]

Grounding and constraint metrics are recomputed *independently* from the raw
synthetic OpenWeatherMap payloads (not by calling agent/validation.py), so
they cross-check the production validator rather than restating it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from agent.errors import ClearCastError
from agent.graph import graph_config
from agent.schemas import (
    WEEKDAYS,
    CampaignPlan,
    CampaignPlanRequest,
    CampaignPlanResponse,
    ClientConstraints,
    ReviewRequest,
    ReviewStatus,
    RevisionRequest,
)
from evaluation.fake_openai import ScriptedOpenAI
from evaluation.harness import FIXED_NOW, build_service, fixed_clock, install_fixture_weather
from evaluation.scenarios import CUSTOM_SCENARIOS, PLAN_SCENARIOS, Scenario
from mcp_server import weather_api
from mcp_server.fixtures import FixtureTransport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMP_TOLERANCE, PCT_TOLERANCE = 0.6, 1.0


@dataclass
class Result:
    name: str
    category: str
    description: str
    passed: bool
    outcome: str
    failures: list[str] = field(default_factory=list)
    windows: int = 0
    grounded_windows: int = 0
    compliant_windows: int = 0
    constrained_windows: int = 0
    schema_valid: bool | None = None
    designed_invalid: bool = False
    transient_invalid: bool = False
    rejected: bool | None = None
    repaired: bool | None = None


# -- independent recomputation from raw fixture payloads -------------------------
def raw_payloads(spec: str) -> tuple[dict, dict]:
    transport = FixtureTransport(spec.split(":")[0], clock=fixed_clock)
    with httpx.Client(transport=transport, base_url="https://fixture") as client:
        forecast = client.get("/data/2.5/forecast", params={"lat": 1, "lon": 2}).json()
        air = client.get("/data/2.5/air_pollution/forecast", params={"lat": 1, "lon": 2}).json()
    return forecast, air


def _id_to_ts(observation_id: str) -> int:
    return int(datetime.strptime(observation_id[3:], "%Y%m%dT%H%MZ").replace(tzinfo=UTC).timestamp())


def independent_window_checks(plan: CampaignPlan, spec: str) -> tuple[int, int, int, list[str]]:
    """Return (grounded windows, compliant windows, constrained windows, problems)."""
    forecast, air = raw_payloads(spec)
    by_ts = {item["dt"]: item for item in forecast["list"]}
    tz = timezone(timedelta(seconds=forecast["city"]["timezone"]))
    aqi_by_hour = {item["dt"]: item["main"]["aqi"] for item in air["list"]}
    constraints: ClientConstraints = plan.client.constraints
    grounded = compliant = constrained = 0
    problems: list[str] = []
    for window in plan.windows:
        items = [by_ts.get(_id_to_ts(oid)) for oid in window.observation_ids]
        ok = all(items)
        if ok:
            # Every value the model restated must equal the raw provider value for that block.
            restated = {v.observation_id: v for v in window.claimed_conditions.cited_values}
            for oid, item in zip(window.observation_ids, items, strict=True):
                value = restated.get(oid)
                ok &= value is not None and value.temperature_f is not None
                if value is None or value.temperature_f is None:
                    continue
                ok &= abs(value.temperature_f - item["main"]["temp"]) <= TEMP_TOLERANCE
                if item.get("pop") is not None:
                    ok &= value.precipitation_probability_pct is not None and (
                        abs(value.precipitation_probability_pct - item["pop"] * 100) <= PCT_TOLERANCE
                    )
            observed = window.observed_conditions
            temps = [i["main"]["temp"] for i in items]
            ok &= observed is not None and abs(observed.temperature_max_f - max(temps)) <= TEMP_TOLERANCE
            span_start = datetime.fromtimestamp(min(i["dt"] for i in items), tz)
            span_end = datetime.fromtimestamp(max(i["dt"] for i in items), tz) + timedelta(hours=3)
            ok &= span_start <= window.start_local < window.end_local <= span_end
            ok &= window.start_local >= FIXED_NOW
        grounded += ok
        if not ok:
            problems.append(f"{window.window_id}: not consistent with raw forecast payload")
        if constraints.is_empty():
            continue
        constrained += 1
        fine = ok
        start, end = window.start_local.astimezone(tz), window.end_local.astimezone(tz)
        for item in items or []:
            block_start = item["dt"]
            if not (block_start < end.timestamp() and start.timestamp() < block_start + 10800):
                continue
            temp, pop, wind = item["main"]["temp"], item.get("pop"), (item.get("wind") or {}).get("speed")
            if constraints.min_temperature_f is not None and temp < constraints.min_temperature_f:
                fine = False
            if constraints.max_temperature_f is not None and temp > constraints.max_temperature_f:
                fine = False
            if constraints.max_precipitation_probability_pct is not None and (
                pop is None or pop * 100 > constraints.max_precipitation_probability_pct
            ):
                fine = False
            if constraints.max_wind_speed_mph is not None and (wind is None or wind > constraints.max_wind_speed_mph):
                fine = False
            if constraints.max_aqi is not None:
                hours = [aqi_by_hour.get(block_start + h * 3600) for h in range(3)]
                if None in hours or max(hours) > constraints.max_aqi:
                    fine = False
        if constraints.allowed_hours:
            day_start = start.replace(hour=0, minute=0)
            fine &= any(
                day_start + timedelta(hours=r.start_hour) <= start and end <= day_start + timedelta(hours=r.end_hour)
                for r in constraints.allowed_hours
            )
        for exclusion in constraints.exclusion_windows:
            if WEEKDAYS[start.weekday()] in exclusion.weekdays:
                day_start = start.replace(hour=0, minute=0)
                cut_start, cut_end = (
                    day_start + timedelta(hours=exclusion.start_hour),
                    day_start + timedelta(hours=exclusion.end_hour),
                )
                fine &= not (start < cut_end and cut_start < end)
        compliant += fine
        if not fine:
            problems.append(f"{window.window_id}: violates a hard constraint on raw data")
    return grounded, compliant, constrained, problems


# -- scenario execution ------------------------------------------------------------------
async def run_plan_scenario(scenario: Scenario) -> Result:
    result = Result(
        scenario.name,
        scenario.category,
        scenario.description,
        False,
        "",
        designed_invalid=scenario.designed_invalid,
        transient_invalid=scenario.transient_invalid,
    )
    install_fixture_weather(scenario.weather)
    fake = ScriptedOpenAI(
        agent_policy=scenario.agent_policy, draft_policies=list(scenario.drafts), failure=scenario.model_failure
    )
    service = build_service(fake)
    failures = result.failures
    try:
        try:
            request = CampaignPlanRequest.model_validate(
                {
                    "session_id": f"eval_{scenario.name}"[:60].ljust(16, "_"),
                    "client_id": scenario.client_id,
                    "brief": scenario.brief,
                    "constraints": scenario.constraints,
                }
            )
            response = await service.create_plan(request, request_id=f"eval-{scenario.name}"[:64])
        except ValidationError:
            result.outcome = "error:invalid_request"
            response = None
        except ClearCastError as exc:
            result.outcome = f"error:{exc.code}"
            response = None
    finally:
        await service.aclose()

    if response is None:
        if scenario.expect_error and result.outcome != f"error:{scenario.expect_error}":
            failures.append(f"expected error {scenario.expect_error}, got {result.outcome}")
        if not scenario.expect_error:
            failures.append(f"unexpected {result.outcome}")
        result.rejected = True if scenario.designed_invalid else None
        result.passed = not failures
        return result

    plan = response.plan
    result.outcome = plan.status.value
    try:
        CampaignPlanResponse.model_validate_json(response.model_dump_json())
        result.schema_valid = True
    except ValidationError:
        result.schema_valid = False
        failures.append("response failed schema round-trip")
    if scenario.expect_error:
        failures.append(f"expected error {scenario.expect_error}, got plan {plan.status.value}")
    if scenario.expect_status and plan.status.value != scenario.expect_status:
        failures.append(f"expected {scenario.expect_status}, got {plan.status.value}")
    codes = {issue.code for issue in plan.validation.errors}
    missing = [code for code in scenario.expect_codes if code not in codes]
    warning_codes = {issue.code for issue in plan.validation.warnings}
    missing += [code for code in scenario.expect_rejected_codes if code not in warning_codes]
    if missing:
        failures.append(f"missing validation codes {missing}; saw {sorted(codes)}")
    if scenario.expect_provider_category and scenario.expect_provider_category not in (
        plan.diagnostics.provider_error_categories
    ):
        failures.append(f"provider category {scenario.expect_provider_category} not reported")
    if scenario.expect_repairs is not None and plan.validation.repair_attempts != scenario.expect_repairs:
        failures.append(f"expected {scenario.expect_repairs} repairs, got {plan.validation.repair_attempts}")
    if scenario.expect_drafting is not None and plan.validation.drafting_attempted != scenario.expect_drafting:
        failures.append(f"drafting_attempted={plan.validation.drafting_attempted}")
    if plan.status == ReviewStatus.VALIDATION_FAILED and plan.windows:
        failures.append("failed plan exposes recommended windows")

    grounded, compliant, constrained, problems = independent_window_checks(plan, scenario.weather)
    result.windows, result.grounded_windows = len(plan.windows), grounded
    result.compliant_windows, result.constrained_windows = compliant, constrained
    failures.extend(problems)
    if scenario.designed_invalid:
        # Rejected = the invalid output is never recommended: either the plan failed, or the
        # corrupted window was moved to rejected_windows and every remaining window is verified.
        result.rejected = plan.status == ReviewStatus.VALIDATION_FAILED or (
            bool(plan.rejected_windows)
            and all(w.grounding.verified and w.constraint_check.eligible for w in plan.windows)
            and all(code in warning_codes for code in scenario.expect_rejected_codes)
        )
    if scenario.transient_invalid:
        result.repaired = plan.status == ReviewStatus.PENDING_REVIEW and plan.validation.repair_attempts >= 1
    result.passed = not failures
    return result


async def _plan(service, session: str, request_id: str, **brief):
    payload = {"location": "Baltimore, MD", "business_type": "Coffee shop", **brief}
    return (
        await service.create_plan(CampaignPlanRequest(session_id=session, brief=payload), request_id=request_id)
    ).plan


async def run_custom(name: str) -> Result:
    category, description = CUSTOM_SCENARIOS[name]
    result = Result(name, category, description, False, "ok")
    failures = result.failures
    install_fixture_weather("rainy_mornings" if name != "validation_failed_cannot_be_approved" else "windy")
    fake = ScriptedOpenAI()
    service = build_service(fake)
    a, b = "eval_session_alpha_0001", "eval_session_bravo_0002"
    try:
        if name == "session_isolation_interleaved":
            order = [(a, "Austin, TX"), (b, "Seattle, WA"), (a, "Denver, CO"), (b, "Miami, FL")]
            for i, (session, location) in enumerate(order):
                await _plan(service, session, f"eval-iso-{i}", location=location)
            graph = await service.runtime.get_graph()
            for session, own, other in (
                (a, ("Austin", "Denver"), ("Seattle", "Miami")),
                (b, ("Seattle", "Miami"), ("Austin", "Denver")),
            ):
                text = json.dumps(
                    [m.content for m in (await graph.aget_state(graph_config(session))).values["messages"]], default=str
                )
                if not all(city in text for city in own) or any(city in text for city in other):
                    failures.append(f"history of {session} is not isolated")
            for body in fake.requests:
                text = json.dumps(body["messages"])
                if any(c in text for c in ("Austin", "Denver")) and any(c in text for c in ("Seattle", "Miami")):
                    failures.append("a model request mixed two sessions")
        elif name == "approval_then_revision_is_stale":
            plan = await _plan(service, a, "eval-stale-1")
            approve = ReviewRequest(session_id=a, decision="approve", plan_hash=plan.plan_hash)
            await service.review(plan.request_id, approve)
            revised = (
                await service.revise(
                    plan.request_id,
                    RevisionRequest(
                        session_id=a, base_plan_hash=plan.plan_hash, ad_copy={"w1": ["Revised copy line."]}
                    ),
                )
            ).plan
            if revised.status != ReviewStatus.PENDING_REVIEW or revised.review.approved_plan_hash is not None:
                failures.append("revision did not invalidate approval")
            try:
                await service.review(plan.request_id, approve)
                failures.append("stale approval was accepted")
            except ClearCastError as exc:
                if exc.code != "stale_plan":
                    failures.append(f"unexpected {exc.code}")
        elif name == "rejection_is_distinct_from_approval":
            plan = await _plan(service, a, "eval-reject-1")
            rejected = (
                await service.review(
                    plan.request_id, ReviewRequest(session_id=a, decision="reject", plan_hash=plan.plan_hash)
                )
            ).plan
            if rejected.status != ReviewStatus.REJECTED or rejected.review.approved_plan_hash:
                failures.append("rejection not recorded distinctly")
            try:
                await service.review(
                    plan.request_id, ReviewRequest(session_id=a, decision="approve", plan_hash=rejected.plan_hash)
                )
                failures.append("rejected plan was approved without revision")
            except ClearCastError:
                pass
        elif name == "cross_session_review_blocked":
            plan = await _plan(service, a, "eval-cross-1")
            try:
                await service.review(
                    plan.request_id, ReviewRequest(session_id=b, decision="approve", plan_hash=plan.plan_hash)
                )
                failures.append("another session approved the plan")
            except ClearCastError as exc:
                if exc.code != "plan_not_found":
                    failures.append(f"unexpected {exc.code}")
        elif name == "validation_failed_cannot_be_approved":
            request = CampaignPlanRequest(
                session_id=a,
                client_id="demo_outdoor_fitness",
                brief={"location": "Austin, TX", "business_type": "Outdoor fitness studio"},
            )
            plan = (await service.create_plan(request, request_id="eval-failed-1")).plan
            try:
                await service.review(
                    plan.request_id, ReviewRequest(session_id=a, decision="approve", plan_hash=plan.plan_hash)
                )
                failures.append("failed plan was approved")
            except ClearCastError as exc:
                if exc.code != "plan_not_validated":
                    failures.append(f"unexpected {exc.code}")
        elif name == "cache_reuse_across_clients":
            transport = install_fixture_weather("rainy_mornings")
            first = await _plan(service, a, "eval-cache-1")
            fitness = CampaignPlanRequest(
                session_id=b,
                client_id="demo_outdoor_fitness",
                brief={"location": "Baltimore, MD", "business_type": "Outdoor fitness studio"},
            )
            second = (await service.create_plan(fitness, request_id="eval-cache-2")).plan
            forecast_calls = transport.calls.count("/data/2.5/forecast")
            source = next(s for s in second.evidence.sources if s.tool == "get_forecast")
            original = next(s for s in first.evidence.sources if s.tool == "get_forecast")
            if forecast_calls != 1 or not source.cache_hit:
                failures.append(f"forecast fetched {forecast_calls} times; cache_hit={source.cache_hit}")
            if source.fetched_at_utc != original.fetched_at_utc:
                failures.append("cached evidence was presented with a newer fetch time")
    finally:
        await service.aclose()
        weather_api.set_client(None)
    result.passed = not failures
    return result


def contract_results() -> tuple[int, int, list[str]]:
    cases = json.loads((PROJECT_ROOT / "contracts" / "fixtures" / "plan_requests.json").read_text("utf-8"))["cases"]
    correct, problems = 0, []
    for case in cases:
        try:
            CampaignPlanRequest.model_validate(case["body"])
            valid = True
        except ValidationError:
            valid = False
        if valid is case["api_valid"]:
            correct += 1
        else:
            problems.append(case["name"])
    return correct, len(cases), problems


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


async def run_all() -> dict:
    started = time.perf_counter()
    results = [await run_plan_scenario(s) for s in PLAN_SCENARIOS]
    results += [await run_custom(name) for name in CUSTOM_SCENARIOS]
    weather_api.set_client(None)
    contract_ok, contract_total, contract_problems = contract_results()
    schema = [r for r in results if r.schema_valid is not None]
    invalid = [r for r in results if r.designed_invalid]
    transient = [r for r in results if r.transient_invalid]
    isolation = [r for r in results if r.category == "sessions"]
    metrics = {
        "scenarios": len(results),
        "scenario_pass_rate": rate(sum(r.passed for r in results), len(results)),
        "schema_validity_rate": rate(sum(bool(r.schema_valid) for r in schema), len(schema)),
        "forecast_grounding_consistency": rate(
            sum(r.grounded_windows for r in results), sum(r.windows for r in results)
        ),
        "hard_constraint_compliance": rate(
            sum(r.compliant_windows for r in results), sum(r.constrained_windows for r in results)
        ),
        "invalid_output_rejection_rate": rate(sum(bool(r.rejected) for r in invalid), len(invalid)),
        "repair_success_rate": rate(sum(bool(r.repaired) for r in transient), len(transient)),
        "session_isolation_correctness": rate(sum(r.passed for r in isolation), len(isolation)),
        "api_contract_correctness": rate(contract_ok, contract_total),
        "recommended_windows_checked": sum(r.windows for r in results),
        "constrained_windows_checked": sum(r.constrained_windows for r in results),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "duration_seconds": round(time.perf_counter() - started, 2),
        "metrics": metrics,
        "contract_mismatches": contract_problems,
        "scenarios": [r.__dict__ for r in results],
        "scope_note": (
            "Deterministic offline tests with synthetic weather and scripted model outputs. They measure "
            "validation, grounding, constraint, session, and contract behaviour of the software. They are not "
            "human evaluations of recommendation usefulness and are not evidence of campaign lift, production "
            "effectiveness, or general LLM accuracy."
        ),
    }


def to_markdown(report: dict) -> str:
    m = report["metrics"]
    pct = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"  # noqa: E731
    lines = [
        "# ClearCast offline evaluation results",
        "",
        f"Generated {report['generated_at']} · Python {report['environment']['python']} · "
        f"{report['duration_seconds']} s · {m['scenarios']} scenarios",
        "",
        f"> {report['scope_note']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Scenario pass rate | {pct(m['scenario_pass_rate'])} |",
        f"| Schema-validity rate (returned plans) | {pct(m['schema_validity_rate'])} |",
        f"| Forecast-grounding consistency ({m['recommended_windows_checked']} windows, independent recheck) | "
        f"{pct(m['forecast_grounding_consistency'])} |",
        f"| Hard-constraint compliance ({m['constrained_windows_checked']} windows, independent recheck) | "
        f"{pct(m['hard_constraint_compliance'])} |",
        f"| Invalid-output rejection rate | {pct(m['invalid_output_rejection_rate'])} |",
        f"| Repair success rate (invalid once, then valid) | {pct(m['repair_success_rate'])} |",
        f"| Session-isolation correctness | {pct(m['session_isolation_correctness'])} |",
        f"| API contract correctness (shared fixtures) | {pct(m['api_contract_correctness'])} |",
        "",
        "| Scenario | Category | Outcome | Pass |",
        "|---|---|---|---|",
    ]
    for scenario in report["scenarios"]:
        lines.append(
            f"| {scenario['name']} | {scenario['category']} | {scenario['outcome']} | "
            f"{'yes' if scenario['passed'] else 'NO: ' + '; '.join(scenario['failures'])} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "evaluation" / "results"))
    args = parser.parse_args()
    report = asyncio.run(run_all())
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    (out / "latest.md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    return 0 if report["metrics"]["scenario_pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
