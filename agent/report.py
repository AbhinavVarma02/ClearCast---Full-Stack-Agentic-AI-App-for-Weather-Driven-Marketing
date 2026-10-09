"""Readable Markdown renderings of a validated CampaignPlan.

The report keeps the original ClearCast sections (campaign windows, reasoning,
ad copy, risks, forecast summary) and adds validation status and evidence.
Weather numbers come from the evidence ledger, not from model text.
"""

from __future__ import annotations

from typing import Any

from agent.schemas import REVIEW_STATUS_LABELS, CampaignPlan, ReviewStatus

STATUS_NOTES = {
    ReviewStatus.PENDING_REVIEW: "Validated against retrieved weather evidence. Awaiting human review.",
    ReviewStatus.APPROVED: "Approved by a human reviewer for this exact plan revision.",
    ReviewStatus.REJECTED: "Rejected by a human reviewer. Not approved for use.",
    ReviewStatus.VALIDATION_FAILED: "Validation failed. This is NOT a usable recommendation.",
    ReviewStatus.DRAFT: "Draft. Not yet validated.",
}


def _num(value: Any, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    text = f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return f"{text}{unit}"


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _local(dt) -> str:
    return dt.strftime("%a %b %d, %H:%M") if dt else "n/a"


def status_label(plan: CampaignPlan) -> str:
    return REVIEW_STATUS_LABELS[plan.status]


def render_report(plan: CampaignPlan) -> str:
    loc = plan.resolved_location
    location = f"{loc.name}, {loc.state or loc.country}" if loc else plan.brief.location
    lines = [
        f"> **Status: {status_label(plan)}** — {STATUS_NOTES[plan.status]}",
        ">",
        f"> Client: {plan.client.display_name} · Location: {_cell(location)} "
        f"({loc.timezone_label if loc else 'timezone unresolved'}) · Request `{plan.request_id}` · "
        f"Revision {plan.revision}",
        "",
    ]
    if plan.status == ReviewStatus.VALIDATION_FAILED:
        lines += ["## Validation Failed", ""]
        lines += [f"- **{issue.code}**: {_cell(issue.message)}" for issue in plan.validation.errors] or [
            "- No eligible recommendation could be produced."
        ]
        lines += [
            "",
            "No campaign windows are recommended. Weather evidence retrieved for this request is "
            "summarised below so the brief or constraints can be adjusted.",
            "",
        ]
    else:
        lines += ["## Recommended Campaign Windows", ""]
        if plan.strategy_summary:
            lines += [_cell(plan.strategy_summary), ""]
        lines += [
            "| # | Window (local time) | Conditions from cited evidence | Evidence |",
            "|---|---|---|---|",
        ]
        for window in plan.windows:
            obs = window.observed_conditions
            conditions = (
                f"{_num(obs.temperature_min_f)}–{_num(obs.temperature_max_f)} °F · "
                f"precip. prob. ≤ {_num(obs.precipitation_probability_max_pct, '%', 0)} · "
                f"wind ≤ {_num(obs.wind_speed_max_mph, ' mph')}"
                + (f" · AQI ≤ {obs.aqi_max}" if obs.aqi_max is not None else "")
            )
            lines.append(
                f"| {window.window_id} | {_local(window.start_local)}–{window.end_local:%H:%M} ({window.daypart}) | "
                f"{conditions} | {', '.join(f'`{oid}`' for oid in window.observation_ids)} |"
            )
        lines += ["", "## Weather-Driven Reasoning", ""]
        for window in plan.windows:
            lines += [
                f"**{window.window_id} · {_cell(window.title)}**",
                "",
                f"- Weather reasoning: {_cell(window.weather_reasoning)}",
                f"- Marketing hypothesis (unverified): {_cell(window.marketing_hypothesis)}",
                "",
            ]
        lines += ["## Suggested Ad Copy", ""]
        for window in plan.windows:
            lines += [f"**{window.window_id} · {_cell(window.title)}**", ""]
            lines += [f"- “{_cell(copy)}”" for copy in window.ad_copy]
            lines.append("")
        lines += ["## Risk Notes", ""]
        risks = [(w.window_id, r) for w in plan.windows for r in w.risks] + [(None, r) for r in plan.risks]
        lines += [
            f"- {f'[{wid}] ' if wid else ''}**{_cell(r.risk)}** — Mitigation: {_cell(r.mitigation)}"
            + (f" (evidence: {', '.join(r.observation_ids)})" if r.observation_ids else "")
            for wid, r in risks
        ] or ["- No specific weather risks were identified."]
        lines.append("")
        if plan.rejected_windows:
            lines += ["### Windows rejected by hard client constraints", ""]
            for window in plan.rejected_windows:
                lines.append(
                    f"- {_cell(window.title)}: " + "; ".join(_cell(v) for v in window.constraint_check.violations)
                )
            lines.append("")

    lines += ["## Forecast Summary", ""]
    if plan.forecast_summary:
        lines += [
            "| Date (local) | Low °F | High °F | Max precip. prob. | Max wind mph | Conditions |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {day.date_local} | {_num(day.low_f)} | {_num(day.high_f)} | "
            f"{_num(day.max_precipitation_probability_pct, '%', 0)} | {_num(day.max_wind_speed_mph)} | "
            f"{_cell(day.conditions)} |"
            for day in plan.forecast_summary
        ]
    else:
        lines.append("No forecast evidence was retrieved for this request.")
    lines += ["", "## Validation & Review", ""]
    lines.append(
        f"- Validation: **{plan.validation.status}** "
        f"({len(plan.validation.errors)} errors, {len(plan.validation.warnings)} warnings, "
        f"{plan.validation.repair_attempts} repair attempts)"
    )
    lines.append(f"- Review status: **{status_label(plan)}**")
    if plan.review.note:
        lines.append(f"- Reviewer note: {_cell(plan.review.note)}")
    for issue in plan.validation.warnings:
        lines.append(f"- Warning ({issue.code}): {_cell(issue.message)}")
    lines += ["", "---", ""]
    lines += [f"*{_cell(text)}*" for text in plan.disclaimers]
    return "\n".join(lines)


def render_evidence(plan: CampaignPlan) -> str:
    """Per-window evidence tables so reviewers can check every cited observation."""
    lines: list[str] = []
    windows = list(plan.windows) + list(plan.rejected_windows)
    if not windows:
        lines.append("No campaign windows to inspect.")
    for window in windows:
        state = "eligible" if window in plan.windows else "rejected"
        lines += [
            f"### {window.window_id} · {_cell(window.title)} ({state})",
            "",
            f"Recommended: {_local(window.start_local)} – {_local(window.end_local)} "
            f"(UTC {window.start_utc:%Y-%m-%d %H:%M} – {window.end_utc:%H:%M})"
            if window.start_utc and window.end_utc
            else "Recommended time could not be parsed.",
            "",
            "| Observation | Local start | Temp °F | Feels °F | Precip. prob. | Rain mm/3h | Wind mph | AQI | Conditions |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for obs in window.evidence:
            lines.append(
                f"| `{obs.observation_id}` | {_local(obs.datetime_local)} | {_num(obs.temperature_f)} | "
                f"{_num(obs.feels_like_f)} | {_num(obs.precipitation_probability_pct, '%', 0)} | "
                f"{_num(obs.rain_volume_mm_3h)} | {_num(obs.wind_speed_mph)} | {_num(obs.aqi)} | "
                f"{_cell(obs.description)} |"
            )
        claimed = window.claimed_conditions
        lines += [
            "",
            f"- Model-claimed: temp {_num(claimed.temperature_min_f)}–{_num(claimed.temperature_max_f)} °F, "
            f"precip. prob. ≤ {_num(claimed.precipitation_probability_max_pct, '%', 0)}, "
            f"wind ≤ {_num(claimed.wind_speed_max_mph, ' mph')}, AQI ≤ {_num(claimed.aqi_max)}",
            f"- Grounding: {'verified' if window.grounding.verified else 'FAILED'}"
            + (f" — {'; '.join(_cell(i) for i in window.grounding.issues)}" if window.grounding.issues else ""),
            f"- Hard constraints: {'satisfied' if window.constraint_check.eligible else 'VIOLATED'}"
            + (
                f" — {'; '.join(_cell(v) for v in window.constraint_check.violations)}"
                if window.constraint_check.violations
                else ""
            ),
            "",
        ]
    ledger = plan.evidence
    lines += ["### Evidence sources", "", "| Tool | Status | Source | Fetched (UTC) | Cache |", "|---|---|---|---|---|"]
    for source in ledger.sources:
        cache = f"hit, {source.cache_age_seconds}s old" if source.cache_hit else "fresh"
        lines.append(
            f"| {source.tool} | {source.status}{f' ({source.error_category})' if source.error_category else ''} | "
            f"{_cell(source.source or 'n/a')} | "
            f"{source.fetched_at_utc:%Y-%m-%d %H:%M:%S} | {cache} |"
            if source.fetched_at_utc
            else f"| {source.tool} | {source.status}{f' ({source.error_category})' if source.error_category else ''} "
            f"| {_cell(source.source or 'n/a')} | n/a | n/a |"
        )
    if ledger.current:
        cur = ledger.current
        lines += [
            "",
            f"Current conditions (`{cur.observation_id}`): {_num(cur.temperature_f, ' °F')}, "
            f"wind {_num(cur.wind_speed_mph, ' mph')}, {_cell(cur.description or 'n/a')}.",
        ]
    if ledger.air_quality:
        air = ledger.air_quality
        lines.append(
            f"Air quality (`{air.observation_id}`): AQI {air.aqi} (OpenWeatherMap 1–5 scale); "
            f"AQI forecast blocks: {air.forecast_block_count}."
        )
    if ledger.issues:
        lines += ["", "Evidence notes:"]
        lines += [f"- {_cell(issue)}" for issue in ledger.issues]
    return "\n".join(lines)


def render_diagnostics(plan: CampaignPlan) -> str:
    d = plan.diagnostics
    usage = d.token_usage
    rows = [
        ("Request ID", f"`{d.request_id}`"),
        ("Session reference", f"`{d.session_ref}` (hashed)"),
        ("Demo client", d.client_id),
        ("Model", d.model),
        ("Total duration", f"{d.duration_ms} ms"),
        ("Agent (LangGraph) duration", f"{d.graph_duration_ms} ms"),
        ("Structured drafting duration", f"{d.drafting_duration_ms} ms"),
        ("LLM calls", str(d.llm_calls)),
        ("MCP tool calls", ", ".join(f"{name} × {count}" for name, count in d.tool_calls.items()) or "none"),
        ("Tool errors", str(d.tool_errors)),
        ("Provider error categories", ", ".join(d.provider_error_categories) or "none"),
        ("Weather cache hits", str(d.cache_hits)),
        ("Repair attempts", str(d.repair_attempts)),
        ("Graph recursion limit", f"{d.recursion_limit}{' (reached)' if d.step_limit_reached else ''}"),
        ("Validation", d.validation_status),
        ("Review status", d.review_status),
        (
            "Reported tokens",
            f"{usage.input_tokens} in / {usage.output_tokens} out ({usage.calls_with_usage} calls reported)"
            if usage
            else "not reported by provider",
        ),
        ("Estimated cost", f"${d.estimated_cost_usd:.6f}" if d.estimated_cost_usd is not None else d.cost_note),
    ]
    lines = ["| Metric | Value |", "|---|---|"] + [f"| {name} | {_cell(value)} |" for name, value in rows]
    lines += [
        "",
        "*Application-level diagnostics for this request only; not production monitoring or SLAs.*",
    ]
    return "\n".join(lines)
