"""Evidence ledger: weather provenance built by Python from tool outputs.

The ledger is reconstructed from the ToolMessages that LangGraph recorded for
*this* request (the messages after the request's own HumanMessage). Model text
is never a source of evidence; observation IDs cited by the model are checked
against this ledger.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent.schemas import (
    AirQualityObservation,
    CurrentObservation,
    DailyForecastSummary,
    EvidenceLedger,
    EvidenceSource,
    ForecastObservation,
    ResolvedLocation,
    ValidationIssue,
)

BLOCK = timedelta(hours=3)
STALE_FORECAST_AFTER = timedelta(hours=3)
STALE_CURRENT_AFTER = timedelta(hours=3)
CLOCK_SKEW_ALLOWANCE = timedelta(minutes=5)
COORDINATE_TOLERANCE_DEG = 0.05
ERROR_CATEGORY_RE = re.compile(r"\[([a-z][a-z_]+)\]")
UNITS = {
    "temperature": "°F",
    "wind_speed": "mph",
    "precipitation_probability": "% (0-100)",
    "rain_volume": "mm per 3-hour block",
    "humidity": "%",
    "aqi": "OpenWeatherMap AQI 1-5 (1=Good, 5=Very Poor)",
    "time": "local times use the UTC offset OpenWeatherMap reports for the resolved location",
}


@dataclass
class ToolResult:
    name: str
    args: dict
    status: str  # "ok" | "error"
    payload: dict | None
    error_category: str | None


def turn_messages(messages: list[BaseMessage], turn_id: str) -> list[BaseMessage]:
    """Return the messages that belong to one request (its HumanMessage onwards)."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage) and message.id == turn_id:
            return list(messages[index:])
    return []


def extract_tool_results(turn: list[BaseMessage]) -> list[ToolResult]:
    calls: dict[str, dict] = {}
    for message in turn:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call["id"]] = call
    results: list[ToolResult] = []
    for message in turn:
        if not isinstance(message, ToolMessage):
            continue
        call = calls.get(message.tool_call_id, {})
        name = message.name or call.get("name") or "unknown"
        args = call.get("args") or {}
        content = message.content if isinstance(message.content, str) else json.dumps(message.content)
        if message.status == "error":
            match = ERROR_CATEGORY_RE.search(content)
            category = match.group(1) if match else "tool_error"
            results.append(ToolResult(name, args, "error", None, category))
            continue
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            results.append(ToolResult(name, args, "error", None, "malformed_tool_output"))
            continue
        results.append(ToolResult(name, args, "ok", payload, None))
    return results


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def offset_label(seconds: int | None) -> str:
    if seconds is None:
        return "unresolved"
    sign = "+" if seconds >= 0 else "-"
    minutes = abs(seconds) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def tzinfo_for(seconds: int | None) -> timezone:
    return timezone(timedelta(seconds=seconds or 0))


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _issue(code: str, message: str, severity: str = "error") -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=severity)


def _coords_differ(args: dict, location: ResolvedLocation) -> bool:
    lat, lon = _float(args.get("lat")), _float(args.get("lon"))
    if lat is None or lon is None:
        return False
    return abs(lat - location.lat) > COORDINATE_TOLERANCE_DEG or abs(lon - location.lon) > COORDINATE_TOLERANCE_DEG


def _latest_ok(results: list[ToolResult], name: str) -> ToolResult | None:
    matching = [result for result in results if result.name == name and result.status == "ok"]
    return matching[-1] if matching else None


def _source(result: ToolResult) -> EvidenceSource:
    payload = result.payload or {}
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else {}
    return EvidenceSource(
        tool=result.name,
        status="ok" if result.status == "ok" else "error",
        source=payload.get("source") if isinstance(payload.get("source"), str) else None,
        fetched_at_utc=parse_timestamp(payload.get("fetched_at_utc")),
        cache_hit=bool(cache.get("hit")),
        cache_age_seconds=int(cache.get("age_seconds") or 0),
        error_category=result.error_category,
    )


def _location(geo: ToolResult | None, offset: int | None, tz_source: str) -> ResolvedLocation | None:
    if geo is None or geo.payload is None:
        return None
    payload = geo.payload
    lat, lon = _float(payload.get("lat")), _float(payload.get("lon"))
    if lat is None or lon is None:
        return None
    return ResolvedLocation(
        name=str(payload.get("name") or ""),
        state=str(payload.get("state") or ""),
        country=str(payload.get("country") or ""),
        lat=lat,
        lon=lon,
        timezone_offset_seconds=offset,
        timezone_label=offset_label(offset),
        timezone_source=tz_source,
        observation_id=str(payload.get("observation_id") or ""),
    )


