"""End-to-end smoke test against a running ClearCast UI (local container or Hugging Face Space).

Drives the public Gradio API exactly like a browser session would:
two independent sessions generate plans, one plan is approved and exported.
Checks structure, validation status, internal evidence consistency, session
isolation, review, export, and that error text contains no credentials.

Usage:
    python scripts/smoke_test.py --url http://127.0.0.1:7860 [--expect-fixture]
    python scripts/smoke_test.py --url https://<space>.hf.space --expect-live

With --expect-live this makes real OpenAI and OpenWeatherMap calls through the
deployed app (a few requests).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from gradio_client import Client

SECRET_RE = re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}|appid=[0-9a-f]{32}")


def generate(client: Client, client_id: str, location: str, business: str, goal: str, tone: str):
    return client.predict(
        client_id, location, business, goal, tone, "", None, None, None, None, "No limit", "", api_name="/generate_plan"
    )


def check_plan(plan: dict, label: str, expect_fixture: bool, expect_live: bool) -> list[str]:
    problems = []
    if plan["status"] not in {"pending_review", "validation_failed"}:
        problems.append(f"{label}: unexpected status {plan['status']}")
    sources = {s["source"] for s in plan["evidence"]["sources"] if s.get("source")}
    if expect_fixture and not all(s.startswith("fixture:") for s in sources):
        problems.append(f"{label}: expected fixture data, got {sources}")
    if expect_live and sources != {"openweathermap"}:
        problems.append(f"{label}: expected live OpenWeatherMap data, got {sources}")
    tools = plan["diagnostics"]["tool_calls"]
    if not {"geocode_city", "get_forecast"} <= set(tools):
        problems.append(f"{label}: MCP tools not called ({tools})")
    by_id = {obs["observation_id"]: obs for obs in plan["evidence"]["forecast"]}
    for window in plan["windows"]:
        for obs in window["evidence"]:
            if by_id.get(obs["observation_id"]) != obs:
                problems.append(f"{label}/{window['window_id']}: cited evidence differs from the ledger")
        for value in window["claimed_conditions"]["cited_values"]:
            source = by_id.get(value["observation_id"])
            if source is None or value["temperature_f"] is None or source["temperature_f"] is None:
                problems.append(f"{label}/{window['window_id']}: restated value lacks matching evidence")
            elif abs(value["temperature_f"] - source["temperature_f"]) > 0.6:
                problems.append(f"{label}/{window['window_id']}: restated temperature differs from evidence")
        if not (window["grounding"]["verified"] and window["constraint_check"]["eligible"]):
            problems.append(f"{label}/{window['window_id']}: unverified window exposed as recommended")
    if plan["status"] == "validation_failed" and plan["windows"]:
        problems.append(f"{label}: failed plan exposes windows")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expect-fixture", action="store_true")
    parser.add_argument("--expect-live", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    started = time.perf_counter()
    problems: list[str] = []
    summary: dict = {"url": args.url}

    alice, bob = Client(args.url, verbose=False), Client(args.url, verbose=False)
    a = generate(alice, "demo_coffee_shop", "Baltimore, MD", "Coffee shop", "Increase morning visits", "Friendly")
    b = generate(
        bob, "demo_outdoor_fitness", "Austin, TX", "Outdoor fitness studio", "Promote outdoor classes", "Urgent"
    )
    for label, result in (("coffee", a), ("fitness", b)):
        if not result[3]:
            problems.append(f"{label}: no plan returned: {result[0][:300]}")
            continue
        plan = json.loads(result[3])
        summary[label] = {
            "request_id": plan["request_id"],
            "status": plan["status"],
            "windows": len(plan["windows"]),
            "rejected_windows": len(plan["rejected_windows"]),
            "validation_errors": [e["code"] for e in plan["validation"]["errors"]],
            "repair_attempts": plan["validation"]["repair_attempts"],
            "repair_reasons": plan["diagnostics"]["repair_reasons"],
            "tool_calls": plan["diagnostics"]["tool_calls"],
            "llm_calls": plan["diagnostics"]["llm_calls"],
            "duration_ms": plan["diagnostics"]["duration_ms"],
            "token_usage": plan["diagnostics"]["token_usage"],
            "session_ref": plan["session_ref"],
            "evidence_sources": sorted({s["source"] for s in plan["evidence"]["sources"] if s.get("source")}),
            "forecast_observations": len(plan["evidence"]["forecast"]),
        }
        problems += check_plan(plan, label, args.expect_fixture, args.expect_live)
    if (
        "coffee" in summary
        and "fitness" in summary
        and summary["coffee"]["session_ref"] == summary["fitness"]["session_ref"]
    ):
        problems.append("two browser sessions shared one server-side session")
    for label, result in (("coffee", a), ("fitness", b)):
        if SECRET_RE.search(json.dumps(result, default=str)):
            problems.append(f"{label}: credential-shaped text in response")

    # Review + export on the first validated plan.
    for label, client, result in (("coffee", alice, a), ("fitness", bob, b)):
        if result[3] and json.loads(result[3])["status"] == "pending_review":
            approved = client.predict("Smoke test approval", api_name="/approve_plan")
            files = [Path(v["value"]) for v in approved if isinstance(v, dict) and isinstance(v.get("value"), str)]
            export = next((f for f in files if f.suffix == ".json"), None)
            status = json.loads(export.read_text("utf-8"))["status"] if export else None
            summary["review"] = {"plan": label, "approved_status": "Approved" in approved[4], "export_status": status}
            if status != "approved":
                problems.append(f"{label}: approval/export failed ({status})")
            break
    else:
        summary["review"] = "skipped: no plan passed validation (weather may legitimately violate constraints)"

    summary["duration_seconds"] = round(time.perf_counter() - started, 1)
    summary["problems"] = problems
    text = json.dumps(summary, indent=2, default=str)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
