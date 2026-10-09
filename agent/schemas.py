"""Validated data contracts for ClearCast campaign planning.

Three kinds of models live here:

* Request and client-configuration models (validated at the API boundary).
* ``CampaignDraft``: the structured output the language model proposes.
* ``CampaignPlan``: the validated result assembled by deterministic Python code,
  including the evidence ledger built from actual tool outputs.

The JSON Schemas in ``contracts/`` are generated from these models
(``python -m api.export_contracts``) and consumed by the Node.js gateway.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"
PLAN_HASH_PATTERN = r"^[0-9a-f]{64}$"
# Printable single-line text containing at least one non-space character.
SINGLE_LINE_PATTERN = r"^[^\x00-\x1f\x7f]*[^\x00-\x20\x7f][^\x00-\x1f\x7f]*$"
# Free text that may contain tabs and newlines but no other control characters.
MULTILINE_PATTERN = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$"

Tone = Literal["Friendly", "Urgent", "Playful", "Premium"]
ClientId = Literal["general", "demo_coffee_shop", "demo_outdoor_fitness"]
Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS: tuple[Weekday, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestModel(BaseModel):
    """Boundary models: unknown fields rejected and no type coercion (matches the gateway)."""

    model_config = ConfigDict(extra="forbid", strict=True)


# --------------------------------------------------------------------------
# Client configuration
# --------------------------------------------------------------------------
class HourRange(RequestModel):
    """A local-time hour range [start_hour, end_hour)."""

    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)

    @model_validator(mode="after")
    def _ordered(self) -> HourRange:
        if self.start_hour >= self.end_hour:
            raise ValueError("start_hour must be earlier than end_hour")
        return self


class ExclusionWindow(RequestModel):
    """Local weekday hours when the client must not run a campaign."""

    weekdays: list[Weekday] = Field(min_length=1, max_length=7)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    reason: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _ordered(self) -> ExclusionWindow:
        if self.start_hour >= self.end_hour:
            raise ValueError("start_hour must be earlier than end_hour")
        return self


class ClientConstraints(RequestModel):
    """Hard constraints verified by deterministic Python rules, never by the model."""

    allowed_hours: list[HourRange] = Field(default_factory=list, max_length=6)
    min_temperature_f: float | None = Field(default=None, ge=-60, le=140)
    max_temperature_f: float | None = Field(default=None, ge=-60, le=140)
    max_precipitation_probability_pct: float | None = Field(default=None, ge=0, le=100)
    max_wind_speed_mph: float | None = Field(default=None, ge=0, le=150)
    max_aqi: int | None = Field(default=None, ge=1, le=5)
    exclusion_windows: list[ExclusionWindow] = Field(default_factory=list, max_length=14)

    @model_validator(mode="after")
    def _consistent(self) -> ClientConstraints:
        if (
            self.min_temperature_f is not None
            and self.max_temperature_f is not None
            and self.min_temperature_f > self.max_temperature_f
        ):
            raise ValueError("min_temperature_f must not exceed max_temperature_f")
        return self

    def is_empty(self) -> bool:
        return self == ClientConstraints()


class ClientPreferences(StrictModel):
    """Soft preferences passed to the model as context; not enforced."""

    preferred_conditions: str = Field(default="", max_length=300)
    promotion_ideas: list[str] = Field(default_factory=list, max_length=6)
    notes: str = Field(default="", max_length=300)


class ClientProfile(StrictModel):
    client_id: ClientId
    display_name: str
    fictional: bool
    business_type: str
    business_objective: str
    default_campaign_goal: str
    default_tone: Tone
    requires_air_quality: bool
    constraints: ClientConstraints
    preferences: ClientPreferences


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class CampaignBrief(RequestModel):
    """The four original campaign inputs."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    location: str = Field(min_length=1, max_length=120, pattern=SINGLE_LINE_PATTERN)
    business_type: str = Field(min_length=1, max_length=80, pattern=SINGLE_LINE_PATTERN)
    campaign_goal: str = Field(default="", max_length=300, pattern=MULTILINE_PATTERN)
    tone: Tone = "Friendly"


class CampaignPlanRequest(RequestModel):
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    brief: CampaignBrief
    client_id: ClientId = "general"
    constraints: ClientConstraints | None = Field(
        default=None,
        description="Replaces the client profile's default constraints when provided.",
    )


class ReviewRequest(RequestModel):
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    decision: Literal["approve", "reject"]
    plan_hash: str = Field(pattern=PLAN_HASH_PATTERN)
    note: str = Field(default="", max_length=500, pattern=MULTILINE_PATTERN)


