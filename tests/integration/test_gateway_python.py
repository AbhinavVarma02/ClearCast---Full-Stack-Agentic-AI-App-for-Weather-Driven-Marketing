"""Gradio handler -> Fastify gateway (Node.js) -> FastAPI -> mocked LangGraph/MCP -> validated response."""

from __future__ import annotations

import json
import os
import subprocess
import threading

import httpx
import pytest
import uvicorn

import frontend.app as ui
from agent.schemas import CampaignPlanResponse
from api.main import create_app
from evaluation.fake_openai import ScriptedOpenAI
from evaluation.harness import build_service, install_fixture_weather
from frontend.gateway_client import GatewayClient
from mcp_server import weather_api
from tests.helpers import PROJECT_ROOT
from tests.integration.conftest import GATEWAY_ENTRY, free_port, require_gateway, wait_http

pytestmark = pytest.mark.integration

INTERNAL_TOKEN = "integration-internal-token-0001"
CLIENT_TOKEN = "integration-client-token-0001"


@pytest.fixture(scope="module")
def stack():
    node = require_gateway()
    install_fixture_weather("baseline_mild")
    fake = ScriptedOpenAI()
    api_port, gateway_port = free_port(), free_port()
    app = create_app(build_service(fake), internal_token=INTERNAL_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_http(f"http://127.0.0.1:{api_port}/internal/health")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_PORT": str(gateway_port),
        "CLEARCAST_API_URL": f"http://127.0.0.1:{api_port}",
        "CLEARCAST_INTERNAL_TOKEN": INTERNAL_TOKEN,
        "CLEARCAST_GATEWAY_TOKEN": CLIENT_TOKEN,
        "GATEWAY_LOG_LEVEL": "warn",
    }
    gateway = subprocess.Popen([node, str(GATEWAY_ENTRY)], cwd=PROJECT_ROOT, env=env)
    try:
        wait_http(f"http://127.0.0.1:{gateway_port}/health", timeout=30)
        wait_http(f"http://127.0.0.1:{gateway_port}/ready", timeout=30)
        yield {"gateway_url": f"http://127.0.0.1:{gateway_port}", "fake": fake}
    finally:
        gateway.terminate()
        gateway.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=15)
        weather_api.set_client(None)


@pytest.fixture
def ui_gateway(stack, monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "gateway", lambda: GatewayClient(base_url=stack["gateway_url"], token=CLIENT_TOKEN))
    monkeypatch.setattr(ui, "EXPORT_DIR", tmp_path)
    return stack


def gradio_inputs(session_id, **overrides):
    values = dict(
        client_id="demo_outdoor_fitness",
        location="Austin, TX",
        business_type="Outdoor fitness studio",
        campaign_goal="Promote outdoor class signups",
        tone="Urgent",
        allowed_hours="06-09, 17-20",
        min_temp=45,
        max_temp=88,
        max_precip=30,
        max_wind=15,
        max_aqi="2 - Fair",
        exclusions="",
        session_id=session_id,
    )
    values.update(overrides)
    return list(values.values())


def test_gradio_request_traverses_gateway_and_returns_a_validated_plan(ui_gateway):
    session = ui.new_session_id()
    outputs = ui.generate_plan(*gradio_inputs(session))
    state = outputs[12]
    assert state is not None, outputs[0]
    plan = CampaignPlanResponse.model_validate(state).plan
    assert plan.status.value == "pending_review"
    assert plan.request_id.startswith("ui-")  # the UI's request id propagated through Node.js to Python
    assert plan.client.client_id == "demo_outdoor_fitness" and plan.client.constraints_source == "profile_default"
    assert all(w.constraint_check.eligible and w.grounding.verified for w in plan.windows)
    assert "Pending Review" in outputs[4]

    approved = ui.approve_plan("ok", state, session)
    assert "Approved" in approved[4]
    revised = ui.revise_plan(
        ui.ad_copy_editor_text(state["plan"]).replace("Spots", "Places", 1), "", approved[12], session
    )
    assert "Pending Review" in revised[4] and "revision 2" in revised[4]


def test_gateway_rejects_invalid_payloads_before_python(ui_gateway):
    before = len(ui_gateway["fake"].requests)
    response = httpx.post(
        f"{ui_gateway['gateway_url']}/v1/campaign-plans",
        json={"session_id": "short", "brief": {}},
        headers={"x-clearcast-client-token": CLIENT_TOKEN},
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert len(ui_gateway["fake"].requests) == before


def test_python_cross_field_validation_passes_through_the_gateway(ui_gateway):
    body = {
        "session_id": "integration_session_0001",
        "brief": {"location": "Austin", "business_type": "Gym"},
        "constraints": {"min_temperature_f": 90, "max_temperature_f": 50},
    }
    response = httpx.post(
        f"{ui_gateway['gateway_url']}/v1/campaign-plans", json=body, headers={"x-clearcast-client-token": CLIENT_TOKEN}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"


def test_gateway_readiness_reflects_the_orchestrator(ui_gateway):
    body = httpx.get(f"{ui_gateway['gateway_url']}/ready").json()
    assert body["status"] == "ready"
    assert body["orchestrator"]["tools"] == ["geocode_city", "get_current_weather", "get_forecast", "get_air_quality"]


def test_wrong_client_token_is_rejected(ui_gateway):
    response = httpx.post(
        f"{ui_gateway['gateway_url']}/v1/campaign-plans", json={}, headers={"x-clearcast-client-token": "nope"}
    )
    assert response.status_code == 401
    assert INTERNAL_TOKEN not in response.text and CLIENT_TOKEN not in json.dumps(response.json())
