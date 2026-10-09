"""Internal FastAPI service: authentication, contract, error envelopes, and readiness."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.schemas import CampaignPlanRequest, CampaignPlanResponse, ErrorResponse
from api.main import create_app
from evaluation.fake_openai import ScriptedOpenAI
from evaluation.harness import build_service
from tests.helpers import PROJECT_ROOT

TOKEN = "internal-test-token-0123456789"
HEADERS = {"x-clearcast-internal-token": TOKEN}
CASES = json.loads((PROJECT_ROOT / "contracts" / "fixtures" / "plan_requests.json").read_text(encoding="utf-8"))[
    "cases"
]
BODY = {"session_id": "session_api_test_000001", "brief": {"location": "Baltimore, MD", "business_type": "Coffee shop"}}


@pytest.fixture
def client(weather):
    weather("rainy_mornings")
    app = create_app(build_service(ScriptedOpenAI()), internal_token=TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def test_token_is_required_except_for_health(client):
    assert client.get("/internal/health").json()["status"] == "ok"
    for response in (
        client.post("/internal/v1/campaign-plans", json=BODY),
        client.post("/internal/v1/campaign-plans", json=BODY, headers={"x-clearcast-internal-token": "wrong"}),
        client.get("/internal/ready"),
    ):
        assert response.status_code == 401
        assert ErrorResponse.model_validate(response.json()).error.code == "unauthorized"


def test_interactive_docs_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_ready_reports_discovered_tools(client):
    response = client.get("/internal/ready", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True and body["model"] == "gpt-4o-mini"
    assert body["tools"] == ["geocode_city", "get_current_weather", "get_forecast", "get_air_quality"]


def test_plan_round_trip_matches_contract_and_propagates_request_id(client):
    response = client.post(
        "/internal/v1/campaign-plans", json=BODY, headers={**HEADERS, "x-request-id": "req-api-0001"}
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-api-0001"
    parsed = CampaignPlanResponse.model_validate(response.json())
    assert parsed.plan.request_id == "req-api-0001"
    assert parsed.plan.status.value == "pending_review"


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_contract_fixtures_against_pydantic(case):
    try:
        CampaignPlanRequest.model_validate(case["body"])
        valid = True
    except ValueError:
        valid = False
    assert valid is case["api_valid"]


def test_invalid_payloads_get_a_safe_error_envelope(client):
    invalid = [case for case in CASES if not case["api_valid"]]
    for case in invalid:
        response = client.post("/internal/v1/campaign-plans", json=case["body"], headers=HEADERS)
        assert response.status_code == 422, case["name"]
        error = ErrorResponse.model_validate(response.json()).error
        assert error.code == "invalid_request" and error.details
    # Validation messages never echo submitted values.
    response = client.post(
        "/internal/v1/campaign-plans",
        json={**BODY, "brief": {**BODY["brief"], "tone": "sk-proj-SECRETSECRETSECRET"}},
        headers=HEADERS,
    )
    assert "SECRETSECRET" not in response.text


def test_review_endpoints_and_conflicts(client):
    plan = client.post("/internal/v1/campaign-plans", json=BODY, headers=HEADERS).json()["plan"]
    path = f"/internal/v1/campaign-plans/{plan['request_id']}"
    review = {"session_id": BODY["session_id"], "decision": "approve", "plan_hash": plan["plan_hash"]}
    approved = client.post(f"{path}/review", json=review, headers=HEADERS)
    assert approved.status_code == 200 and approved.json()["plan"]["status"] == "approved"

    revision = {"session_id": BODY["session_id"], "base_plan_hash": plan["plan_hash"], "ad_copy": {"w1": ["New line"]}}
    revised = client.post(f"{path}/revisions", json=revision, headers=HEADERS).json()["plan"]
    assert revised["status"] == "pending_review" and revised["revision"] == 2

    stale = client.post(f"{path}/review", json=review, headers=HEADERS)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_plan"
    other = client.post(f"{path}/review", json={**review, "session_id": "session_someone_else_01"}, headers=HEADERS)
    assert other.status_code == 404 and other.json()["error"]["code"] == "plan_not_found"
    bad_id = client.post("/internal/v1/campaign-plans/..%2Fx/review", json=review, headers=HEADERS)
    assert bad_id.status_code in (404, 422)


def test_model_failures_map_to_gateway_friendly_statuses(weather):
    weather("baseline_mild")
    for failure, status, code in (("model_500", 502, "model_provider_error"), ("model_timeout", 504, "model_timeout")):
        app = create_app(build_service(ScriptedOpenAI(failure=failure)), internal_token=TOKEN)
        with TestClient(app) as test_client:
            response = test_client.post("/internal/v1/campaign-plans", json=BODY, headers=HEADERS)
        assert response.status_code == status
        error = response.json()["error"]
        assert error["code"] == code and error["retryable"] is True
        assert "scripted" not in error["message"]  # provider messages are not passed through


def test_missing_configuration_is_service_unavailable(monkeypatch):
    from agent.runtime import AgentRuntime
    from agent.service import CampaignPlanningService

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(CampaignPlanningService(AgentRuntime()), internal_token=TOKEN)
    with TestClient(app) as test_client:
        ready = test_client.get("/internal/ready", headers=HEADERS)
        assert ready.status_code == 503 and "OPENAI_API_KEY" in ready.json()["missing_configuration"]
        response = test_client.post("/internal/v1/campaign-plans", json=BODY, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "configuration_missing"


def test_app_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("CLEARCAST_INTERNAL_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        create_app(build_service(ScriptedOpenAI()))