class RevisionRequest(RequestModel):
    """Reviewer edits to ad copy; any edit invalidates an earlier approval."""

    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    base_plan_hash: str = Field(pattern=PLAN_HASH_PATTERN)
    ad_copy: dict[str, list[str]] = Field(
        min_length=1,
        max_length=3,
        description="Window id -> replacement ad copy lines (1-3 lines per window).",
    )
    note: str = Field(default="", max_length=500, pattern=MULTILINE_PATTERN)


# --------------------------------------------------------------------------
# Model-proposed draft (structured output). No defaults: OpenAI strict mode
# requires every property, and None means "no claim made".
# --------------------------------------------------------------------------
class ClaimedConditions(StrictModel):
    temperature_min_f: float | None = Field(
        description="Lowest temperature (°F) across the cited forecast blocks, copied from evidence."
    )
    temperature_max_f: float | None = Field(
        description="Highest temperature (°F) across the cited forecast blocks, copied from evidence."
    )
    precipitation_probability_max_pct: float | None = Field(
        description="Highest precipitation probability across cited blocks, in PERCENT (0-100)."
    )
    wind_speed_max_mph: float | None = Field(description="Highest wind speed (mph) across the cited blocks, or null.")
    aqi_max: int | None = Field(description="Highest AQI (1-5) across the cited blocks from the AQI forecast, or null.")
    conditions_summary: str = Field(description="Short description of the cited conditions.")


class RiskDraft(StrictModel):
    risk: str
    mitigation: str
    observation_ids: list[str] = Field(description="Evidence IDs supporting this risk (may be empty).")


class WindowDraft(StrictModel):
    title: str = Field(description="Short name for the campaign window.")
    observation_ids: list[str] = Field(
        description="IDs of consecutive forecast blocks (fc-...) that cover this window."
    )
    start_local: str = Field(description="Local start time, format YYYY-MM-DDTHH:MM.")
    end_local: str = Field(description="Local end time, format YYYY-MM-DDTHH:MM.")
    claimed_conditions: ClaimedConditions
    weather_reasoning: str = Field(description="Why the cited weather suits this business now.")
    marketing_hypothesis: str = Field(
        description="A testable hypothesis about customer behaviour, starting 'We hypothesize'."
    )
    ad_copy: list[str] = Field(description="1-3 ready-to-use ad copy lines in the requested tone.")
    risks: list[RiskDraft]


class CampaignDraft(StrictModel):
    strategy_summary: str = Field(description="Two or three sentences summarising the plan.")
    windows: list[WindowDraft] = Field(description="1-3 recommended campaign windows.")
    overall_risks: list[RiskDraft]


# --------------------------------------------------------------------------
# Evidence ledger (built by Python from this request's tool outputs)
# --------------------------------------------------------------------------
class ResolvedLocation(StrictModel):
    name: str
    state: str
    country: str
    lat: float
    lon: float
    timezone_offset_seconds: int | None
    timezone_label: str
    timezone_source: str
    observation_id: str


class ForecastObservation(StrictModel):
    observation_id: str
    datetime_utc: datetime
    datetime_local: datetime | None
    temperature_f: float | None
    feels_like_f: float | None
    humidity_pct: float | None
    wind_speed_mph: float | None
    precipitation_probability_pct: float | None
    rain_volume_mm_3h: float
    description: str | None
    aqi: int | None = Field(description="Max AQI for this block from the AQI forecast, if available.")
    aqi_observation_id: str | None
    missing_fields: list[str]


class CurrentObservation(StrictModel):
    observation_id: str
    observed_at_utc: datetime
    temperature_f: float
    feels_like_f: float | None
    humidity_pct: float | None
    wind_speed_mph: float | None
    cloud_cover_pct: float | None
    description: str | None
    missing_fields: list[str]


class AirQualityObservation(StrictModel):
    observation_id: str
    observed_at_utc: datetime
    aqi: int
    pm2_5_ugm3: float | None
    pm10_ugm3: float | None
    o3_ugm3: float | None
    forecast_available: bool
    forecast_block_count: int


class EvidenceSource(StrictModel):
    tool: str
    status: Literal["ok", "error"]
    source: str | None
    fetched_at_utc: datetime | None
    cache_hit: bool
    cache_age_seconds: int
    error_category: str | None


class EvidenceLedger(StrictModel):
    location: ResolvedLocation | None
    forecast: list[ForecastObservation]
    current: CurrentObservation | None
    air_quality: AirQualityObservation | None
    sources: list[EvidenceSource]
    units: dict[str, str]
    issues: list[str]


