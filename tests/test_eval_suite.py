"""Offline evaluation suite as a regression gate (deterministic fixtures, no providers)."""

from __future__ import annotations

from evaluation.run_eval import run_all
from evaluation.scenarios import CUSTOM_SCENARIOS, PLAN_SCENARIOS


async def test_evaluation_scenarios_all_pass_with_measured_metrics():
    assert len(PLAN_SCENARIOS) + len(CUSTOM_SCENARIOS) >= 20
    report = await run_all()
    failing = {s["name"]: s["failures"] for s in report["scenarios"] if not s["passed"]}
    assert failing == {}
    metrics = report["metrics"]
    for name in (
        "scenario_pass_rate",
        "schema_validity_rate",
        "forecast_grounding_consistency",
        "hard_constraint_compliance",
        "invalid_output_rejection_rate",
        "repair_success_rate",
        "session_isolation_correctness",
        "api_contract_correctness",
    ):
        assert metrics[name] == 1.0, name
    # The metrics must be computed over real work, not empty denominators.
    assert metrics["recommended_windows_checked"] >= 20 and metrics["constrained_windows_checked"] >= 10