def _air_blocks(air: ToolResult | None) -> dict[str, tuple[int, str]]:
    """Map forecast block start (UTC ISO) -> (max AQI, AQI forecast observation id)."""
    blocks: dict[str, tuple[int, str]] = {}
    if air is None or air.payload is None:
        return blocks
    for item in air.payload.get("forecast") or []:
        start = parse_timestamp(item.get("datetime_utc"))
        aqi = item.get("aqi_max")
        if start is None or not isinstance(aqi, int) or not isinstance(item.get("observation_id"), str):
            continue
        blocks[start.isoformat()] = (aqi, item["observation_id"])
    return blocks


def _forecast_observations(
    forecast: ToolResult | None, offset: int | None, air: dict[str, tuple[int, str]]
) -> list[ForecastObservation]:
    if forecast is None or forecast.payload is None:
        return []
    tz = tzinfo_for(offset) if offset is not None else None
    observations = []
    for entry in forecast.payload.get("forecast") or []:
        start = parse_timestamp(entry.get("datetime_utc"))
        if start is None or not isinstance(entry.get("observation_id"), str):
            continue
        pop = _float(entry.get("rain_probability"))
        aqi = air.get(start.isoformat())
        observations.append(
            ForecastObservation(
                observation_id=entry["observation_id"],
                datetime_utc=start,
                datetime_local=start.astimezone(tz) if tz else None,
                temperature_f=_float(entry.get("temperature")),
                feels_like_f=_float(entry.get("feels_like")),
                humidity_pct=_float(entry.get("humidity")),
                wind_speed_mph=_float(entry.get("wind_speed")),
                # Converted once, here, so every downstream number is a percentage.
                precipitation_probability_pct=round(pop * 100, 1) if pop is not None else None,
                rain_volume_mm_3h=_float(entry.get("rain_volume_3h")) or 0.0,
                description=entry.get("description") if isinstance(entry.get("description"), str) else None,
                aqi=aqi[0] if aqi else None,
                aqi_observation_id=aqi[1] if aqi else None,
                missing_fields=[str(name) for name in entry.get("missing_fields") or []],
            )
        )
    observations.sort(key=lambda obs: obs.datetime_utc)
    return observations


def _current(result: ToolResult | None) -> CurrentObservation | None:
    if result is None or result.payload is None:
        return None
    payload = result.payload
    observed = parse_timestamp(payload.get("observed_at_utc"))
    temperature = _float(payload.get("temperature"))
    if observed is None or temperature is None or not isinstance(payload.get("observation_id"), str):
        return None
    return CurrentObservation(
        observation_id=payload["observation_id"],
        observed_at_utc=observed,
        temperature_f=temperature,
        feels_like_f=_float(payload.get("feels_like")),
        humidity_pct=_float(payload.get("humidity")),
        wind_speed_mph=_float(payload.get("wind_speed")),
        cloud_cover_pct=_float(payload.get("cloud_cover")),
        description=payload.get("description") if isinstance(payload.get("description"), str) else None,
        missing_fields=[str(name) for name in payload.get("missing_fields") or []],
    )


def _air_quality(result: ToolResult | None, block_count: int) -> AirQualityObservation | None:
    if result is None or result.payload is None:
        return None
    payload = result.payload
    observed = parse_timestamp(payload.get("observed_at_utc"))
    aqi = payload.get("aqi")
    if observed is None or not isinstance(aqi, int) or not isinstance(payload.get("observation_id"), str):
        return None
    return AirQualityObservation(
        observation_id=payload["observation_id"],
        observed_at_utc=observed,
        aqi=aqi,
        pm2_5_ugm3=_float(payload.get("pm2_5")),
        pm10_ugm3=_float(payload.get("pm10")),
        o3_ugm3=_float(payload.get("o3")),
        forecast_available=bool(payload.get("forecast_available")),
        forecast_block_count=block_count,
    )