class DailyForecastSummary(StrictModel):
    date_local: str
    low_f: float | None
    high_f: float | None
    max_precipitation_probability_pct: float | None
    max_wind_speed_mph: float | None
    conditions: str
    block_count: int


# --------------------------------------------------------------------------
# Validated plan
# --------------------------------------------------------------------------
class ReviewStatus(StrEnum):
    DRAFT = "draft"
    VALIDATION_FAILED = "validation_failed"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


REVIEW_STATUS_LABELS = {
    ReviewStatus.DRAFT: "Draft",
    ReviewStatus.VALIDATION_FAILED: "Validation Failed",
    ReviewStatus.PENDING_REVIEW: "Pending Review",
    ReviewStatus.APPROVED: "Approved",
    ReviewStatus.REJECTED: "Rejected",
}


class ObservedConditions(StrictModel):
    """Deterministically computed from the cited evidence."""

    temperature_min_f: float | None
    temperature_max_f: float | None
    feels_like_min_f: float | None
    precipitation_probability_max_pct: float | None
    wind_speed_max_mph: float | None
    rain_volume_total_mm: float
    aqi_max: int | None
    conditions: list[str]


class ConstraintCheck(StrictModel):
    eligible: bool
    violations: list[str]


class GroundingCheck(StrictModel):
    verified: bool
    issues: list[str]


class Risk(StrictModel):
    risk: str
    mitigation: str
    observation_ids: list[str]


class CampaignWindow(StrictModel):
    window_id: str
    title: str
    daypart: str
    start_local: datetime | None
    end_local: datetime | None
    start_utc: datetime | None
    end_utc: datetime | None
    observation_ids: list[str]
    evidence: list[ForecastObservation]
    observed_conditions: ObservedConditions | None
    claimed_conditions: ClaimedConditions
    weather_reasoning: str
    marketing_hypothesis: str
    ad_copy: list[str]
    risks: list[Risk]
    constraint_check: ConstraintCheck
    grounding: GroundingCheck


class ClientSnapshot(StrictModel):
    client_id: ClientId
    display_name: str
    fictional: bool
    business_objective: str
    constraints: ClientConstraints
    constraints_source: Literal["profile_default", "request_override"]
    preferences: ClientPreferences


class ValidationIssue(StrictModel):
    code: str
    message: str
    severity: Literal["error", "warning"]
    window_id: str | None = None


class ValidationReport(StrictModel):
    status: Literal["passed", "failed"]
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    repair_attempts: int
    drafting_attempted: bool
    checked_at: datetime


class ReviewEvent(StrictModel):
    at: datetime
    action: str
    from_status: ReviewStatus | None
    to_status: ReviewStatus
    revision: int
    plan_hash: str | None
    note: str | None


class ReviewState(StrictModel):
    status: ReviewStatus
    decided_at: datetime | None
    note: str | None
    approved_plan_hash: str | None
    history: list[ReviewEvent]


class TokenUsage(StrictModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    calls_with_usage: int
    calls_without_usage: int


class Diagnostics(StrictModel):
    request_id: str
    session_ref: str
    client_id: str
    model: str
    duration_ms: int
    graph_duration_ms: int
    drafting_duration_ms: int
    llm_calls: int
    tool_calls: dict[str, int]
    tool_errors: int
    provider_error_categories: list[str]
    cache_hits: int
    repair_attempts: int
    recursion_limit: int
    step_limit_reached: bool
    validation_status: str
    review_status: str
    token_usage: TokenUsage | None
    estimated_cost_usd: float | None
    cost_note: str


class CampaignPlan(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str
    session_ref: str
    revision: int
    plan_hash: str
    generated_at: datetime
    updated_at: datetime
    status: ReviewStatus
    brief: CampaignBrief
    campaign_goal: str
    client: ClientSnapshot
    resolved_location: ResolvedLocation | None
    strategy_summary: str | None
    windows: list[CampaignWindow]
    rejected_windows: list[CampaignWindow]
    risks: list[Risk]
    forecast_summary: list[DailyForecastSummary]
    evidence: EvidenceLedger
    analyst_notes: str | None
    validation: ValidationReport
    review: ReviewState
    diagnostics: Diagnostics
    disclaimers: list[str]


class CampaignPlanResponse(StrictModel):
    plan: CampaignPlan
    report_markdown: str


class ErrorDetail(StrictModel):
    path: str
    message: str


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str | None
    retryable: bool
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(StrictModel):
    error: ErrorBody
