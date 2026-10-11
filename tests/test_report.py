"""Markdown report: every rejected window says why it was not recommended."""

from __future__ import annotations

import json

from agent.report import rejection_reason, render_report
from agent.schemas import CampaignPlan, ConstraintCheck, GroundingCheck
from tests.helpers import PROJECT_ROOT

SAMPLE = json.loads((PROJECT_ROOT / "contracts" / "fixtures" / "sample_plan_response.json").read_text("utf-8"))
WIND = "wind 18.0 mph exceeds the 15 mph limit"
UNKNOWN_ID = "Observation fc-20991231T0000Z is not in this request's forecast evidence."


def plan_with_rejected_second_window(**checks) -> CampaignPlan:
    plan = CampaignPlan.model_validate(SAMPLE["plan"])
    kept, rejected = plan.windows
    return plan.model_copy(update={"windows": [kept], "rejected_windows": [rejected.model_copy(update=checks)]})


def test_constraint_violations_are_listed_for_a_rejected_window():
    report = render_report(
        plan_with_rejected_second_window(constraint_check=ConstraintCheck(eligible=False, violations=[WIND]))
    )
    line = next(text for text in report.splitlines() if text.startswith("- w2 · "))
    assert line.endswith(f": Hard constraints violated: {WIND}.")
    assert "Grounding failed" not in report


def test_grounding_and_constraint_failures_are_reported_together():
    plan = plan_with_rejected_second_window(
        grounding=GroundingCheck(verified=False, issues=[UNKNOWN_ID]),
        constraint_check=ConstraintCheck(eligible=False, violations=[WIND]),
    )
    assert rejection_reason(plan.rejected_windows[0]) == (
        f"Grounding failed: {UNKNOWN_ID} Hard constraints violated: {WIND}."
    )


def test_unverified_window_without_issue_text_still_gets_a_reason():
    report = render_report(plan_with_rejected_second_window(grounding=GroundingCheck(verified=False, issues=[])))
    assert "- w2 · " in report
    assert "Grounding failed: the cited evidence could not be verified." in report