def build_ledger(results: list[ToolResult], *, now: datetime) -> tuple[EvidenceLedger, list[ValidationIssue]]:
    """Build the evidence ledger and report missing, stale, or inconsistent evidence."""
    findings: list[ValidationIssue] = []
    geo = _latest_ok(results, "geocode_city")
    forecast = _latest_ok(results, "get_forecast")
    current_result = _latest_ok(results, "get_current_weather")
    air_result = _latest_ok(results, "get_air_quality")

    offset, tz_source = None, "unresolved"
    for result, label in ((forecast, "get_forecast"), (current_result, "get_current_weather")):
        value = (result.payload or {}).get("timezone_offset_seconds") if result else None
        if isinstance(value, int):
            offset, tz_source = value, f"OpenWeatherMap {label} timezone offset"
            break

    location = _location(geo, offset, tz_source)
    air_blocks = _air_blocks(air_result)
    observations = _forecast_observations(forecast, offset, air_blocks)
    ledger = EvidenceLedger(
        location=location,
        forecast=observations,
        current=_current(current_result),
        air_quality=_air_quality(air_result, len(air_blocks)),
        sources=[_source(result) for result in results],
        units=UNITS,
        issues=[],
    )

    errors_by_tool = {r.name: r.error_category for r in results if r.status == "error"}
    if location is None:
        detail = f" ({errors_by_tool['geocode_city']})" if "geocode_city" in errors_by_tool else ""
        findings.append(
            _issue("missing_location_evidence", f"No resolved location was retrieved for this request{detail}.")
        )
    if forecast is None:
        detail = f" ({errors_by_tool['get_forecast']})" if "get_forecast" in errors_by_tool else ""
        findings.append(
            _issue("missing_forecast_evidence", f"No forecast evidence was retrieved for this request{detail}.")
        )
    elif not observations:
        findings.append(_issue("forecast_empty", "The forecast response contained no usable observations."))
    if forecast is not None and offset is None:
        findings.append(
            _issue("timezone_unresolved", "The location's UTC offset was not reported; local times cannot be verified.")
        )

    if location is not None:
        for result in results:
            if result.status == "ok" and result.name != "geocode_city" and _coords_differ(result.args, location):
                findings.append(
                    _issue(
                        "coordinates_mismatch",
                        f"{result.name} was called for coordinates that differ from the resolved location.",
                    )
                )

    for source in ledger.sources:
        if source.status != "ok" or source.fetched_at_utc is None:
            continue
        age = now - source.fetched_at_utc
        limit = STALE_FORECAST_AFTER if source.tool == "get_forecast" else STALE_CURRENT_AFTER
        if age > limit:
            severity = "error" if source.tool == "get_forecast" else "warning"
            findings.append(
                _issue(
                    "stale_evidence",
                    f"{source.tool} data was fetched {int(age.total_seconds() // 60)} minutes before this request.",
                    severity,
                )
            )
        elif age < -CLOCK_SKEW_ALLOWANCE:
            findings.append(
                _issue("clock_skew", f"{source.tool} reports a fetch time later than the request time.", "warning")
            )

    for name, category in errors_by_tool.items():
        findings.append(_issue("provider_error", f"{name} failed ({category}).", "warning"))
    if current_result is None:
        findings.append(_issue("current_unavailable", "Current conditions were not retrieved.", "warning"))
    if air_result is None:
        findings.append(_issue("air_quality_unavailable", "Air quality was not retrieved.", "warning"))
    elif not air_blocks:
        findings.append(
            _issue("air_quality_forecast_unavailable", "No AQI forecast is available for forecast blocks.", "warning")
        )
    incomplete = [obs.observation_id for obs in observations if obs.missing_fields]
    if incomplete:
        findings.append(
            _issue(
                "forecast_fields_missing",
                f"{len(incomplete)} forecast blocks are missing fields; constraints that need them cannot be verified.",
                "warning",
            )
        )

    ledger.issues = [finding.message for finding in findings]
    return ledger, findings


def daily_summary(observations: list[ForecastObservation]) -> list[DailyForecastSummary]:
    """Deterministic per-day rollup used for the report's forecast summary table."""
    days: dict[str, list[ForecastObservation]] = {}
    for obs in observations:
        moment = obs.datetime_local or obs.datetime_utc
        days.setdefault(moment.date().isoformat(), []).append(obs)

    def _max(values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return round(max(present), 1) if present else None

    def _min(values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return round(min(present), 1) if present else None

    summary = []
    for day, blocks in days.items():
        conditions = Counter(obs.description for obs in blocks if obs.description)
        summary.append(
            DailyForecastSummary(
                date_local=day,
                low_f=_min([obs.temperature_f for obs in blocks]),
                high_f=_max([obs.temperature_f for obs in blocks]),
                max_precipitation_probability_pct=_max([obs.precipitation_probability_pct for obs in blocks]),
                max_wind_speed_mph=_max([obs.wind_speed_mph for obs in blocks]),
                conditions=conditions.most_common(1)[0][0] if conditions else "unknown",
                block_count=len(blocks),
            )
        )
    return summary


def known_observation_ids(ledger: EvidenceLedger) -> set[str]:
    ids = {obs.observation_id for obs in ledger.forecast}
    ids.update(obs.aqi_observation_id for obs in ledger.forecast if obs.aqi_observation_id)
    if ledger.current:
        ids.add(ledger.current.observation_id)
    if ledger.air_quality:
        ids.add(ledger.air_quality.observation_id)
    if ledger.location and ledger.location.observation_id:
        ids.add(ledger.location.observation_id)
    return ids
