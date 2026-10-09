"""Deterministic validation of model-proposed campaign drafts.

The model proposes windows, cites forecast observation IDs, and restates the
conditions it relied on. This module checks every factual claim against the
evidence ledger and applies the client's hard constraints. The model cannot
override any decision made here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from agent.evidence import BLOCK, known_observation_ids, tzinfo_for
from agent.schemas import (
    WEEKDAYS,
    CampaignDraft,
    ClientConstraints,
    EvidenceLedger,
    ForecastObservation,
    ObservedConditions,
    RiskDraft,
    ValidationIssue,
    WindowDraft,
)

MAX_WINDOWS = 3
MAX_BLOCKS_PER_WINDOW = 2
MIN_WINDOW = timedelta(hours=1)
TEMPERATURE_TOLERANCE_F = 0.6
PERCENT_TOLERANCE = 1.0
WIND_TOLERANCE_MPH = 0.6
TEXT_TEMPERATURE_TOLERANCE_F = 1.0
MAX_TEXT_LENGTH = 1200
MAX_AD_COPY_LENGTH = 200

TEMPERATURE_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°|º)\s*[FC]?|(-?\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg)\b", re.I)
WIND_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:mph|miles per hour)\b", re.I)
PRECIP_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:chance|probability|precip\w*|rain|showers?)"
    r"|(?:chance|probability|precip\w*|rain|showers?)\D{0,25}?(\d{1,3}(?:\.\d+)?)\s*%",
    re.I,
)
# Claims of measured or guaranteed business outcomes. The model may propose
# hypotheses, but not KPIs, ROI, or proven demand effects.
BUSINESS_CLAIM_PATTERNS = [
    re.compile(r"\broi\b|\broas\b|return on (?:ad(?:vertising)? )?(?:spend|investment)", re.I),
    re.compile(r"\bguarantee[ds]?\b|\bproven\b|\bwill (?:definitely|certainly)\b", re.I),
    re.compile(r"\b(?:increase|boost|lift|raise|grow|drive|improve)\w*\b[^.]{0,40}?\bby\s+\d+(?:\.\d+)?\s*%", re.I),
    re.compile(r"\d+(?:\.\d+)?\s*%\s*(?:increase|lift|uplift|boost|more|higher|growth|rise)", re.I),
    re.compile(r"\b(?:conversion rate|click-through rate|ctr)\b[^.]{0,30}\d", re.I),
]
AD_COPY_CLAIM_PATTERNS = BUSINESS_CLAIM_PATTERNS[:2]
HEDGE_RE = re.compile(r"\b(?:hypothes\w*|may|might|could|expect\w*|likely|test\w*|suspect)\b", re.I)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


def _intersect(a: Interval, b: Interval) -> Interval | None:
    start, end = max(a.start, b.start), min(a.end, b.end)
    return Interval(start, end) if start < end else None


def _subtract(intervals: list[Interval], cut: Interval) -> list[Interval]:
    result = []
    for interval in intervals:
        if not interval.overlaps(cut):
            result.append(interval)
            continue
        if interval.start < cut.start:
            result.append(Interval(interval.start, cut.start))
        if cut.end < interval.end:
            result.append(Interval(cut.end, interval.end))
    return result


def _merge(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for interval in sorted(intervals, key=lambda item: item.start):
        if merged and interval.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return merged


def _covers(intervals: list[Interval], target: Interval) -> bool:
    return any(item.start <= target.start and target.end <= item.end for item in _merge(intervals))


def _local_day_start(day, hours: int, tz: timezone) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz) + timedelta(hours=hours)


@dataclass
class BlockAssessment:
    """Constraint evaluation for one three-hour forecast block."""

    observation: ForecastObservation
    interval: Interval
    weather_violations: list[str]
    time_notes: list[str]
    activation: list[Interval]

    @property
    def eligible(self) -> bool:
        return not self.weather_violations and bool(self.activation)


def _weather_violations(obs: ForecastObservation, c: ClientConstraints) -> list[str]:
    violations = []
    temp = obs.temperature_f
    if c.min_temperature_f is not None:
        if temp is None:
            violations.append("temperature missing; minimum temperature cannot be verified")
        elif temp < c.min_temperature_f:
            violations.append(f"temperature {temp:.1f} °F is below the {c.min_temperature_f:g} °F minimum")
    if c.max_temperature_f is not None:
        if temp is None:
            violations.append("temperature missing; maximum temperature cannot be verified")
        elif temp > c.max_temperature_f:
            violations.append(f"temperature {temp:.1f} °F is above the {c.max_temperature_f:g} °F maximum")
    if c.max_precipitation_probability_pct is not None:
        pop = obs.precipitation_probability_pct
        if pop is None:
            violations.append("precipitation probability missing; precipitation limit cannot be verified")
        elif pop > c.max_precipitation_probability_pct:
            violations.append(
                f"precipitation probability {pop:g}% exceeds the {c.max_precipitation_probability_pct:g}% limit"
            )
    if c.max_wind_speed_mph is not None:
        wind = obs.wind_speed_mph
        if wind is None:
            violations.append("wind speed missing; wind limit cannot be verified")
        elif wind > c.max_wind_speed_mph:
            violations.append(f"wind {wind:.1f} mph exceeds the {c.max_wind_speed_mph:g} mph limit")
    if c.max_aqi is not None:
        if obs.aqi is None:
            violations.append("AQI forecast unavailable for this block; AQI limit cannot be verified")
        elif obs.aqi > c.max_aqi:
            violations.append(f"AQI {obs.aqi} exceeds the limit of {c.max_aqi}")
    return violations


def _activation(
    interval: Interval, c: ClientConstraints, tz: timezone, now: datetime
) -> tuple[list[Interval], list[str]]:
    """Return the allowed, non-excluded, future sub-intervals of a block."""
    notes: list[str] = []
    if interval.end <= now:
        return [], ["block has already ended"]
    days = sorted({interval.start.date(), (interval.end - timedelta(microseconds=1)).date()})
    if c.allowed_hours:
        allowed = []
        for day in days:
            for hours in c.allowed_hours:
                window = Interval(
                    _local_day_start(day, hours.start_hour, tz), _local_day_start(day, hours.end_hour, tz)
                )
                if part := _intersect(interval, window):
                    allowed.append(part)
        if not allowed:
            notes.append("outside the client's allowed hours")
    else:
        allowed = [interval]
    for day in days:
        weekday = WEEKDAYS[day.weekday()]
        for exclusion in c.exclusion_windows:
            if weekday not in exclusion.weekdays:
                continue
            cut = Interval(
                _local_day_start(day, exclusion.start_hour, tz), _local_day_start(day, exclusion.end_hour, tz)
            )
            if any(part.overlaps(cut) for part in allowed):
                notes.append(f"excluded: {exclusion.reason or 'client exclusion window'}")
            allowed = _subtract(allowed, cut)
    future = []
    for part in allowed:
        if part.end <= now:
            continue
        future.append(Interval(max(part.start, now), part.end))
    if allowed and not future:
        notes.append("allowed hours in this block have already passed")
    return _merge(future), notes


def assess_blocks(ledger: EvidenceLedger, constraints: ClientConstraints, now: datetime) -> dict[str, BlockAssessment]:
    tz = tzinfo_for(ledger.location.timezone_offset_seconds if ledger.location else None)
    if ledger.location is None or ledger.location.timezone_offset_seconds is None:
        tz = tzinfo_for(_offset_from_forecast(ledger))
    assessments = {}
    for obs in ledger.forecast:
        start = obs.datetime_utc.astimezone(tz)
        interval = Interval(start, start + BLOCK)
        activation, notes = _activation(interval, constraints, tz, now)
        assessments[obs.observation_id] = BlockAssessment(
            observation=obs,
            interval=interval,
            weather_violations=_weather_violations(obs, constraints),
            time_notes=notes,
            activation=activation,
        )
    return assessments


def _offset_from_forecast(ledger: EvidenceLedger) -> int | None:
    for obs in ledger.forecast:
        if obs.datetime_local is not None and obs.datetime_local.utcoffset() is not None:
            return int(obs.datetime_local.utcoffset().total_seconds())
    return None


def observed_conditions(blocks: list[ForecastObservation]) -> ObservedConditions:
    def values(attr: str) -> list[float]:
        return [getattr(obs, attr) for obs in blocks if getattr(obs, attr) is not None]

    temps, feels = values("temperature_f"), values("feels_like_f")
    pops, winds = values("precipitation_probability_pct"), values("wind_speed_mph")
    aqis = [obs.aqi for obs in blocks if obs.aqi is not None]
    complete_aqi = len(aqis) == len(blocks) and blocks
    conditions = list(dict.fromkeys(obs.description for obs in blocks if obs.description))
    return ObservedConditions(
        temperature_min_f=min(temps) if len(temps) == len(blocks) and temps else None,
        temperature_max_f=max(temps) if len(temps) == len(blocks) and temps else None,
        feels_like_min_f=min(feels) if feels else None,
        precipitation_probability_max_pct=max(pops) if len(pops) == len(blocks) and pops else None,
        wind_speed_max_mph=max(winds) if len(winds) == len(blocks) and winds else None,
        rain_volume_total_mm=round(sum(obs.rain_volume_mm_3h for obs in blocks), 2),
        aqi_max=max(aqis) if complete_aqi else None,
        conditions=conditions,
    )


@dataclass
class WindowValidation:
    window_id: str
    draft: WindowDraft
    blocks: list[ForecastObservation] = field(default_factory=list)
    start_local: datetime | None = None
    end_local: datetime | None = None
    observed: ObservedConditions | None = None
    grounding_issues: list[ValidationIssue] = field(default_factory=list)
    constraint_violations: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not self.grounding_issues

    @property
    def eligible(self) -> bool:
        return self.grounded and not self.constraint_violations


@dataclass
class DraftValidation:
    windows: list[WindowValidation]
    plan_errors: list[ValidationIssue]

    @property
    def grounding_errors(self) -> list[ValidationIssue]:
        issues = list(self.plan_errors)
        for window in self.windows:
            issues.extend(window.grounding_issues)
        return issues

    @property
    def eligible_windows(self) -> list[WindowValidation]:
        return [window for window in self.windows if window.eligible]

    @property
    def needs_repair(self) -> bool:
        return bool(self.grounding_errors) or any(w.constraint_violations for w in self.windows)

    @property
    def passed(self) -> bool:
        """Plan-level checks pass and at least one window is fully verified and eligible.

        Windows are verified independently: a window with any grounding issue or
        constraint violation is never recommended (it is reported as rejected),
        but it does not discard other windows that passed every check.
        """
        return not self.plan_errors and bool(self.eligible_windows)


class _Validator:
    def __init__(
        self,
        ledger: EvidenceLedger,
        assessments: dict[str, BlockAssessment],
        constraints: ClientConstraints,
        now: datetime,
    ) -> None:
        self.ledger = ledger
        self.assessments = assessments
        self.constraints = constraints
        self.now = now
        self.tz = tzinfo_for(
            ledger.location.timezone_offset_seconds
            if ledger.location and ledger.location.timezone_offset_seconds is not None
            else _offset_from_forecast(ledger)
        )
        self.forecast = {obs.observation_id: obs for obs in ledger.forecast}
        self.known_ids = known_observation_ids(ledger)
        self.temperatures = self._value_set(
            ["temperature_f", "feels_like_f"],
            constraints_values=[
                constraints.min_temperature_f,
                constraints.max_temperature_f,
            ],
        )
        self.winds = self._value_set(["wind_speed_mph"], constraints_values=[constraints.max_wind_speed_mph])
        self.precips = self._value_set(
            ["precipitation_probability_pct"], constraints_values=[constraints.max_precipitation_probability_pct]
        )

    def _value_set(self, attrs: list[str], constraints_values: list[float | None]) -> list[float]:
        values = [v for v in constraints_values if v is not None]
        for obs in self.ledger.forecast:
            values.extend(getattr(obs, attr) for attr in attrs if getattr(obs, attr) is not None)
        current = self.ledger.current
        if current is not None:
            mapping = {
                "temperature_f": current.temperature_f,
                "feels_like_f": current.feels_like_f,
                "wind_speed_mph": current.wind_speed_mph,
            }
            values.extend(mapping[attr] for attr in attrs if mapping.get(attr) is not None)
        return values

    # -- helpers -------------------------------------------------------------
    def _issue(self, code: str, message: str, window_id: str | None = None) -> ValidationIssue:
        return ValidationIssue(code=code, message=message, severity="error", window_id=window_id)

    def _parse_local(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.strip())
        except (ValueError, AttributeError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=self.tz)
        if parsed.utcoffset() != self.tz.utcoffset(None):
            return None
        return parsed

    def _text_issues(self, text: str, label: str, window_id: str | None) -> list[ValidationIssue]:
        issues = []
        if len(text) > MAX_TEXT_LENGTH:
            issues.append(self._issue("text_too_long", f"{label} exceeds {MAX_TEXT_LENGTH} characters.", window_id))
        for pattern in BUSINESS_CLAIM_PATTERNS:
            if pattern.search(text):
                issues.append(
                    self._issue(
                        "unsupported_business_claim",
                        f"{label} states a business outcome (KPI, ROI, or guaranteed effect) that the "
                        "evidence cannot support; phrase it as a hypothesis without numbers.",
                        window_id,
                    )
                )
                break
        issues.extend(self._numeric_text_issues(text, label, window_id))
        return issues

    def _numeric_text_issues(self, text: str, label: str, window_id: str | None) -> list[ValidationIssue]:
        issues = []
        checks = (
            (TEMPERATURE_RE, self.temperatures, TEXT_TEMPERATURE_TOLERANCE_F, "°F"),
            (WIND_RE, self.winds, WIND_TOLERANCE_MPH, "mph"),
            (PRECIP_RE, self.precips, PERCENT_TOLERANCE, "% precipitation probability"),
        )
        for pattern, allowed, tolerance, unit in checks:
            for match in pattern.finditer(text):
                raw = next(group for group in match.groups() if group is not None)
                value = float(raw)
                if not any(abs(value - candidate) <= tolerance for candidate in allowed):
                    issues.append(
                        self._issue(
                            "unsupported_weather_value",
                            f"{label} mentions {raw} {unit}, which does not match any retrieved observation.",
                            window_id,
                        )
                    )
        return issues

    def _risk_issues(self, risks: list[RiskDraft], window_id: str | None) -> list[ValidationIssue]:
        issues = []
        for risk in risks:
            unknown = [oid for oid in risk.observation_ids if oid not in self.known_ids]
            if unknown:
                issues.append(
                    self._issue(
                        "risk_unknown_observation",
                        f"Risk cites evidence IDs that were not retrieved for this request: {', '.join(unknown[:3])}.",
                        window_id,
                    )
                )
            issues.extend(self._text_issues(f"{risk.risk} {risk.mitigation}", "A risk note", window_id))
        return issues

    # -- window checks ---------------------------------------------------------
    def window(self, window_id: str, draft: WindowDraft) -> WindowValidation:
        result = WindowValidation(window_id=window_id, draft=draft)
        issues = result.grounding_issues
        if not draft.title.strip():
            issues.append(self._issue("missing_title", "Window title is empty.", window_id))
        if not draft.weather_reasoning.strip():
            issues.append(self._issue("missing_reasoning", "Weather reasoning is empty.", window_id))

        ids = draft.observation_ids
        if not ids:
            issues.append(self._issue("missing_evidence", "The window cites no forecast observations.", window_id))
        if len(set(ids)) != len(ids):
            issues.append(self._issue("duplicate_observation", "The window cites an observation twice.", window_id))
        for oid in dict.fromkeys(ids):
            if oid in self.forecast:
                result.blocks.append(self.forecast[oid])
            elif oid in self.known_ids:
                issues.append(
                    self._issue(
                        "observation_not_forecast",
                        f"{oid} is not a forecast block; windows must cite forecast (fc-) observations.",
                        window_id,
                    )
                )
            else:
                issues.append(
                    self._issue(
                        "unknown_observation",
                        f"{oid} was not retrieved for this request; it cannot support a recommendation.",
                        window_id,
                    )
                )
        if len(result.blocks) > MAX_BLOCKS_PER_WINDOW:
            issues.append(
                self._issue("window_too_long", f"A window may cite at most {MAX_BLOCKS_PER_WINDOW} blocks.", window_id)
            )
        result.blocks.sort(key=lambda obs: obs.datetime_utc)
        for previous, current in zip(result.blocks, result.blocks[1:], strict=False):
            if current.datetime_utc - previous.datetime_utc != BLOCK:
                issues.append(
                    self._issue("non_contiguous_observations", "Cited forecast blocks are not consecutive.", window_id)
                )
                break

        start, end = self._parse_local(draft.start_local), self._parse_local(draft.end_local)
        if start is None or end is None:
            issues.append(
                self._issue(
                    "invalid_time",
                    "start_local/end_local must be local times formatted YYYY-MM-DDTHH:MM "
                    f"in the location's offset ({self.tz.tzname(None)}).",
                    window_id,
                )
            )
        elif start >= end:
            issues.append(self._issue("invalid_time", "The window ends before it starts.", window_id))
        else:
            result.start_local, result.end_local = start, end
            if end - start < MIN_WINDOW:
                issues.append(self._issue("window_too_short", "Windows must last at least one hour.", window_id))
            if start < self.now:
                issues.append(
                    self._issue("window_in_past", "The window starts before the time of this request.", window_id)
                )

        if result.blocks and result.start_local and result.end_local:
            span = Interval(
                result.blocks[0].datetime_utc.astimezone(self.tz),
                result.blocks[-1].datetime_utc.astimezone(self.tz) + BLOCK,
            )
            target = Interval(result.start_local, result.end_local)
            if not (span.start <= target.start and target.end <= span.end):
                issues.append(
                    self._issue(
                        "window_outside_evidence",
                        f"The window {draft.start_local}-{draft.end_local} is not covered by the cited forecast "
                        f"blocks ({span.start:%Y-%m-%dT%H:%M}-{span.end:%Y-%m-%dT%H:%M} local).",
                        window_id,
                    )
                )
            for obs in result.blocks:
                block = Interval(obs.datetime_utc.astimezone(self.tz), obs.datetime_utc.astimezone(self.tz) + BLOCK)
                if not block.overlaps(target):
                    issues.append(
                        self._issue(
                            "observation_outside_window",
                            f"{obs.observation_id} does not overlap the recommended window.",
                            window_id,
                        )
                    )

        if result.blocks:
            result.observed = observed_conditions(result.blocks)
            issues.extend(self._claim_issues(draft, result.blocks, window_id))
            result.constraint_violations = self._constraint_violations(result)

        issues.extend(self._text_issues(draft.weather_reasoning, "Weather reasoning", window_id))
        issues.extend(self._text_issues(draft.claimed_conditions.conditions_summary, "Conditions summary", window_id))
        hypothesis = draft.marketing_hypothesis.strip()
        if not hypothesis:
            issues.append(self._issue("missing_hypothesis", "A marketing hypothesis is required.", window_id))
        else:
            issues.extend(self._text_issues(hypothesis, "Marketing hypothesis", window_id))
            if not HEDGE_RE.search(hypothesis):
                issues.append(
                    self._issue(
                        "hypothesis_not_hedged",
                        "The marketing hypothesis must be phrased as an untested hypothesis "
                        "(for example, 'We hypothesize ...').",
                        window_id,
                    )
                )
        issues.extend(ad_copy_issues(draft.ad_copy, window_id))
        issues.extend(self._risk_issues(draft.risks, window_id))
        return result

    def _claim_issues(
        self, draft: WindowDraft, blocks: list[ForecastObservation], window_id: str
    ) -> list[ValidationIssue]:
        """Every value the model restated for a cited block must match that block's evidence."""
        issues: list[ValidationIssue] = []
        claims: dict[str, list] = {}
        for value in draft.claimed_conditions.cited_values:
            claims.setdefault(value.observation_id, []).append(value)
        cited = {obs.observation_id for obs in blocks}
        for oid in claims.keys() - cited:
            issues.append(
                self._issue(
                    "claim_for_uncited_observation",
                    f"Values are restated for {oid}, which the window does not cite.",
                    window_id,
                )
            )

        def compare(
            oid: str, name: str, claim: float | None, actual: float | None, tolerance: float, unit: str
        ) -> None:
            if claim is None and actual is None:
                return
            if claim is None:
                issues.append(
                    self._issue("missing_claim", f"{oid}: restate its {name} from the evidence table.", window_id)
                )
            elif actual is None:
                issues.append(
                    self._issue(
                        "claim_unverifiable", f"{oid}: the evidence has no {name}, so none may be stated.", window_id
                    )
                )
            elif abs(claim - actual) > tolerance:
                issues.append(
                    self._issue(
                        f"{name.split()[0].lower()}_mismatch",
                        f"{oid}: stated {name} {claim:g} {unit} does not match the evidence ({actual:g} {unit}).",
                        window_id,
                    )
                )

        for obs in blocks:
            values = claims.get(obs.observation_id, [])
            if len(values) != 1:
                issues.append(
                    self._issue(
                        "missing_claim" if not values else "duplicate_claim",
                        f"{obs.observation_id}: restate its values exactly once in cited_values.",
                        window_id,
                    )
                )
                continue
            claim = values[0]
            compare(
                obs.observation_id, "temperature", claim.temperature_f, obs.temperature_f, TEMPERATURE_TOLERANCE_F, "°F"
            )
            pop, actual_pop = claim.precipitation_probability_pct, obs.precipitation_probability_pct
            if pop is not None and actual_pop is not None and actual_pop > 1.5 and abs(pop - actual_pop / 100) <= 0.011:
                issues.append(
                    self._issue(
                        "precipitation_unit_error",
                        f"{obs.observation_id}: precipitation probability must be in percent (0-100), not a fraction.",
                        window_id,
                    )
                )
            else:
                compare(obs.observation_id, "precipitation probability", pop, actual_pop, PERCENT_TOLERANCE, "%")
            if claim.wind_speed_mph is not None:
                compare(
                    obs.observation_id,
                    "wind speed",
                    claim.wind_speed_mph,
                    obs.wind_speed_mph,
                    WIND_TOLERANCE_MPH,
                    "mph",
                )
            if claim.aqi is not None:
                if obs.aqi is None:
                    issues.append(
                        self._issue(
                            "aqi_without_evidence",
                            f"{obs.observation_id}: no AQI forecast covers this block.",
                            window_id,
                        )
                    )
                elif claim.aqi != obs.aqi:
                    issues.append(
                        self._issue(
                            "aqi_mismatch",
                            f"{obs.observation_id}: stated AQI {claim.aqi} does not match the evidence (AQI {obs.aqi}).",
                            window_id,
                        )
                    )
        return issues

    def _constraint_violations(self, result: WindowValidation) -> list[str]:
        violations: list[str] = []
        target = Interval(result.start_local, result.end_local) if result.start_local and result.end_local else None
        activation: list[Interval] = []
        notes: list[str] = []
        for obs in result.blocks:
            assessment = self.assessments.get(obs.observation_id)
            if assessment is None:
                continue
            if target is None or assessment.interval.overlaps(target):
                violations.extend(f"{obs.observation_id}: {v}" for v in assessment.weather_violations)
            activation.extend(assessment.activation)
            notes.extend(assessment.time_notes)
        if target is not None and not _covers(activation, target):
            detail = "; ".join(dict.fromkeys(notes)) or "outside the client's allowed, non-excluded hours"
            violations.append(f"window time is not permitted for this client ({detail})")
        return list(dict.fromkeys(violations))

    # -- plan checks -------------------------------------------------------------
    def draft(self, draft: CampaignDraft) -> DraftValidation:
        plan_errors: list[ValidationIssue] = []
        if not draft.windows:
            plan_errors.append(self._issue("no_windows", "The draft recommends no campaign windows."))
        if len(draft.windows) > MAX_WINDOWS:
            plan_errors.append(self._issue("too_many_windows", f"Recommend at most {MAX_WINDOWS} windows."))
        windows = [self.window(f"w{index}", window) for index, window in enumerate(draft.windows[:MAX_WINDOWS], 1)]
        timed = [w for w in windows if w.start_local and w.end_local]
        for index, first in enumerate(timed):
            for second in timed[index + 1 :]:
                if Interval(first.start_local, first.end_local).overlaps(
                    Interval(second.start_local, second.end_local)
                ):
                    plan_errors.append(
                        self._issue(
                            "overlapping_windows",
                            f"Windows {first.window_id} and {second.window_id} overlap.",
                            second.window_id,
                        )
                    )
        if not draft.strategy_summary.strip():
            plan_errors.append(self._issue("missing_summary", "The strategy summary is empty."))
        plan_errors.extend(self._text_issues(draft.strategy_summary, "Strategy summary", None))
        plan_errors.extend(self._risk_issues(draft.overall_risks, None))
        return DraftValidation(windows=windows, plan_errors=plan_errors)


