"""ClearCast Gradio Blocks frontend.

The UI collects the campaign brief and client rules, sends them through the
Node.js gateway, and renders the validated plan, its evidence, review
controls, and exports. Each browser session gets a server-generated session
identifier held in Gradio session state (``gr.State``), never a shared default.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from agent.clients import load_profiles
from agent.report import render_diagnostics, render_evidence
from agent.schemas import REVIEW_STATUS_LABELS, WEEKDAYS, CampaignPlan, ClientConstraints, ReviewStatus
from frontend.gateway_client import GatewayClient, GatewayError
from frontend.theme import (
    ARCHITECTURE_HTML,
    CLEARCAST_CSS,
    CLEARCAST_THEME,
    EMPTY_REPORT_HTML,
    HERO_HTML,
    WORKFLOW_HTML,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

PROFILES = load_profiles()
CLIENT_CHOICES = [(profile.display_name, profile.client_id) for profile in PROFILES.values()]
AQI_CHOICES = ["No limit", "1 - Good", "2 - Fair", "3 - Moderate", "4 - Poor"]
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
HOUR_RANGE_RE = re.compile(r"^\s*(\d{1,2})(?::00)?\s*-\s*(\d{1,2})(?::00)?\s*$")
EXPORT_DIR = Path(tempfile.gettempdir()) / "clearcast-exports"
TONES = ["Friendly", "Urgent", "Playful", "Premium"]


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------
def new_session_id() -> str:
    """Server-generated, unguessable identifier for one Gradio browser session."""
    return secrets.token_urlsafe(24)


def ensure_session(session_id: str | None) -> str:
    return session_id if isinstance(session_id, str) and SESSION_ID_RE.match(session_id) else new_session_id()


def gateway() -> GatewayClient:
    return GatewayClient.from_env()


# --------------------------------------------------------------------------
# Client-constraint form helpers
# --------------------------------------------------------------------------
def format_hour_ranges(ranges) -> str:
    return ", ".join(f"{r.start_hour:02d}-{r.end_hour:02d}" for r in ranges)


def parse_hour_ranges(text: str | None) -> list[dict]:
    ranges = []
    for part in [chunk for chunk in (text or "").split(",") if chunk.strip()]:
        match = HOUR_RANGE_RE.match(part)
        if not match:
            raise ValueError(f"'{part.strip()}' is not an hour range like 06-11")
        ranges.append({"start_hour": int(match.group(1)), "end_hour": int(match.group(2))})
    return ranges


def format_exclusions(windows) -> str:
    return "; ".join(
        f"{','.join(w.weekdays)} {w.start_hour:02d}-{w.end_hour:02d}" + (f" ({w.reason})" if w.reason else "")
        for w in windows
    )


def parse_exclusions(text: str | None) -> list[dict]:
    windows = []
    for part in [chunk.strip() for chunk in (text or "").split(";") if chunk.strip()]:
        reason = ""
        if "(" in part and part.endswith(")"):
            part, reason = part[: part.index("(")].strip(), part[part.index("(") + 1 : -1].strip()
        days_text, _, hours = part.partition(" ")
        days = [day.strip().title()[:3] for day in days_text.split(",") if day.strip()]
        match = HOUR_RANGE_RE.match(hours)
        if not days or any(day not in WEEKDAYS for day in days) or not match:
            raise ValueError(f"'{part}' should look like 'Sun 06-08' or 'Sat,Sun 17-20 (reason)'")
        windows.append(
            {"weekdays": days, "start_hour": int(match.group(1)), "end_hour": int(match.group(2)), "reason": reason}
        )
    return windows


def aqi_label(value: int | None) -> str:
    return AQI_CHOICES[value] if value is not None and 1 <= value < len(AQI_CHOICES) else "No limit"


def build_constraints(
    allowed_hours, min_temp, max_temp, max_precip, max_wind, max_aqi, exclusions
) -> ClientConstraints:
    """Validate the form with the same Pydantic model the Python service uses."""
    try:
        return ClientConstraints(
            allowed_hours=parse_hour_ranges(allowed_hours),
            min_temperature_f=min_temp,
            max_temperature_f=max_temp,
            max_precipitation_probability_pct=max_precip,
            max_wind_speed_mph=max_wind,
            max_aqi=None if max_aqi in (None, "No limit") else int(str(max_aqi)[0]),
            exclusion_windows=parse_exclusions(exclusions),
        )
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        message = exc.errors()[0]["msg"] if hasattr(exc, "errors") else str(exc)
        raise ValueError(message.removeprefix("Value error, ")) from None


def profile_form_values(client_id: str, business_type: str, campaign_goal: str, tone: str) -> tuple:
    """Fill the brief and constraint fields from a client profile's defaults."""
    profile = PROFILES.get(client_id) or PROFILES["general"]
    c = profile.constraints
    if profile.client_id != "general":
        business_type = profile.business_type
        campaign_goal = profile.default_campaign_goal
        tone = profile.default_tone
    return (
        business_type,
        campaign_goal,
        tone,
        format_hour_ranges(c.allowed_hours),
        c.min_temperature_f,
        c.max_temperature_f,
        c.max_precipitation_probability_pct,
        c.max_wind_speed_mph,
        aqi_label(c.max_aqi),
        format_exclusions(c.exclusion_windows),
        client_note_html(profile.client_id),
    )


