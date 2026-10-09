"""Orchestration service: end-to-end offline runs, repairs, sessions, and review."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage

from agent.errors import ModelProviderError, PlanNotFoundError, ReviewConflictError, SessionBusyError
from agent.graph import graph_config
from agent.review import approval_is_current
from agent.schemas import CampaignPlanResponse, ReviewRequest, ReviewStatus, RevisionRequest
from evaluation.fake_openai import ScriptedOpenAI
from tests.helpers import plan_request

SESSION_A = "session_alpha_000000001"
SESSION_B = "session_bravo_000000002"


async def test_grounded_plan_is_pending_review_with_evidence_and_diagnostics(weather, make_service):
    weather("rainy_mornings")
    fake = ScriptedOpenAI()
    service = make_service(fake)
    response = await service.create_plan(plan_request(client_id="demo_coffee_shop"), request_id="req-grounded-1")
    plan = response.plan
    assert plan.status == ReviewStatus.PENDING_REVIEW
    assert plan.validation.status == "passed" and plan.validation.repair_attempts == 0
    assert plan.windows and all(w.grounding.verified and w.constraint_check.eligible for w in plan.windows)
    for window in plan.windows:
        assert window.start_local.hour >= 6 and window.end_local.hour <= 11  # coffee shop hours
        ids = {obs.observation_id for obs in plan.evidence.forecast}
        assert set(window.observation_ids) <= ids
    d = plan.diagnostics
    assert d.tool_calls == {"geocode_city": 1, "get_forecast": 1, "get_current_weather": 1}
    assert d.token_usage is not None and d.token_usage.calls_without_usage == 0
    assert d.estimated_cost_usd is None and "not configured" in d.cost_note
    assert d.model == "gpt-4o-mini" and d.recursion_limit == 10
    # The response round-trips through its own contract.
    assert CampaignPlanResponse.model_validate(response.model_dump(mode="json")) == response
    assert "## Recommended Campaign Windows" in response.report_markdown
    assert "## Forecast Summary" in response.report_markdown


async def test_constraint_violation_is_repaired_and_never_overridden(weather, make_service):
    weather("baseline_mild")
    service = make_service(ScriptedOpenAI(draft_policies=["ineligible_choice", "grounded"]))
    plan = (await service.create_plan(plan_request(client_id="demo_outdoor_fitness"), request_id="req-repair-1")).plan
    assert plan.status == ReviewStatus.PENDING_REVIEW
    assert plan.validation.repair_attempts == 1
    for window in plan.windows:
        assert window.constraint_check.eligible
        for obs in window.evidence:
            assert obs.precipitation_probability_pct <= 30 and obs.wind_speed_mph <= 15 and obs.aqi <= 2


async def test_persistent_invalid_output_fails_validation_after_bounded_repairs(weather, make_service):
    weather("baseline_mild")
    fake = ScriptedOpenAI(draft_policies=["invented_id"])
    service = make_service(fake)
    response = await service.create_plan(plan_request(), request_id="req-invalid-1")
    plan = response.plan
    assert plan.status == ReviewStatus.VALIDATION_FAILED
    assert plan.windows == [] and plan.rejected_windows
    assert plan.validation.repair_attempts == 2 and fake.draft_calls == 3
    assert "unknown_observation" in {e.code for e in plan.validation.errors}
    assert "Validation Failed" in response.report_markdown
    assert "## Recommended Campaign Windows" not in response.report_markdown


async def test_no_eligible_blocks_skips_drafting(weather, make_service):
    weather("windy")
    fake = ScriptedOpenAI()
    service = make_service(fake)
    plan = (await service.create_plan(plan_request(client_id="demo_outdoor_fitness"), request_id="req-windy-1")).plan
    assert plan.status == ReviewStatus.VALIDATION_FAILED
    assert fake.draft_calls == 0 and plan.validation.drafting_attempted is False
    assert "wind above the limit" in plan.validation.errors[0].message


async def test_provider_outage_is_explicit_missing_evidence(weather, make_service):
    weather("baseline_mild:owm_down")
    plan = (await make_service().create_plan(plan_request(), request_id="req-outage-1")).plan
    assert plan.status == ReviewStatus.VALIDATION_FAILED
    assert "provider_unavailable" in plan.diagnostics.provider_error_categories
    assert {e.code for e in plan.validation.errors} >= {"missing_forecast_evidence"}


async def test_model_failures_raise_categorised_errors(weather, make_service):
    weather("baseline_mild")
    with pytest.raises(ModelProviderError) as server_error:
        await make_service(ScriptedOpenAI(failure="model_500")).create_plan(plan_request(), request_id="req-m500")
    assert server_error.value.code == "model_provider_error" and server_error.value.http_status == 502
    with pytest.raises(ModelProviderError) as timeout:
        await make_service(ScriptedOpenAI(failure="model_timeout")).create_plan(plan_request(), request_id="req-mto")
    assert timeout.value.code == "model_timeout" and timeout.value.http_status == 504


async def test_recursion_limit_keeps_gathered_state_and_reports_it(weather, make_service):
    weather("baseline_mild")
    plan = (
        await make_service(ScriptedOpenAI(agent_policy="loop_tools")).create_plan(
            plan_request(), request_id="req-loop-1"
        )
    ).plan
    assert plan.diagnostics.step_limit_reached is True
    assert plan.status == ReviewStatus.VALIDATION_FAILED
    assert "step_limit_reached" in {w.code for w in plan.validation.warnings}


async def test_two_sessions_with_interleaved_requests_never_share_history(weather, make_service):
    weather("baseline_mild")
    fake = ScriptedOpenAI()
    service = make_service(fake)
    briefs = [
        (SESSION_A, "Austin, TX", "Coffee shop"),
        (SESSION_B, "Seattle, WA", "Surf shop"),
        (SESSION_A, "Denver, CO", "Ice cream parlor"),
        (SESSION_B, "Miami, FL", "Bakery"),
    ]
    for index, (session, location, business) in enumerate(briefs):
        await service.create_plan(
            plan_request(session_id=session, location=location, business_type=business), request_id=f"req-iso-{index}"
        )
    graph = await service.runtime.get_graph()
    history = {}
    for session in (SESSION_A, SESSION_B):
        state = await graph.aget_state(graph_config(session))
        history[session] = [m.content for m in state.values["messages"] if isinstance(m, HumanMessage)]
    assert [h.splitlines()[2] for h in history[SESSION_A]] == ["Location: Austin, TX", "Location: Denver, CO"]
    assert [h.splitlines()[2] for h in history[SESSION_B]] == ["Location: Seattle, WA", "Location: Miami, FL"]
    # The model never received another session's brief: inspect every agent request.
    for body in fake.requests:
        text = " ".join(str(m.get("content")) for m in body["messages"])
        sessions_seen = {
            "A": "Austin, TX" in text or "Denver, CO" in text,
            "B": "Seattle, WA" in text or "Miami, FL" in text,
        }
        assert not (sessions_seen["A"] and sessions_seen["B"])


async def test_concurrent_requests_in_one_session_are_rejected_not_interleaved(weather, make_service):
    weather("baseline_mild")
    service = make_service()
    first = asyncio.create_task(service.create_plan(plan_request(session_id=SESSION_A), request_id="req-busy-1"))
    await asyncio.sleep(0)
    with pytest.raises(SessionBusyError):
        await service.create_plan(plan_request(session_id=SESSION_A), request_id="req-busy-2")
    assert (await first).plan.request_id == "req-busy-1"


async def test_review_lifecycle_and_stale_approval(weather, make_service):
    weather("rainy_mornings")
    service = make_service()
    plan = (await service.create_plan(plan_request(session_id=SESSION_A), request_id="req-review-1")).plan
    approve = ReviewRequest(session_id=SESSION_A, decision="approve", plan_hash=plan.plan_hash, note="Looks good")
    approved = (await service.review(plan.request_id, approve)).plan
    assert approved.status == ReviewStatus.APPROVED and approval_is_current(approved)
    assert approved.plan_hash == plan.plan_hash  # approval does not change the content hash

    revision = RevisionRequest(
        session_id=SESSION_A, base_plan_hash=approved.plan_hash, ad_copy={"w1": ["Fresh copy for the rainy morning."]}
    )
    revised = (await service.revise(plan.request_id, revision)).plan
    assert revised.revision == 2 and revised.status == ReviewStatus.PENDING_REVIEW
    assert revised.plan_hash != approved.plan_hash and revised.review.approved_plan_hash is None
    assert "approval_invalidated" in [event.action for event in revised.review.history]

    with pytest.raises(ReviewConflictError) as stale:
        await service.review(plan.request_id, approve)  # old hash
    assert stale.value.code == "stale_plan"

    reject = ReviewRequest(session_id=SESSION_A, decision="reject", plan_hash=revised.plan_hash)
    rejected = (await service.review(plan.request_id, reject)).plan
    assert rejected.status == ReviewStatus.REJECTED and not approval_is_current(rejected)
    with pytest.raises(ReviewConflictError) as invalid:
        await service.review(
            plan.request_id, ReviewRequest(session_id=SESSION_A, decision="approve", plan_hash=rejected.plan_hash)
        )
    assert invalid.value.code == "invalid_transition"


async def test_failed_plans_cannot_be_approved_and_other_sessions_cannot_see_plans(weather, make_service):
    weather("windy")
    service = make_service()
    plan = (
        await service.create_plan(
            plan_request(session_id=SESSION_A, client_id="demo_outdoor_fitness"), request_id="req-fail-1"
        )
    ).plan
    with pytest.raises(ReviewConflictError) as blocked:
        await service.review(
            plan.request_id, ReviewRequest(session_id=SESSION_A, decision="approve", plan_hash=plan.plan_hash)
        )
    assert blocked.value.code == "plan_not_validated"
    with pytest.raises(PlanNotFoundError):
        await service.review(
            plan.request_id, ReviewRequest(session_id=SESSION_B, decision="approve", plan_hash=plan.plan_hash)
        )


async def test_ad_copy_revisions_are_validated(weather, make_service):
    weather("rainy_mornings")
    service = make_service()
    plan = (await service.create_plan(plan_request(session_id=SESSION_A), request_id="req-copy-1")).plan
    from agent.errors import ClearCastError

    with pytest.raises(ClearCastError) as bad:
        await service.revise(
            plan.request_id,
            RevisionRequest(
                session_id=SESSION_A, base_plan_hash=plan.plan_hash, ad_copy={"w1": ["Guaranteed sellout, proven ROI!"]}
            ),
        )
    assert bad.value.code == "invalid_revision"
    with pytest.raises(ClearCastError):
        await service.revise(
            plan.request_id,
            RevisionRequest(session_id=SESSION_A, base_plan_hash=plan.plan_hash, ad_copy={"w9": ["Unknown window"]}),
        )
