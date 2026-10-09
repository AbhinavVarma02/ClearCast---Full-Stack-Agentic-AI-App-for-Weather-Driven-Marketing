"""Offline evaluation scenarios (synthetic weather + scripted model behaviour).

These scenarios test the *system's* behaviour under controlled inputs:
grounding, hard constraints, repair, provider failures, sessions, and review.
They do not measure recommendation usefulness, campaign lift, or general LLM
accuracy; the model's outputs are scripted fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COFFEE_BRIEF = {
    "location": "Baltimore, MD",
    "business_type": "Coffee shop",
    "campaign_goal": "Increase morning visits",
    "tone": "Friendly",
}
FITNESS_BRIEF = {
    "location": "Austin, TX",
    "business_type": "Outdoor fitness studio",
    "campaign_goal": "Promote outdoor class signups",
    "tone": "Urgent",
}
GENERAL_BRIEF = {
    "location": "Chicago, IL",
    "business_type": "Ice cream parlor",
    "campaign_goal": "Boost weekend foot traffic",
    "tone": "Playful",
}


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    description: str
    weather: str = "baseline_mild"
    client_id: str = "general"
    brief: dict = field(default_factory=lambda: dict(GENERAL_BRIEF))
    constraints: dict | None = None
    agent_policy: str = "standard"
    drafts: tuple[str, ...] = ("grounded",)
    model_failure: str | None = None
    expect_status: str | None = None  # pending_review | validation_failed
    expect_error: str | None = None  # error code when no plan is returned
    expect_codes: tuple[str, ...] = ()  # validation error codes that must be present
    expect_provider_category: str | None = None
    expect_repairs: int | None = None
    expect_drafting: bool | None = None
    expect_rejected_codes: tuple[str, ...] = ()  # codes reported for windows rejected while the plan passes
    designed_invalid: bool = False  # model output stays invalid on every attempt
    transient_invalid: bool = False  # invalid first, valid after one repair


def coffee(name, description, **kwargs) -> Scenario:
    return Scenario(
        name,
        kwargs.pop("category", "coffee_shop"),
        description,
        client_id="demo_coffee_shop",
        brief=dict(COFFEE_BRIEF),
        **kwargs,
    )


def fitness(name, description, **kwargs) -> Scenario:
    return Scenario(
        name,
        kwargs.pop("category", "outdoor_fitness"),
        description,
        client_id="demo_outdoor_fitness",
        brief=dict(FITNESS_BRIEF),
        **kwargs,
    )


PLAN_SCENARIOS: list[Scenario] = [
    # Normal client campaigns
    coffee(
        "coffee_rainy_mornings",
        "Rainy mornings suit warm-drink promotions.",
        weather="rainy_mornings",
        expect_status="pending_review",
        expect_repairs=0,
    ),
    coffee("coffee_clear_week", "Clear, warm week.", weather="clear_warm", expect_status="pending_review"),
    coffee(
        "coffee_cold_snap",
        "Extreme cold; coffee shop has no temperature limits.",
        weather="cold_snap",
        category="extreme_weather",
        expect_status="pending_review",
    ),
    coffee(
        "coffee_poor_air_no_aqi_rule",
        "Poor air quality is not a hard rule for this client.",
        weather="poor_air",
        category="air_quality",
        expect_status="pending_review",
    ),
    coffee(
        "coffee_out_of_hours_choice_repaired",
        "Model first proposes a block outside 06-11.",
        drafts=("ineligible_choice", "grounded"),
        category="constraint_repair",
        expect_status="pending_review",
        expect_repairs=1,
        transient_invalid=True,
    ),
    fitness("fitness_mild_week", "Mild week with one showery day.", expect_status="pending_review"),
    fitness(
        "fitness_rainy_mornings",
        "Rainy mornings leave only evening classes eligible.",
        weather="rainy_mornings",
        category="rain",
        expect_status="pending_review",
    ),
    fitness(
        "fitness_ineligible_choice_repaired",
        "Model first proposes a rule-breaking block.",
        drafts=("ineligible_choice", "grounded"),
        category="constraint_repair",
        expect_status="pending_review",
        expect_repairs=1,
        transient_invalid=True,
    ),
    fitness(
        "fitness_heat_wave",
        "Extreme heat leaves only early mornings under 88 F.",
        weather="heat_wave",
        category="extreme_weather",
        expect_status="pending_review",
    ),
    fitness(
        "fitness_cold_snap",
        "Every block is below the 45 F minimum.",
        weather="cold_snap",
        category="extreme_weather",
        expect_status="validation_failed",
        expect_codes=("no_eligible_blocks",),
        expect_drafting=False,
    ),
    fitness(
        "fitness_strong_wind",
        "Every block exceeds the 15 mph wind limit.",
        weather="windy",
        category="wind",
        expect_status="validation_failed",
        expect_codes=("no_eligible_blocks",),
        expect_drafting=False,
    ),
    fitness(
        "fitness_poor_air_quality",
        "AQI 4 exceeds the AQI 2 limit everywhere.",
        weather="poor_air",
        category="air_quality",
        expect_status="validation_failed",
        expect_codes=("no_eligible_blocks",),
        expect_drafting=False,
    ),
    fitness(
        "fitness_missing_weather_fields",
        "Half the blocks lack wind and precipitation probability.",
        weather="missing_fields",
        category="missing_fields",
        expect_status="pending_review",
    ),
    Scenario(
        "general_storm_week",
        "general",
        "No client rules; storm week still yields grounded windows.",
        weather="storm_week",
        expect_status="pending_review",
    ),
    fitness(
        "conflicting_constraints_unsatisfiable",
        "Zero-precipitation rule on a week with rain chances.",
        weather="rainy_mornings",
        constraints={"max_precipitation_probability_pct": 0},
        category="conflicting_constraints",
        expect_status="validation_failed",
        expect_codes=("no_eligible_blocks",),
    ),
    fitness(
        "conflicting_constraints_invalid_config",
        "Minimum temperature above maximum is rejected up front.",
        constraints={"min_temperature_f": 90, "max_temperature_f": 50},
        category="conflicting_constraints",
        expect_error="invalid_request",
    ),
    # Invalid model output
    Scenario(
        "invalid_window_times_persistent",
        "invalid_output",
        "Window extends beyond its cited blocks.",
        drafts=("outside_window",),
        expect_status="validation_failed",
        expect_codes=("window_outside_evidence",),
        expect_repairs=2,
        designed_invalid=True,
    ),
    Scenario(
        "elapsed_window_persistent",
        "invalid_output",
        "Window starts before the request time.",
        drafts=("past_window",),
        expect_status="validation_failed",
        expect_codes=("window_in_past",),
        designed_invalid=True,
    ),
    Scenario(
        "unsupported_evidence_repaired",
        "invalid_output",
        "Invented ID, fixed after feedback.",
        drafts=("invented_id", "grounded"),
        expect_status="pending_review",
        expect_repairs=1,
        transient_invalid=True,
    ),
    Scenario(
        "partial_invented_id_window_rejected",
        "invalid_output",
        "One window cites an invented ID on every attempt; verified windows survive, the bad one is rejected.",
        drafts=("first:invented_id",),
        expect_status="pending_review",
        expect_rejected_codes=("unknown_observation",),
        designed_invalid=True,
    ),
    Scenario(
        "partial_value_mismatch_window_rejected",
        "invalid_output",
        "One window restates a temperature 15 F off on every attempt.",
        drafts=("first:wrong_temperature",),
        expect_status="pending_review",
        expect_rejected_codes=("temperature_mismatch",),
        designed_invalid=True,
    ),
    Scenario(
        "unsupported_evidence_persistent",
        "invalid_output",
        "Invented IDs on every attempt.",
        drafts=("invented_id",),
        expect_status="validation_failed",
        expect_codes=("unknown_observation",),
        designed_invalid=True,
    ),
    Scenario(
        "value_mismatch_persistent",
        "invalid_output",
        "Claimed temperature 15 F above evidence.",
        drafts=("wrong_temperature",),
        expect_status="validation_failed",
        expect_codes=("temperature_mismatch",),
        designed_invalid=True,
    ),
    Scenario(
        "unit_confusion_repaired",
        "invalid_output",
        "Precipitation as a fraction, then fixed.",
        weather="rainy_mornings",
        drafts=("unit_confusion", "grounded"),
        expect_status="pending_review",
        expect_repairs=1,
        transient_invalid=True,
    ),
    Scenario(
        "invented_weather_number_persistent",
        "invalid_output",
        "Reasoning cites 131 F.",
        drafts=("invented_number",),
        expect_status="validation_failed",
        expect_codes=("unsupported_weather_value",),
        designed_invalid=True,
    ),
    Scenario(
        "kpi_claim_persistent",
        "invalid_output",
        "Hypothesis claims +25% sales and ROI.",
        drafts=("kpi_claim",),
        expect_status="validation_failed",
        expect_codes=("unsupported_business_claim",),
        designed_invalid=True,
    ),
    Scenario(
        "unhedged_hypothesis_repaired",
        "invalid_output",
        "Behaviour stated as fact, then hedged.",
        drafts=("unhedged", "grounded"),
        expect_status="pending_review",
        transient_invalid=True,
    ),
    Scenario(
        "malformed_output_repaired",
        "malformed_output",
        "Truncated JSON, then valid.",
        drafts=("malformed", "grounded"),
        expect_status="pending_review",
        expect_repairs=1,
        transient_invalid=True,
    ),
    Scenario(
        "malformed_output_persistent",
        "malformed_output",
        "Truncated JSON on every attempt.",
        drafts=("malformed",),
        expect_status="validation_failed",
        expect_codes=("schema_invalid",),
        designed_invalid=True,
    ),
    Scenario(
        "schema_missing_fields_persistent",
        "malformed_output",
        "Required fields missing every time.",
        drafts=("missing_fields",),
        expect_status="validation_failed",
        expect_codes=("schema_invalid",),
        designed_invalid=True,
    ),
    # Provider and model failures
    Scenario(
        "owm_unavailable",
        "provider_failure",
        "OpenWeatherMap returns HTTP 503.",
        weather="baseline_mild:owm_down",
        expect_status="validation_failed",
        expect_codes=("missing_location_evidence",),
        expect_provider_category="provider_unavailable",
        expect_drafting=False,
    ),
    Scenario(
        "owm_rate_limited",
        "provider_failure",
        "OpenWeatherMap returns HTTP 429.",
        weather="baseline_mild:owm_rate_limited",
        expect_status="validation_failed",
        expect_provider_category="provider_rate_limited",
        expect_drafting=False,
    ),
    Scenario(
        "owm_timeout",
        "provider_failure",
        "OpenWeatherMap requests time out.",
        weather="baseline_mild:owm_timeout",
        expect_status="validation_failed",
        expect_provider_category="provider_timeout",
        expect_drafting=False,
    ),
    Scenario(
        "owm_invalid_json",
        "provider_failure",
        "Forecast endpoint returns HTML.",
        weather="baseline_mild:owm_invalid_json",
        expect_status="validation_failed",
        expect_codes=("missing_forecast_evidence",),
        expect_provider_category="provider_bad_response",
    ),
    Scenario(
        "geocode_not_found",
        "provider_failure",
        "Unknown location.",
        weather="baseline_mild:geocode_empty",
        expect_status="validation_failed",
        expect_codes=("missing_location_evidence",),
        expect_provider_category="location_not_found",
    ),
    Scenario(
        "model_server_error",
        "model_failure",
        "OpenAI returns HTTP 500.",
        model_failure="model_500",
        expect_error="model_provider_error",
    ),
    Scenario(
        "model_timeout",
        "model_failure",
        "OpenAI requests time out.",
        model_failure="model_timeout",
        expect_error="model_timeout",
    ),
    Scenario(
        "model_skips_tools",
        "model_failure",
        "Model answers without retrieving weather.",
        agent_policy="skip_tools",
        expect_status="validation_failed",
        expect_codes=("missing_location_evidence", "missing_forecast_evidence"),
        expect_drafting=False,
    ),
    Scenario(
        "agent_step_limit",
        "model_failure",
        "Model loops on tool calls until the recursion limit.",
        agent_policy="loop_tools",
        expect_status="validation_failed",
        expect_drafting=False,
    ),
]

CUSTOM_SCENARIOS = {
    "session_isolation_interleaved": ("sessions", "Two sessions, four interleaved requests, no shared history."),
    "approval_then_revision_is_stale": ("review", "Approve, revise ad copy, old approval hash is rejected."),
    "rejection_is_distinct_from_approval": ("review", "Rejected plans cannot be approved and export as rejected."),
    "cross_session_review_blocked": ("sessions", "Another session cannot approve a plan it did not create."),
    "validation_failed_cannot_be_approved": ("review", "Failed plans are blocked from approval."),
    "cache_reuse_across_clients": ("cache", "Same city for two clients reuses the forecast with its original age."),
}