def client_note_html(client_id: str) -> str:
    profile = PROFILES.get(client_id) or PROFILES["general"]
    if profile.client_id == "general":
        return (
            '<p class="constraints-note">No client profile: only the rules you enter below are enforced. '
            "Hard rules are checked by Python against the forecast, not by the model.</p>"
        )
    return (
        f'<p class="constraints-note"><strong>{profile.display_name}</strong> is a fictional demo configuration. '
        f"Objective: {profile.business_objective} Preferences (soft, sent to the model): "
        f"{profile.preferences.preferred_conditions} Hard rules below are verified in Python.</p>"
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
STATUS_KEYS = {
    ReviewStatus.PENDING_REVIEW.value: "pending_review",
    ReviewStatus.APPROVED.value: "approved",
    ReviewStatus.REJECTED.value: "rejected",
    ReviewStatus.VALIDATION_FAILED.value: "validation_failed",
    ReviewStatus.DRAFT.value: "idle",
}


def status_html(key: str, label: str, detail: str = "") -> str:
    detail_html = f'<span class="review-note" style="margin-left:10px">{detail}</span>' if detail else ""
    return f'<div><span class="cc-status cc-status-{key}">{label}</span>{detail_html}</div>'


def report_header_html(key: str = "idle", label: str = "Agent ready") -> str:
    return f"""
<div class="report-header">
  <div class="report-title-wrap">
    <h2>Campaign Strategy Report</h2>
    <p>Recommended windows, weather reasoning, ad copy, and risk notes.</p>
  </div>
  <span class="cc-status cc-status-{key}">{label}</span>
</div>
"""


def ad_copy_editor_text(plan: dict) -> str:
    sections = []
    for window in plan.get("windows", []):
        sections.append("\n".join([f"[{window['window_id']}] {window['title']}", *window["ad_copy"]]))
    return "\n\n".join(sections)


def parse_ad_copy_editor(text: str, plan: dict) -> dict[str, list[str]]:
    edits: dict[str, list[str]] = {}
    current: str | None = None
    for line in (text or "").splitlines():
        header = re.match(r"^\[(w\d+)\]", line.strip())
        if header:
            current = header.group(1)
            edits[current] = []
        elif current and line.strip():
            edits[current].append(line.strip())
    original = {w["window_id"]: w["ad_copy"] for w in plan.get("windows", [])}
    return {wid: lines for wid, lines in edits.items() if lines != original.get(wid)}


def write_exports(response: dict) -> tuple[str, str]:
    """Write JSON and Markdown exports; filenames carry the review status."""
    plan = response["plan"]
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"clearcast-{plan['request_id']}-r{plan['revision']}-{plan['status']}"
    json_path = EXPORT_DIR / f"{stem}.json"
    md_path = EXPORT_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    parsed = CampaignPlan.model_validate(plan)
    markdown = "\n\n".join(
        [
            f"# ClearCast campaign plan ({REVIEW_STATUS_LABELS[parsed.status]})",
            response["report_markdown"],
            "## Evidence",
            render_evidence(parsed),
            "## Execution diagnostics",
            render_diagnostics(parsed),
        ]
    )
    md_path.write_text(markdown, encoding="utf-8")
    return str(json_path), str(md_path)


def _plan_views(response: dict, session_id: str, notice: str | None = None) -> tuple:
    plan = response["plan"]
    parsed = CampaignPlan.model_validate(plan)
    status = plan["status"]
    key = STATUS_KEYS.get(status, "idle")
    label = REVIEW_STATUS_LABELS[parsed.status]
    report = response["report_markdown"]
    if notice:
        report = f"> **Notice:** {notice}\n\n{report}"
    json_path, md_path = write_exports(response)
    pending = status == ReviewStatus.PENDING_REVIEW.value
    revisable = status in {ReviewStatus.PENDING_REVIEW.value, ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value}
    detail = f"Request {plan['request_id']} · revision {plan['revision']}"
    return (
        report,
        render_evidence(parsed),
        render_diagnostics(parsed),
        json.dumps(plan, indent=2, ensure_ascii=False),
        status_html(key, label, detail),
        report_header_html(key, label),
        ad_copy_editor_text(plan),
        gr.Button(interactive=pending),
        gr.Button(interactive=pending),
        gr.Button(interactive=revisable),
        gr.DownloadButton(value=json_path, interactive=True),
        gr.DownloadButton(value=md_path, interactive=True),
        response,
        session_id,
    )


def _message_views(
    message: str, session_id: str, *, key: str = "error", label: str = "Needs attention", response: dict | None = None
) -> tuple:
    if response is not None:
        return _plan_views(response, session_id, notice=message)
    return (
        f"> **{label}:** {message}",
        "",
        "",
        "",
        status_html(key, label),
        report_header_html(key, label),
        "",
        gr.Button(interactive=False),
        gr.Button(interactive=False),
        gr.Button(interactive=False),
        gr.DownloadButton(value=None, interactive=False),
        gr.DownloadButton(value=None, interactive=False),
        None,
        session_id,
    )


def describe_error(err: GatewayError) -> str:
    text = err.message
    if err.details:
        text += " " + "; ".join(f"{d.get('path', '')}: {d.get('message', '')}" for d in err.details[:5])
    if err.retryable:
        text += " You can try again shortly."
    if err.request_id:
        text += f" (request {err.request_id})"
    return text


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------
def on_load(session_id: str | None) -> tuple[str, str]:
    session_id = ensure_session(session_id)
    ready, body = gateway().readiness()
    if ready:
        return session_id, report_header_html("ready", "Agent ready")
    orchestrator = body.get("orchestrator") if isinstance(body, dict) else None
    missing = orchestrator.get("missing_configuration") if isinstance(orchestrator, dict) else None
    if missing:
        return session_id, report_header_html("error", f"Missing configuration: {', '.join(missing)}")
    return session_id, report_header_html("warming", "Agent warming up")


def generate_plan(
    client_id,
    location,
    business_type,
    campaign_goal,
    tone,
    allowed_hours,
    min_temp,
    max_temp,
    max_precip,
    max_wind,
    max_aqi,
    exclusions,
    session_id,
):
    """Validate the form, call the gateway, and render the validated plan."""
    session_id = ensure_session(session_id)
    if not (location or "").strip():
        return _message_views("Please enter a location.", session_id)
    if not (business_type or "").strip():
        return _message_views("Please enter a business type.", session_id)
    try:
        constraints = build_constraints(allowed_hours, min_temp, max_temp, max_precip, max_wind, max_aqi, exclusions)
    except ValueError as exc:
        return _message_views(f"Check the client constraints: {exc}", session_id)
    profile = PROFILES.get(client_id) or PROFILES["general"]
    payload = {
        "session_id": session_id,
        "client_id": profile.client_id,
        "brief": {
            "location": location.strip(),
            "business_type": business_type.strip(),
            "campaign_goal": (campaign_goal or "").strip(),
            "tone": tone if tone in TONES else "Friendly",
        },
        # Unchanged profile defaults are sent as null so the plan records their source.
        "constraints": None if constraints == profile.constraints else constraints.model_dump(mode="json"),
    }
    try:
        response = gateway().create_plan(payload)
    except GatewayError as err:
        return _message_views(describe_error(err), session_id)
    return _plan_views(response, session_id)


def _decide(decision: str, note: str, state: dict | None, session_id: str | None):
    session_id = ensure_session(session_id)
    if not state:
        return _message_views("Generate a plan before reviewing it.", session_id)
    plan = state["plan"]
    payload = {"session_id": session_id, "decision": decision, "plan_hash": plan["plan_hash"], "note": note or ""}
    try:
        response = gateway().review(plan["request_id"], payload)
    except GatewayError as err:
        return _message_views(describe_error(err), session_id, response=state)
    return _plan_views(response, session_id)


def approve_plan(note, state, session_id):
    return _decide("approve", note, state, session_id)


def reject_plan(note, state, session_id):
    return _decide("reject", note, state, session_id)


def revise_plan(ad_copy_text, note, state, session_id):
    session_id = ensure_session(session_id)
    if not state:
        return _message_views("Generate a plan before revising it.", session_id)
    plan = state["plan"]
    edits = parse_ad_copy_editor(ad_copy_text, plan)
    if not edits:
        return _message_views("No ad copy changes to save.", session_id, response=state)
    payload = {"session_id": session_id, "base_plan_hash": plan["plan_hash"], "ad_copy": edits, "note": note or ""}
    try:
        response = gateway().revise(plan["request_id"], payload)
    except GatewayError as err:
        return _message_views(describe_error(err), session_id, response=state)
    return _plan_views(response, session_id, notice="Revision saved. Any earlier approval no longer applies.")


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------
with gr.Blocks(title="ClearCast", fill_width=True) as app:
    session_state = gr.State(None)
    plan_state = gr.State(None)
    gr.HTML(HERO_HTML)

    with gr.Row(elem_classes=["dashboard-grid"]):
        with gr.Column(scale=5, min_width=330, elem_classes=["glass-panel", "input-panel"]):
            gr.HTML(
                """
                <div class="panel-heading">
                  <div class="panel-eyebrow">Campaign Brief</div>
                  <h2>Plan your next weather moment</h2>
                  <p>Define the market and objective. ClearCast will handle the forecast intelligence.</p>
                </div>
                """
            )
            client_profile = gr.Dropdown(
                label="Client Profile",
                choices=CLIENT_CHOICES,
                value="general",
                info="Fictional demo clients show per-customer configuration of one shared agent.",
            )
            location = gr.Textbox(
                label="City, State or Country",
                placeholder="Baltimore, MD",
                info="Enter the market where the campaign will run.",
            )
            business_type = gr.Textbox(
                label="Business Type",
                placeholder="Coffee shop",
                info="Example: coffee shop, gym, surf shop, ice cream parlor",
            )
            campaign_goal = gr.Textbox(
                label="Campaign Goal",
                placeholder="Increase morning visits this weekend",
                info="Example: increase morning visits, promote weekend sale",
                lines=2,
            )
            tone = gr.Dropdown(
                label="Ad Copy Tone",
                choices=TONES,
                value="Friendly",
                info="Choose the voice for the suggested campaign copy.",
            )
            with gr.Accordion(
                "Client constraints (hard rules verified in Python)", open=False, elem_classes=["constraints-panel"]
            ):
                client_note = gr.HTML(client_note_html("general"))
                allowed_hours = gr.Textbox(label="Allowed local hours", placeholder="06-11, 17-20 (blank = any time)")
                with gr.Row():
                    min_temp = gr.Number(label="Min temp (°F)", value=None)
                    max_temp = gr.Number(label="Max temp (°F)", value=None)
                with gr.Row():
                    max_precip = gr.Number(label="Max precip. probability (%)", value=None, minimum=0, maximum=100)
                    max_wind = gr.Number(label="Max wind (mph)", value=None, minimum=0)
                max_aqi = gr.Dropdown(label="Max AQI (OpenWeatherMap 1-5)", choices=AQI_CHOICES, value="No limit")
                exclusions = gr.Textbox(label="Exclusion windows", placeholder="Sun 06-08 (Opens at 8 on Sundays)")
            submit_btn = gr.Button(
                "Generate Campaign Strategy",
                variant="primary",
                size="lg",
                elem_id="strategy-button",
            )
            gr.HTML(
                """
                <div class="security-note">
                  <span class="security-dot"></span>
                  Live forecast data is used only to generate this strategy. Nothing is published.
                </div>
                """
            )

        with gr.Column(scale=8, min_width=520, elem_classes=["glass-panel", "report-panel"]):
            report_header = gr.HTML(report_header_html())
            with gr.Tabs(elem_classes=["report-tabs"]):
                with gr.Tab("Report"):
                    output = gr.Markdown(value=EMPTY_REPORT_HTML, show_label=False, elem_classes=["report-output"])
                with gr.Tab("Windows & evidence"):
                    evidence_md = gr.Markdown(
                        value="Generate a plan to inspect its cited forecast evidence.", elem_classes=["report-output"]
                    )
                with gr.Tab("Diagnostics"):
                    diagnostics_md = gr.Markdown(
                        value="Execution diagnostics appear after a request.", elem_classes=["report-output"]
                    )
                with gr.Tab("Plan JSON"):
                    plan_json = gr.Code(value="", language="json", show_label=False, elem_classes=["plan-json"])
            with gr.Column(elem_classes=["review-panel"]):
                gr.HTML(
                    "<h3>Human review</h3><p class='review-note'>Only validated plans can be approved. "
                    "Editing ad copy creates a new revision and invalidates any earlier approval.</p>"
                )
                review_status = gr.HTML(status_html("idle", "No plan yet"))
                reviewer_note = gr.Textbox(label="Reviewer note (optional)", lines=1, max_length=500)
                with gr.Row():
                    approve_btn = gr.Button("Approve plan", interactive=False, elem_id="approve-button")
                    reject_btn = gr.Button("Reject plan", interactive=False, elem_id="reject-button")
                with gr.Accordion("Revise ad copy", open=False):
                    ad_copy_editor = gr.Textbox(
                        label="Ad copy by window",
                        lines=6,
                        info="Keep the [w1] headers; one ad line per row (max 3 per window).",
                    )
                    revise_btn = gr.Button("Save ad-copy revision", interactive=False)
                with gr.Row():
                    json_download = gr.DownloadButton("Export JSON", value=None, interactive=False)
                    md_download = gr.DownloadButton("Export Markdown report", value=None, interactive=False)

    gr.HTML(
        """
        <div class="section-label">
          <h2>How it works</h2>
          <p>From campaign brief to reviewed, exportable recommendation.</p>
        </div>
        """
    )
    gr.HTML(WORKFLOW_HTML)
    gr.HTML(ARCHITECTURE_HTML)

    gr.HTML(
        """
        <div class="section-label">
          <h2>Example campaigns</h2>
          <p>Start with a polished brief, then tailor it to your own market.</p>
        </div>
        """
    )
    with gr.Column(elem_classes=["examples-card"]):
        gr.Examples(
            examples=[
                ["Baltimore, MD", "Coffee shop", "Increase morning visits this weekend", "Friendly"],
                ["Austin, TX", "Fitness studio", "Drive outdoor class signups", "Urgent"],
                ["Chicago, IL", "Ice cream parlor", "Boost weekend foot traffic", "Playful"],
                ["Miami, FL", "Surf shop", "Promote the new premium collection", "Premium"],
            ],
            inputs=[location, business_type, campaign_goal, tone],
            label="Presentation-ready campaign briefs",
            examples_per_page=4,
        )

    plan_outputs = [
        output,
        evidence_md,
        diagnostics_md,
        plan_json,
        review_status,
        report_header,
        ad_copy_editor,
        approve_btn,
        reject_btn,
        revise_btn,
        json_download,
        md_download,
        plan_state,
        session_state,
    ]
    constraint_inputs = [allowed_hours, min_temp, max_temp, max_precip, max_wind, max_aqi, exclusions]

    client_profile.change(
        fn=profile_form_values,
        inputs=[client_profile, business_type, campaign_goal, tone],
        outputs=[business_type, campaign_goal, tone, *constraint_inputs, client_note],
        api_name=False,
    )
    submit_btn.click(
        fn=generate_plan,
        inputs=[client_profile, location, business_type, campaign_goal, tone, *constraint_inputs, session_state],
        outputs=plan_outputs,
        api_name="generate_plan",
        concurrency_limit=4,
    )
    approve_btn.click(
        fn=approve_plan,
        inputs=[reviewer_note, plan_state, session_state],
        outputs=plan_outputs,
        api_name="approve_plan",
    )
    reject_btn.click(
        fn=reject_plan, inputs=[reviewer_note, plan_state, session_state], outputs=plan_outputs, api_name="reject_plan"
    )
    revise_btn.click(
        fn=revise_plan,
        inputs=[ad_copy_editor, reviewer_note, plan_state, session_state],
        outputs=plan_outputs,
        api_name="revise_plan",
    )
    app.load(fn=on_load, inputs=[session_state], outputs=[session_state, report_header], api_name=False)


def launch(**kwargs):
    """Launch ClearCast with its existing Gradio theme and CSS."""
    launch_kwargs = {
        "theme": CLEARCAST_THEME,
        "css": CLEARCAST_CSS,
        "ssr_mode": False,
        "server_name": os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        "server_port": int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        "allowed_paths": [str(EXPORT_DIR)],
        "show_error": False,
    }
    launch_kwargs.update(kwargs)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    app.queue(default_concurrency_limit=4)
    return app.launch(**launch_kwargs)


if __name__ == "__main__":
    launch()