def ad_copy_issues(lines: list[str], window_id: str | None) -> list[ValidationIssue]:
    issues = []
    cleaned = [line.strip() for line in lines]
    if not 1 <= len(cleaned) <= 3:
        issues.append(
            ValidationIssue(
                code="ad_copy_count", message="Provide 1 to 3 ad copy lines.", severity="error", window_id=window_id
            )
        )
    for line in cleaned:
        if not line or len(line) > MAX_AD_COPY_LENGTH:
            issues.append(
                ValidationIssue(
                    code="ad_copy_length",
                    message=f"Each ad copy line must contain 1-{MAX_AD_COPY_LENGTH} characters.",
                    severity="error",
                    window_id=window_id,
                )
            )
        if any(pattern.search(line) for pattern in AD_COPY_CLAIM_PATTERNS):
            issues.append(
                ValidationIssue(
                    code="unsupported_business_claim",
                    message="Ad copy must not promise guaranteed or proven outcomes or cite ROI.",
                    severity="error",
                    window_id=window_id,
                )
            )
    return issues


def validate_draft(
    draft: CampaignDraft,
    ledger: EvidenceLedger,
    assessments: dict[str, BlockAssessment],
    constraints: ClientConstraints,
    now: datetime,
) -> DraftValidation:
    return _Validator(ledger, assessments, constraints, now).draft(draft)


