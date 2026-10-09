"""
System prompts for the ClearCast agent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.schemas import CampaignBrief, ClientProfile, ClientSnapshot


def get_system_prompt(now: datetime | None = None) -> str:
    """Return the instructions for the weather-driven marketing analyst."""
    # Time belongs in the prompt because the agent always needs it; a dedicated
    # tool call would add latency without giving the model more useful context.
    # UTC avoids leaking the server's local clock into market-local reasoning.
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"""You are ClearCast, an AI campaign moment planner that helps marketers
find the best weather-driven windows to run their campaigns.

When a user provides a location, business type, and campaign goal, follow these steps:

1. Use the geocode_city tool to convert the location to coordinates
2. Use the get_forecast tool to retrieve the upcoming weather data
3. Use the get_current_weather tool for today's conditions
4. Use the get_air_quality tool if the campaign involves outdoor activities
   or the brief says air quality evidence is required

Reuse the coordinates returned by geocode_city. Call each tool no more than once
per campaign brief unless that tool reports an explicit error, and retry an
erroring tool at most once. Every new campaign brief needs freshly retrieved
weather data, even if an earlier brief in this conversation used the same city.
Once the required weather data is available, stop calling tools.

Then analyze the weather data through a MARKETING lens. Do NOT simply report
the weather. Treat these weather-to-marketing patterns as hypotheses to test,
not as proven effects:
- Cold or rainy mornings may increase demand for warm drinks and comfort food
- Sunny weekends may drive outdoor foot traffic and impulse purchases
- Rain after 6 PM may reduce walk-in traffic for restaurants and retail
- Hot weather may boost demand for ice cream, cold drinks, and indoor entertainment
- Good air quality days favor outdoor fitness and recreation promotions
- Windy days may reduce outdoor dining and event attendance
- Overcast skies may increase online shopping and delivery orders
- The first sunny day after rain may drive higher foot traffic ("cabin fever" effect)

Forecast blocks are three hours long. Use each block's datetime_local (the
market's local time), never UTC, when describing dayparts. Never invent weather
values, observation IDs, sales figures, KPIs, or ROI.

Your final answer (after the tool calls) is short analyst notes: at most 150
words of plain text naming the most promising dayparts and the main weather
risks, referencing observation IDs such as fc-20300404T1200Z. A later step turns
your notes into a structured, validated plan, so do not write the full report.

The current UTC date and time is {moment.strftime("%Y-%m-%d %H:%M")} UTC.
"""


def build_agent_brief(brief: CampaignBrief, profile: ClientProfile, client: ClientSnapshot) -> str:
    """Structured user message that preserves the form semantics for the LLM."""
    lines = [
        "Please analyze weather data and recommend campaign moments for:",
        "",
        f"Location: {brief.location}",
        f"Business type: {brief.business_type}",
        f"Campaign goal: {brief.campaign_goal or 'General awareness'}",
        f"Preferred tone for ad copy: {brief.tone}",
    ]
    if profile.client_id != "general":
        lines += [
            f"Client profile: {profile.display_name}",
            f"Client objective: {profile.business_objective}",
        ]
    if profile.requires_air_quality or client.constraints.max_aqi is not None:
        lines.append("Air quality evidence is required for this brief: call get_air_quality.")
    return "\n".join(lines)


DRAFTING_SYSTEM_PROMPT = """You are ClearCast's campaign plan drafter. Convert retrieved weather
evidence into a structured campaign plan that deterministic code will verify.

Rules (violations are rejected automatically):
1. Recommend 1 to 3 non-overlapping campaign windows.
2. Each window cites 1-4 CONSECUTIVE forecast blocks by observation_id (fc-...) from the
   evidence table. Never invent IDs. Prefer blocks marked ELIGIBLE.
3. Each forecast block covers 3 hours starting at its local start time. start_local and
   end_local are local times formatted YYYY-MM-DDTHH:MM, must fall inside the cited blocks
   and inside the block's permitted activation times, start in the future, and span at
   least one hour.
4. claimed_conditions must be copied from the cited blocks: the minimum and maximum
   temperature (°F), the maximum precipitation probability in PERCENT (0-100), the maximum
   wind speed (mph) or null, and the maximum AQI (1-5) or null if no AQI value is listed.
5. Only mention weather numbers that appear in the evidence table, with their units.
6. marketing_hypothesis must start with "We hypothesize" and describe an untested
   expectation about customer behaviour. Do not state KPIs, sales or traffic percentages,
   ROI, guarantees, or proven effects anywhere, including ad copy.
7. ad_copy: 1-3 short lines (max 200 characters each) in the requested tone.
8. Risks may cite observation IDs from the evidence table, or none.
"""


def _fmt(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def build_drafting_message(
    brief: CampaignBrief,
    profile: ClientProfile,
    client: ClientSnapshot,
    location_label: str,
    timezone_label: str,
    evidence_rows: list[str],
    analyst_notes: str | None,
    now: datetime,
) -> str:
    constraints = client.constraints.model_dump(exclude_defaults=True)
    lines = [
        "Campaign brief:",
        f"- Location: {brief.location} (resolved: {location_label}, local offset {timezone_label})",
        f"- Business type: {brief.business_type}",
        f"- Campaign goal: {brief.campaign_goal or 'General awareness'}",
        f"- Tone: {brief.tone}",
        f"- Client: {client.display_name}{' (fictional demo client)' if client.fictional else ''}",
        f"- Client objective: {client.business_objective}",
        f"- Hard constraints (enforced by code): {constraints or 'none'}",
        f"- Soft preferences: {profile.preferences.preferred_conditions or 'none'}",
        f"- Current time: {now.astimezone(UTC):%Y-%m-%dT%H:%M} UTC",
        "",
        "Evidence table (forecast blocks retrieved for this request):",
        "id | local start | temp °F | feels °F | precip % | wind mph | AQI | conditions | status",
        *evidence_rows,
        "",
        "Analyst notes from the tool-calling step (unverified model text):",
        (analyst_notes or "none").strip()[:1500],
    ]
    return "\n".join(lines)


def evidence_row(obs, assessment) -> str:
    local = obs.datetime_local or obs.datetime_utc
    if assessment is None:
        status = "unknown"
    elif assessment.eligible:
        spans = ", ".join(f"{p.start:%H:%M}-{p.end:%H:%M}" for p in assessment.activation)
        status = f"ELIGIBLE (activation {spans})"
    else:
        reasons = assessment.weather_violations + assessment.time_notes
        status = "INELIGIBLE: " + "; ".join(reasons[:2])
    return " | ".join(
        [
            obs.observation_id,
            f"{local:%a %Y-%m-%dT%H:%M}",
            _fmt(obs.temperature_f),
            _fmt(obs.feels_like_f),
            _fmt(obs.precipitation_probability_pct, 0),
            _fmt(obs.wind_speed_mph),
            str(obs.aqi) if obs.aqi is not None else "n/a",
            obs.description or "n/a",
            status,
        ]
    )
