"""Evidence ledger: built only from this request's tool outputs, with provenance checks."""

from __future__ import annotations

import json
from datetime import timedelta

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.evidence import build_ledger, daily_summary, extract_tool_results, offset_label, turn_messages
from evaluation.harness import FIXED_NOW
from tests.validation_helpers import ledger_for


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "name": name, "args": args}


def test_turn_messages_only_include_the_current_request():
    messages = [
        HumanMessage("old brief", id="turn-old"),
        ToolMessage('{"x": 1}', tool_call_id="a", name="get_forecast"),
        HumanMessage("new brief", id="turn-new"),
        AIMessage("notes"),
    ]
    assert [m.content for m in turn_messages(messages, "turn-new")] == ["new brief", "notes"]
    assert turn_messages(messages, "turn-missing") == []


def test_tool_messages_are_parsed_with_error_categories():
    turn = [
        HumanMessage("brief", id="t"),
        AIMessage(
            "", tool_calls=[_call("1", "geocode_city", {"city": "X"}), _call("2", "get_forecast", {"lat": 1, "lon": 2})]
        ),
        ToolMessage(json.dumps({"lat": 1.0, "lon": 2.0, "name": "X"}), tool_call_id="1", name="geocode_city"),
        ToolMessage(
            "Error: Error executing tool get_forecast: [provider_rate_limited] OpenWeatherMap rate limit reached",
            tool_call_id="2",
            name="get_forecast",
            status="error",
        ),
        ToolMessage("not json", tool_call_id="3", name="get_air_quality"),
    ]
    results = extract_tool_results(turn)
    assert [(r.name, r.status, r.error_category) for r in results] == [
        ("geocode_city", "ok", None),
        ("get_forecast", "error", "provider_rate_limited"),
        ("get_air_quality", "error", "malformed_tool_output"),
    ]
    ledger, findings = build_ledger(results, now=FIXED_NOW)
    codes = {f.code: f for f in findings}
    assert "missing_forecast_evidence" in codes
    assert "provider_rate_limited" in codes["missing_forecast_evidence"].message
    assert ledger.forecast == []


def test_ledger_converts_units_and_aligns_air_quality_blocks():
    ledger = ledger_for("poor_air")
    block = ledger.forecast[0]
    assert ledger.location.timezone_label == "UTC-04:00"
    assert block.datetime_local.utcoffset() == timedelta(hours=-4)
    assert block.precipitation_probability_pct == 10.0  # converted from the 0.1 fraction
    assert block.aqi == 4 and block.aqi_observation_id.startswith("aqf-")
    assert ledger.units["precipitation_probability"].startswith("%")


def test_stale_and_mismatched_evidence_is_flagged():
    # Forecast fetched four hours before the request time.
    stale = ledger_for("baseline_mild", now=FIXED_NOW, fetched_at=FIXED_NOW - timedelta(hours=4))
    assert stale.issues and any("minutes before this request" in issue for issue in stale.issues)

    turn = [
        HumanMessage("brief", id="t"),
        AIMessage(
            "", tool_calls=[_call("1", "geocode_city", {}), _call("2", "get_forecast", {"lat": 10.0, "lon": 10.0})]
        ),
        ToolMessage(
            json.dumps({"lat": 39.3, "lon": -76.6, "name": "Baltimore", "observation_id": "geo-1"}),
            tool_call_id="1",
            name="geocode_city",
        ),
        ToolMessage(
            json.dumps({"timezone_offset_seconds": -14400, "forecast": []}), tool_call_id="2", name="get_forecast"
        ),
    ]
    _, findings = build_ledger(extract_tool_results(turn), now=FIXED_NOW)
    assert "coordinates_mismatch" in {f.code for f in findings}
    assert "forecast_empty" in {f.code for f in findings}


def test_daily_summary_is_computed_from_evidence():
    ledger = ledger_for("clear_warm")
    summary = daily_summary(ledger.forecast)
    assert sum(day.block_count for day in summary) == len(ledger.forecast)
    full_day = next(day for day in summary if day.block_count == 8)
    assert full_day.low_f < full_day.high_f
    assert full_day.max_precipitation_probability_pct == 0.0
    assert full_day.conditions == "clear sky"


def test_offset_labels():
    assert offset_label(-14400) == "UTC-04:00"
    assert offset_label(19800) == "UTC+05:30"
    assert offset_label(None) == "unresolved"
