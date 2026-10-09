"""Gradio frontend: session identifiers, form parsing, gateway calls, review, and exports."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import frontend.app as ui
from frontend.gateway_client import GatewayClient, GatewayError
from tests.helpers import PROJECT_ROOT

SAMPLE = json.loads((PROJECT_ROOT / "contracts" / "fixtures" / "sample_plan_response.json").read_text("utf-8"))
REPORT, EVIDENCE, DIAG, PLAN_JSON, STATUS, HEADER, EDITOR = range(7)
APPROVE, REJECT, REVISE, JSON_DL, MD_DL, PLAN_STATE, SESSION = range(7, 14)


class RecordingGateway:
    """Stands in for the Node.js gateway over an httpx MockTransport."""

    def __init__(self, responder=None) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.responder = responder or (lambda path, body: (200, SAMPLE))

    def client(self) -> GatewayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            self.requests.append((request.url.path, body, dict(request.headers)))
            status, payload = self.responder(request.url.path, body)
            return httpx.Response(status, json=payload)

        return GatewayClient(base_url="http://gateway.test", token="ui-token", transport=httpx.MockTransport(handler))


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    recorder = RecordingGateway()
    monkeypatch.setattr(ui, "gateway", recorder.client)
    monkeypatch.setattr(ui, "EXPORT_DIR", tmp_path)
    return recorder


def form(**overrides):
    values = {
        "client_id": "general",
        "location": "Baltimore, MD",
        "business_type": "Coffee shop",
        "campaign_goal": "Increase morning visits",
        "tone": "Friendly",
        "allowed_hours": "",
        "min_temp": None,
        "max_temp": None,
        "max_precip": None,
        "max_wind": None,
        "max_aqi": "No limit",
        "exclusions": "",
        "session_id": None,
    }
    values.update(overrides)
    return list(values.values())


def test_session_ids_are_server_generated_unique_and_validated():
    ids = {ui.new_session_id() for _ in range(200)}
    assert len(ids) == 200 and all(ui.SESSION_ID_RE.match(value) for value in ids)
    assert ui.ensure_session("1") != "1"  # never the old shared default
    existing = ui.new_session_id()
    assert ui.ensure_session(existing) == existing


def test_constraint_form_parsing_and_validation():
    constraints = ui.build_constraints("06-09, 17:00-20:00", 45, 88, 30, 15, "2 - Fair", "Sat,sun 06-08 (Closed)")
    assert [(r.start_hour, r.end_hour) for r in constraints.allowed_hours] == [(6, 9), (17, 20)]
    assert constraints.max_aqi == 2
    assert constraints.exclusion_windows[0].weekdays == ["Sat", "Sun"]
    assert constraints.exclusion_windows[0].reason == "Closed"
    with pytest.raises(ValueError, match="hour range"):
        ui.build_constraints("morning", None, None, None, None, "No limit", "")
    with pytest.raises(ValueError, match="must not exceed"):
        ui.build_constraints("", 90, 50, None, None, "No limit", "")


def test_selecting_a_demo_client_fills_profile_defaults():
    values = ui.profile_form_values("demo_outdoor_fitness", "", "", "Friendly")
    business, _goal, tone, hours, min_t, max_t, precip, wind, aqi, _exclusions, note = values
    assert (business, tone, hours, min_t, max_t, precip, wind, aqi) == (
        "Outdoor fitness studio",
        "Urgent",
        "06-09, 17-20",
        45,
        88,
        30,
        15,
        "2 - Fair",
    )
    assert "fictional" in note
    # The general profile keeps what the user typed.
    assert ui.profile_form_values("general", "Bakery", "Sell bread", "Playful")[:3] == (
        "Bakery",
        "Sell bread",
        "Playful",
    )


def test_original_input_validation_messages_are_preserved(gateway):
    assert "Please enter a location." in ui.generate_plan(*form(location="  "))[REPORT]
    assert "Please enter a business type." in ui.generate_plan(*form(business_type=""))[REPORT]
    assert gateway.requests == []


def test_generation_goes_through_the_gateway_with_the_session_id(gateway):
    session = ui.new_session_id()
    outputs = ui.generate_plan(
        *form(
            client_id="demo_coffee_shop",
            allowed_hours="06-11",
            exclusions="Sun 06-08 (Store opens at 8 AM on Sundays)",
            session_id=session,
        )
    )
    path, body, headers = gateway.requests[0]
    assert path == "/v1/campaign-plans"
    assert body["session_id"] == session and outputs[SESSION] == session
    assert headers["x-clearcast-client-token"] == "ui-token" and headers["x-request-id"].startswith("ui-")
    assert body["constraints"] is None  # unchanged profile defaults are not sent as overrides
    assert outputs[PLAN_STATE] == SAMPLE
    assert "Pending Review" in outputs[STATUS]
    assert outputs[APPROVE].interactive and outputs[REJECT].interactive


def test_edited_constraints_are_sent_as_overrides(gateway):
    ui.generate_plan(
        *form(
            client_id="demo_outdoor_fitness",
            allowed_hours="06-09",
            min_temp=50,
            max_temp=85,
            max_precip=20,
            max_wind=10,
            max_aqi="1 - Good",
        )
    )
    constraints = gateway.requests[0][1]["constraints"]
    assert constraints["max_wind_speed_mph"] == 10 and constraints["max_aqi"] == 1


def test_two_browser_sessions_keep_distinct_identifiers(gateway):
    first = ui.generate_plan(*form(session_id=None))[SESSION]
    second = ui.generate_plan(*form(session_id=None))[SESSION]
    again = ui.generate_plan(*form(session_id=first))[SESSION]
    assert first != second and again == first
    assert [request[1]["session_id"] for request in gateway.requests] == [first, second, first]


def test_gateway_errors_are_rendered_safely(monkeypatch, tmp_path):
    recorder = RecordingGateway(
        lambda path, body: (
            503,
            {
                "error": {
                    "code": "configuration_missing",
                    "message": "ClearCast is missing required configuration: OPENAI_API_KEY.",
                    "request_id": "req-x",
                    "retryable": False,
                    "details": [],
                }
            },
        )
    )
    monkeypatch.setattr(ui, "gateway", recorder.client)
    outputs = ui.generate_plan(*form())
    assert "OPENAI_API_KEY" in outputs[REPORT] and "req-x" in outputs[REPORT]
    assert outputs[PLAN_STATE] is None and not outputs[APPROVE].interactive

    unreachable = GatewayClient(base_url="http://127.0.0.1:9", token="t")
    with pytest.raises(GatewayError) as caught:
        unreachable.create_plan({})
    assert caught.value.code == "gateway_unavailable"


def test_review_and_revision_flows_use_the_current_plan_hash(gateway):
    session = ui.new_session_id()
    state = ui.generate_plan(*form(session_id=session))[PLAN_STATE]
    ui.approve_plan("ok", state, session)
    path, body, _ = gateway.requests[-1]
    assert path.endswith(f"/{SAMPLE['plan']['request_id']}/review")
    assert body == {
        "session_id": session,
        "decision": "approve",
        "plan_hash": SAMPLE["plan"]["plan_hash"],
        "note": "ok",
    }

    editor = ui.ad_copy_editor_text(state["plan"])
    unchanged = ui.revise_plan(editor, "", state, session)
    assert "No ad copy changes" in unchanged[REPORT]
    edited = editor.replace(state["plan"]["windows"][0]["ad_copy"][0], "A brand new line")
    ui.revise_plan(edited, "", state, session)
    path, body, _ = gateway.requests[-1]
    assert path.endswith("/revisions") and body["ad_copy"]["w1"][0] == "A brand new line"
    assert body["base_plan_hash"] == SAMPLE["plan"]["plan_hash"]


def test_exports_carry_status_and_full_evidence(gateway, tmp_path):
    outputs = ui.generate_plan(*form())

    def file_path(button) -> Path:
        value = button.value  # Gradio wraps file values as FileData dicts
        return Path(value["path"] if isinstance(value, dict) else value)

    json_path, md_path = file_path(outputs[JSON_DL]), file_path(outputs[MD_DL])
    assert json_path.name.endswith("-pending_review.json") and md_path.suffix == ".md"
    exported = json.loads(json_path.read_text("utf-8"))
    assert exported["plan_hash"] == SAMPLE["plan"]["plan_hash"]
    markdown = md_path.read_text("utf-8")
    assert "(Pending Review)" in markdown and "## Evidence" in markdown and "## Execution diagnostics" in markdown


def test_hosted_launch_settings(monkeypatch):
    captured = {}
    monkeypatch.setattr(ui.app, "launch", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(ui.app, "queue", lambda **kwargs: None)
    monkeypatch.setenv("GRADIO_SERVER_NAME", "0.0.0.0")
    ui.launch()
    assert "inbrowser" not in captured and captured["ssr_mode"] is False
    assert captured["server_name"] == "0.0.0.0" and captured["server_port"] == 7860
