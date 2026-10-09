"""Deterministic grounding and hard-constraint validation of model drafts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent.schemas import ClientConstraints
from agent.validation import assess_blocks
from evaluation.harness import FIXED_NOW
from tests.validation_helpers import codes, draft, find_block, ledger_for, local, validate, window


@pytest.fixture(scope="module")
def ledger():
    return ledger_for("baseline_mild")


def test_grounded_draft_passes(ledger):
    a, b = find_block(ledger, 4, 8), find_block(ledger, 4, 11)
    result = validate(ledger, draft(window(ledger, [a, b])))
    assert result.passed, [i.message for i in result.grounding_errors]
    assert result.eligible_windows[0].observed.temperature_max_f == max(a.temperature_f, b.temperature_f)


def test_invented_and_non_forecast_ids_are_rejected(ledger):
    block = find_block(ledger, 4, 8)
    invented = window(ledger, [block], observation_ids=["fc-20991231T0000Z"])
    current = window(ledger, [block], observation_ids=[ledger.current.observation_id])
    result = validate(ledger, draft(invented))
    assert "unknown_observation" in codes(result) and not result.passed
    assert "observation_not_forecast" in codes(validate(ledger, draft(current)))


def test_non_contiguous_blocks_are_rejected(ledger):
    a, c = find_block(ledger, 4, 8), find_block(ledger, 4, 14)
    result = validate(ledger, draft(window(ledger, [a, c], end=local(a) + timedelta(hours=3))))
    assert "non_contiguous_observations" in codes(result)


def test_window_must_fall_inside_cited_evidence_and_in_the_future(ledger):
    block = find_block(ledger, 4, 8)
    outside = window(ledger, [block], end=local(block) + timedelta(hours=5))
    assert "window_outside_evidence" in codes(validate(ledger, draft(outside)))
    later = FIXED_NOW + timedelta(days=1)  # request time after the block started
    assert "window_in_past" in codes(validate(ledger, draft(window(ledger, [block])), now=later))
    short = window(ledger, [block], end=local(block) + timedelta(minutes=30))
    assert "window_too_short" in codes(validate(ledger, draft(short)))


def test_times_in_the_wrong_timezone_are_rejected(ledger):
    block = find_block(ledger, 4, 8)
    utc_text = (block.datetime_utc).strftime("%Y-%m-%dT%H:%M+00:00")
    result = validate(ledger, draft(window(ledger, [block], start_local=utc_text)))
    assert "invalid_time" in codes(result)


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"temperature_max_f": 99.0}, "temperature_mismatch"),
        ({"temperature_min_f": None}, "missing_claim"),
        ({"wind_speed_max_mph": 40.0}, "wind_mismatch"),
        ({"aqi_max": 5}, "aqi_mismatch"),
    ],
)
def test_claimed_values_must_match_cited_evidence(ledger, override, code):
    block = find_block(ledger, 4, 8)
    assert code in codes(validate(ledger, draft(window(ledger, [block], **override))))


def test_precipitation_fraction_is_flagged_as_a_unit_error(ledger):
    block = next(
        o for o in ledger.forecast if (o.precipitation_probability_pct or 0) >= 20 and o.datetime_utc > FIXED_NOW
    )
    fraction = block.precipitation_probability_pct / 100
    result = validate(ledger, draft(window(ledger, [block], precipitation_probability_max_pct=fraction)))
    assert "precipitation_unit_error" in codes(result)


def test_aqi_claim_without_aqi_evidence_is_rejected():
    no_air = ledger_for("baseline_mild", air=False)
    block = find_block(no_air, 4, 8)
    assert "aqi_without_evidence" in codes(validate(no_air, draft(window(no_air, [block], aqi_max=2))))


def test_free_text_weather_numbers_must_exist_in_evidence(ledger):
    block = find_block(ledger, 4, 8)
    invented = window(ledger, [block], weather_reasoning="Expect a balmy 131°F with 9 mph breezes.")
    result = validate(ledger, draft(invented))
    assert "unsupported_weather_value" in codes(result)
    real = window(ledger, [block], weather_reasoning=f"Around {block.temperature_f:.0f}°F in the morning.")
    assert "unsupported_weather_value" not in codes(validate(ledger, draft(real)))


@pytest.mark.parametrize(
    "text",
    [
        "This will increase sales by 25%.",
        "Expect a 30% lift in visits.",
        "Guaranteed to fill every class.",
        "Strong ROI from this weather window.",
    ],
)
def test_business_outcome_claims_are_rejected(ledger, text):
    block = find_block(ledger, 4, 8)
    result = validate(ledger, draft(window(ledger, [block], marketing_hypothesis=f"We hypothesize this. {text}")))
    assert "unsupported_business_claim" in codes(result)


def test_hypotheses_must_be_hedged(ledger):
    block = find_block(ledger, 4, 8)
    result = validate(ledger, draft(window(ledger, [block], marketing_hypothesis="Customers buy more coffee.")))
    assert "hypothesis_not_hedged" in codes(result)


def test_plan_level_checks(ledger):
    a, b = find_block(ledger, 4, 8), find_block(ledger, 5, 8)
    overlapping = draft(window(ledger, [a]), window(ledger, [a]))
    assert "overlapping_windows" in codes(validate(ledger, overlapping))
    too_many = draft(*(window(ledger, [find_block(ledger, day, 8)]) for day in (4, 5, 6, 7)))
    assert "too_many_windows" in codes(validate(ledger, too_many))
    assert "no_windows" in codes(validate(ledger, draft()))
    bad_risk = draft(
        window(ledger, [b]), overall_risks=[{"risk": "Rain", "mitigation": "Tents", "observation_ids": ["fc-x"]}]
    )
    assert "risk_unknown_observation" in codes(validate(ledger, bad_risk))


# -- hard constraints ----------------------------------------------------------------
COFFEE = ClientConstraints(
    allowed_hours=[{"start_hour": 6, "end_hour": 11}],
    exclusion_windows=[{"weekdays": ["Sun"], "start_hour": 6, "end_hour": 8, "reason": "Opens at 8 on Sundays"}],
)
FITNESS = ClientConstraints(
    allowed_hours=[{"start_hour": 6, "end_hour": 9}, {"start_hour": 17, "end_hour": 20}],
    min_temperature_f=45,
    max_temperature_f=88,
    max_precipitation_probability_pct=30,
    max_wind_speed_mph=15,
    max_aqi=2,
)


def test_allowed_hours_are_enforced_even_if_the_model_insists(ledger):
    block = find_block(ledger, 4, 11)  # 11:00-14:00 local
    result = validate(ledger, draft(window(ledger, [block])), COFFEE)
    assert result.grounding_errors == []
    assert result.windows[0].constraint_violations
    assert not result.passed  # grounded, but no eligible window remains


def test_activation_can_trim_a_block_to_allowed_hours(ledger):
    block = find_block(ledger, 4, 5)  # 05:00-08:00 local; coffee allows 06:00-11:00
    trimmed = window(ledger, [block], start=local(block) + timedelta(hours=1))
    result = validate(ledger, draft(trimmed), COFFEE)
    assert result.passed, [i.message for i in result.grounding_errors] + result.windows[0].constraint_violations


def test_exclusion_windows_apply_by_local_weekday(ledger):
    sunday = find_block(ledger, 7, 5)  # 2030-04-07 is a Sunday
    early = window(ledger, [sunday], start=local(sunday) + timedelta(hours=1))
    assert any("excluded" in v for v in validate(ledger, draft(early), COFFEE).windows[0].constraint_violations)
    sunday_late = find_block(ledger, 7, 8)
    assert validate(ledger, draft(window(ledger, [sunday_late])), COFFEE).passed


def test_weather_limits_and_unverifiable_fields_make_windows_ineligible():
    rainy = ledger_for("baseline_mild")  # day 1 (2030-04-05) has afternoon showers at 60%
    evening = find_block(rainy, 5, 14)
    result = validate(
        rainy, draft(window(rainy, [evening], start=local(evening) + timedelta(hours=3) - timedelta(hours=1))), FITNESS
    )
    assert any("exceeds" in v or "not permitted" in v for v in result.windows[0].constraint_violations)

    missing = ledger_for("missing_fields")
    gap = next(o for o in missing.forecast if o.missing_fields and o.datetime_local.hour == 8)
    gap_window = window(missing, [gap], end=local(gap) + timedelta(hours=1))
    violations = validate(missing, draft(gap_window), FITNESS).windows[0].constraint_violations
    assert any("cannot be verified" in v for v in violations)

    poor = ledger_for("poor_air")
    block = find_block(poor, 4, 17)
    assert any(
        "AQI 4 exceeds" in v
        for v in validate(poor, draft(window(poor, [block])), FITNESS).windows[0].constraint_violations
    )


def test_overnight_allowed_hours_merge_across_midnight(ledger):
    overnight = ClientConstraints(allowed_hours=[{"start_hour": 22, "end_hour": 24}, {"start_hour": 0, "end_hour": 2}])
    block = find_block(ledger, 4, 23)  # 23:00-02:00 local
    activation = assess_blocks(ledger, overnight, FIXED_NOW)[block.observation_id].activation
    assert len(activation) == 1
    assert activation[0].start == local(block) and activation[0].end == local(block) + timedelta(hours=3)


def test_elapsed_blocks_are_never_eligible(ledger):
    later = FIXED_NOW + timedelta(days=2)
    assessments = assess_blocks(ledger, ClientConstraints(), later)
    first = ledger.forecast[0]
    assert not assessments[first.observation_id].eligible
    assert "block has already ended" in assessments[first.observation_id].time_notes
