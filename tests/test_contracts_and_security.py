"""Contract drift, client configuration validation, observability, and secret safety."""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest
from pydantic import ValidationError

from agent.clients import PROFILES_PATH, load_profiles
from agent.observability import RequestMetrics, configure_langsmith, redact, session_ref
from agent.schemas import CampaignPlanResponse, ClientConstraints
from api.export_contracts import main as export_contracts
from evaluation.fake_openai import ScriptedOpenAI
from tests.helpers import PROJECT_ROOT, plan_request


def test_committed_contracts_match_the_pydantic_models():
    assert export_contracts(["--check"]) == 0


def test_sample_response_fixture_is_contract_valid():
    sample = json.loads((PROJECT_ROOT / "contracts" / "fixtures" / "sample_plan_response.json").read_text("utf-8"))
    assert CampaignPlanResponse.model_validate(sample).plan.status.value == "pending_review"


def test_demo_client_profiles_are_validated_configuration():
    profiles = load_profiles()
    assert set(profiles) == {"general", "demo_coffee_shop", "demo_outdoor_fitness"}
    coffee, fitness = profiles["demo_coffee_shop"], profiles["demo_outdoor_fitness"]
    assert coffee.fictional and fitness.fictional and "fictional" in coffee.display_name
    assert fitness.constraints.max_aqi == 2 and fitness.requires_air_quality
    assert coffee.constraints.exclusion_windows[0].weekdays == ["Sun"]
    raw = json.loads(PROFILES_PATH.read_text("utf-8"))
    assert raw["_comment"].lower().count("fictional") >= 1


@pytest.mark.parametrize(
    "bad",
    [
        {"min_temperature_f": 90, "max_temperature_f": 50},
        {"allowed_hours": [{"start_hour": 9, "end_hour": 6}]},
        {"max_aqi": 0},
        {"unknown_rule": True},
    ],
)
def test_conflicting_or_unknown_constraints_are_rejected(bad):
    with pytest.raises(ValidationError):
        ClientConstraints.model_validate(bad)


def test_redaction_and_session_reference():
    text = "key sk-proj-FAKEKEYFAKEKEY12 hf_FAKEFAKEFAKE12 appid=abc123 lsv2_pt_fakefakefake Bearer fakebearertoken1"
    cleaned = redact(text)
    for secret in ("FAKEKEYFAKEKEY12", "hf_FAKEFAKEFAKE12", "abc123", "fakefakefake", "fakebearertoken1"):
        assert secret not in cleaned
    assert session_ref("session_abc") != "session_abc" and len(session_ref("session_abc")) == 12


def test_cost_is_only_estimated_when_prices_are_configured(monkeypatch):
    metrics = RequestMetrics("r", "s", "general", "gpt-4o-mini")
    assert metrics.cost() == (None, "Not calculated: the provider reported no token usage.")
    metrics.record_usage({"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500})
    assert metrics.cost()[0] is None
    monkeypatch.setenv("CLEARCAST_MODEL_INPUT_USD_PER_1M_TOKENS", "1.0")
    monkeypatch.setenv("CLEARCAST_MODEL_OUTPUT_USD_PER_1M_TOKENS", "2.0")
    assert metrics.cost()[0] == pytest.approx(0.002)
    metrics.record_usage(None)  # a call without usage makes the estimate unreliable
    assert metrics.cost()[0] is None


def test_langsmith_is_enabled_only_with_credentials(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert configure_langsmith() == {"langsmith_tracing": False}
    import os

    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_test_key")
    assert configure_langsmith() == {"langsmith_tracing": True}


async def test_logs_and_responses_never_contain_credentials_or_prompt_text(weather, make_service, caplog, monkeypatch):
    secret = "sk-proj-THISMUSTNEVERAPPEARANYWHERE"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    caplog.set_level(logging.DEBUG)
    weather("baseline_mild")
    response = await make_service(ScriptedOpenAI()).create_plan(
        plan_request(location="Secretville, ZZ"), request_id="req-secret-1"
    )
    dumped = response.model_dump_json()
    assert secret not in dumped and secret not in caplog.text
    completed = [r for r in caplog.records if getattr(r, "event", "") == "plan.completed"]
    assert completed and "Secretville" not in caplog.text  # user text is not logged


def test_secret_scanner_passes_on_the_repository():
    result = subprocess.run(
        [sys.executable, "scripts/check_secrets.py"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
