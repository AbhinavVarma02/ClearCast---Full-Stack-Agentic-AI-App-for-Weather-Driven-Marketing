"""Build real evidence ledgers and drafts for validation tests (no model involved)."""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.evidence import ToolResult, build_ledger
from agent.schemas import CampaignDraft, ClientConstraints, EvidenceLedger
from agent.validation import assess_blocks, validate_draft
from evaluation.harness import FIXED_NOW
from mcp_server.fixtures import FixtureTransport
from mcp_server.weather_api import OpenWeatherMapClient


def ledger_for(spec: str = "baseline_mild", *, now: datetime = FIXED_NOW, air: bool = True, fetched_at=None):
    fetch_clock = (lambda: fetched_at) if fetched_at else (lambda: now)
    client = OpenWeatherMapClient(
        transport=FixtureTransport(spec, clock=fetch_clock),
        clock=fetch_clock,
        require_api_key=False,
        source=f"fixture:{spec}",
    )
    geo = client.geocode_city("Baltimore, MD")
    coords = {"lat": geo["lat"], "lon": geo["lon"]}
    results = [
        ToolResult("geocode_city", {"city": "Baltimore, MD"}, "ok", geo, None),
        ToolResult("get_forecast", coords, "ok", client.get_forecast(**coords), None),
        ToolResult("get_current_weather", coords, "ok", client.get_current_weather(**coords), None),
    ]
    if air:
        results.append(ToolResult("get_air_quality", coords, "ok", client.get_air_quality(**coords), None))
    return build_ledger(results, now=now)[0]


def local(obs) -> datetime:
    return obs.datetime_local


def find_block(ledger: EvidenceLedger, day: int, hour: int):
    """Forecast block whose local start is on 2030-04-<day> at <hour>:00."""
    return next(o for o in ledger.forecast if o.datetime_local.day == day and o.datetime_local.hour == hour)


def window(ledger: EvidenceLedger, blocks, start: datetime | None = None, end: datetime | None = None, **overrides):
    """A draft window whose restated values are copied exactly from the cited evidence."""
    start = start or local(blocks[0])
    end = end or local(blocks[-1]) + timedelta(hours=3)
    data = {
        "title": "Test window",
        "observation_ids": [b.observation_id for b in blocks],
        "start_local": f"{start:%Y-%m-%dT%H:%M}",
        "end_local": f"{end:%Y-%m-%dT%H:%M}",
        "claimed_conditions": {
            "cited_values": [
                {
                    "observation_id": b.observation_id,
                    "temperature_f": b.temperature_f,
                    "precipitation_probability_pct": b.precipitation_probability_pct,
                    "wind_speed_mph": b.wind_speed_mph,
                    "aqi": b.aqi,
                }
                for b in blocks
            ],
            "conditions_summary": blocks[0].description or "",
        },
        "weather_reasoning": "Mild conditions suit the brief.",
        "marketing_hypothesis": "We hypothesize that mild weather may prompt more visits.",
        "ad_copy": ["Stop by today."],
        "risks": [],
    }
    first_claim = data["claimed_conditions"]["cited_values"][0]
    for key, value in overrides.items():
        if key in first_claim:
            first_claim[key] = value  # claim overrides apply to the first cited block
        else:
            data[key] = value
    return data


def draft(*windows, summary: str = "Plan summary.", overall_risks=None) -> CampaignDraft:
    return CampaignDraft.model_validate(
        {"strategy_summary": summary, "windows": list(windows), "overall_risks": overall_risks or []}
    )


def validate(
    ledger: EvidenceLedger,
    plan_draft: CampaignDraft,
    constraints: ClientConstraints | None = None,
    now: datetime = FIXED_NOW,
):
    constraints = constraints or ClientConstraints()
    assessments = assess_blocks(ledger, constraints, now)
    return validate_draft(plan_draft, ledger, assessments, constraints, now)


def codes(result) -> set[str]:
    return {issue.code for issue in result.grounding_errors}