def repair_feedback(validation: DraftValidation, assessments: dict[str, BlockAssessment]) -> str:
    """Explain validation failures so the model can produce a corrected draft."""
    lines = [
        "Your previous JSON failed deterministic validation. Return a complete, corrected JSON object.",
        "Cite only observation IDs from the evidence table and copy values exactly.",
        "",
        "Errors:",
    ]
    for issue in validation.grounding_errors:
        prefix = f"[{issue.window_id}] " if issue.window_id else ""
        lines.append(f"- {prefix}{issue.code}: {issue.message}")
    violated = [w for w in validation.windows if w.constraint_violations]
    if violated:
        lines += ["", "Hard-constraint violations (these windows will be rejected; replace them):"]
        for window in violated:
            for violation in window.constraint_violations:
                lines.append(f"- [{window.window_id}] {violation}")
    lines += ["", "Eligible forecast blocks and permitted local activation times:"]
    lines.extend(eligible_block_lines(assessments) or ["- none"])
    return "\n".join(lines)


def eligible_block_lines(assessments: dict[str, BlockAssessment], limit: int = 40) -> list[str]:
    lines = []
    for oid, assessment in assessments.items():
        if not assessment.eligible:
            continue
        spans = ", ".join(f"{part.start:%Y-%m-%dT%H:%M}-{part.end:%H:%M}" for part in assessment.activation)
        lines.append(f"- {oid}: {spans}")
        if len(lines) >= limit:
            break
    return lines
