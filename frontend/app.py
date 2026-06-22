"""ClearCast Gradio Blocks frontend.

This form-based interface turns campaign parameters into weather-driven
recommendations, following the Week 4 Sidekick Blocks pattern.
"""

import asyncio
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from agent.graph import build_graph, invoke_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)

# Gradio startup is synchronous in this setup, so the async graph factory is
# completed once before the first request.
print("Building ClearCast agent...")
graph = asyncio.run(build_graph())
print("Agent ready!")


def process_request(
    location: str, business_type: str, campaign_goal: str, tone: str
) -> str:
    """Validate the form, invoke the agent, and return rendered Markdown."""
    if not location.strip():
        return "Please enter a location."
    if not business_type.strip():
        return "Please enter a business type."

    # A structured message preserves the form semantics for the LLM.
    user_message = f"""Please analyze weather data and recommend campaign moments for:

Location: {location}
Business type: {business_type}
Campaign goal: {campaign_goal or 'General awareness'}
Preferred tone for ad copy: {tone}

Provide your full analysis with recommended campaign windows, weather reasoning,
suggested ad copy, risk notes, and a forecast summary table."""

    try:
        return invoke_graph(graph, user_message)
    except Exception as exc:  # Show the error type without echoing secret-bearing details.
        return (
            f"Request failed ({type(exc).__name__}). "
            "Please check your API keys and try again."
        )


CLEARCAST_THEME = gr.themes.Base(
    primary_hue="indigo",
    secondary_hue="violet",
    neutral_hue="slate",
    font=[
        gr.themes.GoogleFont("Inter"),
        "ui-sans-serif",
        "system-ui",
        "sans-serif",
    ],
    font_mono=[
        gr.themes.GoogleFont("JetBrains Mono"),
        "ui-monospace",
        "monospace",
    ],
)

CLEARCAST_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@600;700;800&display=swap');

:root {
    --cc-bg: #050810;
    --cc-surface: rgba(15, 23, 42, 0.66);
    --cc-surface-strong: rgba(17, 24, 39, 0.92);
    --cc-border: rgba(148, 163, 184, 0.14);
    --cc-border-bright: rgba(129, 140, 248, 0.36);
    --cc-text: #f8fafc;
    --cc-muted: #94a3b8;
    --cc-faint: #64748b;
    --cc-accent: #818cf8;
    --cc-accent-2: #a855f7;
    --cc-cyan: #22d3ee;
    --cc-display: "Plus Jakarta Sans", Inter, ui-sans-serif, system-ui, sans-serif;
}

/* ---------- Base canvas + animated aurora ---------- */
body,
.gradio-container {
    background: var(--cc-bg) !important;
    color: var(--cc-text) !important;
}

.gradio-container::before {
    content: "";
    position: fixed;
    inset: -20% -20% auto -20%;
    height: 90vh;
    z-index: 0;
    pointer-events: none;
    background:
        radial-gradient(40rem 30rem at 12% 6%, rgba(99, 102, 241, 0.26), transparent 60%),
        radial-gradient(38rem 28rem at 88% 12%, rgba(14, 165, 233, 0.18), transparent 60%),
        radial-gradient(42rem 30rem at 50% 0%, rgba(168, 85, 247, 0.16), transparent 62%);
    filter: blur(8px);
    animation: cc-aurora 18s ease-in-out infinite alternate;
}

@keyframes cc-aurora {
    0%   { transform: translate3d(0, 0, 0) scale(1); opacity: 0.9; }
    50%  { transform: translate3d(0, 2.4%, 0) scale(1.06); opacity: 1; }
    100% { transform: translate3d(-1.5%, -1.5%, 0) scale(1.02); opacity: 0.85; }
}

.gradio-container {
    position: relative;
    z-index: 1;
    max-width: 1500px !important;
    margin: 0 auto !important;
    padding: 30px clamp(18px, 3vw, 48px) 64px !important;
}

footer { display: none !important; }

/* ---------- Scrollbars ---------- */
* { scrollbar-width: thin; scrollbar-color: rgba(129, 140, 248, 0.4) transparent; }
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(129, 140, 248, 0.55), rgba(168, 85, 247, 0.5));
    border: 2px solid transparent;
    background-clip: content-box;
}
*::-webkit-scrollbar-thumb:hover { background: rgba(129, 140, 248, 0.8); background-clip: content-box; }

/* ---------- Entrance animation ---------- */
@keyframes cc-fade-up {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
}
.cc-hero,
.dashboard-grid,
.section-label,
.workflow-strip,
.architecture-rail,
.examples-card { animation: cc-fade-up 0.7s cubic-bezier(0.22, 1, 0.36, 1) both; }
.dashboard-grid { animation-delay: 0.06s; }
.section-label { animation-delay: 0.1s; }
.workflow-strip { animation-delay: 0.14s; }

/* ---------- Hero ---------- */
.cc-hero {
    position: relative;
    overflow: hidden;
    padding: clamp(30px, 5vw, 62px);
    margin-bottom: 26px;
    border: 1px solid var(--cc-border-bright);
    border-radius: 30px;
    background:
        linear-gradient(125deg, rgba(30, 41, 59, 0.92), rgba(13, 18, 32, 0.86)),
        linear-gradient(125deg, rgba(99, 102, 241, 0.3), rgba(14, 165, 233, 0.16));
    box-shadow: 0 32px 90px rgba(0, 0, 0, 0.46), inset 0 1px rgba(255, 255, 255, 0.07);
}

.cc-hero::after {
    content: "";
    position: absolute;
    width: 440px;
    height: 440px;
    right: -140px;
    top: -210px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(129, 140, 248, 0.5), transparent 68%);
    pointer-events: none;
    animation: cc-aurora 14s ease-in-out infinite alternate;
}

.cc-brand { display: flex; align-items: center; gap: 14px; margin-bottom: 30px; position: relative; z-index: 1; }

.cc-logo {
    display: grid;
    place-items: center;
    width: 48px;
    height: 48px;
    border-radius: 15px;
    color: white;
    background: linear-gradient(135deg, #6366f1, #8b5cf6 55%, #0ea5e9);
    box-shadow: 0 14px 30px rgba(99, 102, 241, 0.42), inset 0 1px rgba(255, 255, 255, 0.3);
}
.cc-logo svg { width: 27px; height: 27px; }
.cc-wordmark { font-family: var(--cc-display); font-size: 1.1rem; font-weight: 800; letter-spacing: 0.01em; }
.cc-kicker {
    margin-left: 2px;
    padding: 5px 11px;
    border: 1px solid rgba(129, 140, 248, 0.32);
    border-radius: 999px;
    color: #c7d2fe;
    background: rgba(99, 102, 241, 0.14);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
}

.cc-hero h1 {
    position: relative;
    z-index: 1;
    max-width: 880px;
    margin: 0 0 16px;
    color: white;
    font-family: var(--cc-display);
    font-size: clamp(2.5rem, 5.4vw, 4.8rem);
    line-height: 0.98;
    letter-spacing: -0.055em;
}

.cc-gradient-text {
    background: linear-gradient(90deg, #c7d2fe, #a5b4fc 38%, #67e8f9);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}

.cc-subtitle {
    position: relative;
    z-index: 1;
    max-width: 820px;
    margin: 0 0 12px;
    color: #dbeafe;
    font-size: clamp(1rem, 1.6vw, 1.28rem);
    font-weight: 600;
}

.cc-description {
    position: relative;
    z-index: 1;
    max-width: 760px;
    margin: 0;
    color: var(--cc-muted);
    font-size: 0.99rem;
    line-height: 1.75;
}

.cc-chips { position: relative; z-index: 1; display: flex; flex-wrap: wrap; gap: 9px; margin-top: 26px; }
.cc-chip {
    padding: 7px 12px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 999px;
    color: #cbd5e1;
    background: rgba(15, 23, 42, 0.5);
    font-size: 0.77rem;
    font-weight: 650;
    transition: border-color 160ms ease, color 160ms ease, transform 160ms ease;
}
.cc-chip:hover { border-color: var(--cc-border-bright); color: #e2e8f0; transform: translateY(-1px); }

/* ---------- Stat bar ---------- */
.cc-stats {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-top: 30px;
    padding-top: 26px;
    border-top: 1px solid rgba(148, 163, 184, 0.12);
}
.cc-stat-value {
    font-family: var(--cc-display);
    font-size: 1.5rem;
    font-weight: 800;
    background: linear-gradient(90deg, #e0e7ff, #a5b4fc);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
}
.cc-stat-label { margin-top: 3px; color: var(--cc-faint); font-size: 0.76rem; font-weight: 600; letter-spacing: 0.02em; }

/* ---------- Glass panels ---------- */
.dashboard-grid { gap: 22px !important; align-items: stretch !important; }
.glass-panel {
    border: 1px solid var(--cc-border) !important;
    border-radius: 24px !important;
    background: var(--cc-surface) !important;
    box-shadow: 0 26px 64px rgba(0, 0, 0, 0.3), inset 0 1px rgba(255, 255, 255, 0.05) !important;
    backdrop-filter: blur(20px);
    transition: border-color 220ms ease, box-shadow 220ms ease, transform 220ms ease;
}
.glass-panel:hover {
    border-color: rgba(129, 140, 248, 0.26) !important;
    box-shadow: 0 32px 76px rgba(0, 0, 0, 0.4), inset 0 1px rgba(255, 255, 255, 0.07) !important;
}

.input-panel { padding: 28px !important; }
.report-panel { padding: 0 !important; overflow: hidden !important; }
.panel-heading { margin-bottom: 20px; }
.panel-eyebrow {
    margin-bottom: 8px;
    color: #a5b4fc;
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
}
.panel-heading h2 {
    margin: 0 0 7px;
    color: var(--cc-text);
    font-family: var(--cc-display);
    font-size: 1.38rem;
    letter-spacing: -0.02em;
}
.panel-heading p { margin: 0; color: var(--cc-muted); font-size: 0.88rem; line-height: 1.6; }

/* ---------- Inputs ---------- */
.input-panel .block {
    border-color: rgba(148, 163, 184, 0.14) !important;
    border-radius: 14px !important;
    background: rgba(2, 6, 23, 0.55) !important;
    transition: border-color 160ms ease, box-shadow 160ms ease;
}
.input-panel .block:focus-within {
    border-color: rgba(129, 140, 248, 0.6) !important;
    box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.18) !important;
}
.input-panel label span { color: #e2e8f0 !important; font-weight: 650 !important; }
.input-panel .info { color: #7f8da3 !important; }
.input-panel input,
.input-panel textarea { color: #f8fafc !important; background: transparent !important; }
.input-panel input::placeholder,
.input-panel textarea::placeholder { color: #58657a !important; }

/* ---------- Primary button with shimmer ---------- */
#strategy-button {
    position: relative;
    overflow: hidden;
    min-height: 54px !important;
    margin-top: 10px !important;
    border: 1px solid rgba(199, 210, 254, 0.32) !important;
    border-radius: 15px !important;
    color: white !important;
    background: linear-gradient(105deg, #4f46e5 0%, #7c3aed 52%, #0284c7 100%) !important;
    box-shadow: 0 16px 36px rgba(79, 70, 229, 0.36) !important;
    font-size: 1rem !important;
    font-weight: 750 !important;
    letter-spacing: 0.01em !important;
    transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease !important;
}
#strategy-button::after {
    content: "";
    position: absolute;
    top: 0;
    left: -120%;
    width: 60%;
    height: 100%;
    background: linear-gradient(100deg, transparent, rgba(255, 255, 255, 0.35), transparent);
    transform: skewX(-18deg);
    transition: left 620ms ease;
}
#strategy-button:hover {
    filter: brightness(1.08) !important;
    transform: translateY(-2px) !important;
    box-shadow: 0 22px 44px rgba(79, 70, 229, 0.5) !important;
}
#strategy-button:hover::after { left: 130%; }
#strategy-button:active { transform: translateY(0) !important; }

.security-note { display: flex; align-items: center; gap: 8px; margin-top: 14px; color: #6b7689; font-size: 0.74rem; }
.security-dot { width: 7px; height: 7px; border-radius: 50%; background: #34d399; box-shadow: 0 0 12px #34d399; animation: cc-pulse 2.4s ease-in-out infinite; }
@keyframes cc-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

/* ---------- Report ---------- */
.report-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 24px 30px 20px;
    border-bottom: 1px solid var(--cc-border);
    background: rgba(15, 23, 42, 0.7);
}
.report-title-wrap h2 { margin: 0 0 5px; color: white; font-family: var(--cc-display); font-size: 1.32rem; }
.report-title-wrap p { margin: 0; color: var(--cc-muted); font-size: 0.84rem; }
.report-status {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 7px 11px;
    border: 1px solid rgba(34, 211, 238, 0.2);
    border-radius: 999px;
    color: #a5f3fc;
    background: rgba(8, 145, 178, 0.1);
    font-size: 0.72rem;
    font-weight: 700;
    white-space: nowrap;
}
.report-status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--cc-cyan); box-shadow: 0 0 10px var(--cc-cyan); animation: cc-pulse 2.4s ease-in-out infinite; }

.report-output {
    min-height: 660px;
    padding: 30px 34px !important;
    border: 0 !important;
    background: linear-gradient(180deg, rgba(7, 12, 27, 0.74), rgba(4, 8, 18, 0.9)) !important;
}
.report-output h1,
.report-output h2,
.report-output h3 { color: #f8fafc !important; font-family: var(--cc-display); letter-spacing: -0.02em; }
.report-output h2 {
    margin-top: 1.9rem !important;
    padding-bottom: 0.6rem;
    border-bottom: 1px solid rgba(129, 140, 248, 0.22);
    color: #c7d2fe !important;
}
.report-output p,
.report-output li { color: #cbd5e1 !important; line-height: 1.74 !important; }
.report-output strong { color: #e7ecf6 !important; }
.report-output a { color: #93c5fd !important; }
.report-output blockquote {
    margin: 1rem 0;
    padding: 0.6rem 1rem;
    border-left: 3px solid var(--cc-accent);
    border-radius: 0 10px 10px 0;
    background: rgba(99, 102, 241, 0.08);
    color: #c7d2fe !important;
}
.report-output code {
    padding: 0.12rem 0.4rem;
    border-radius: 6px;
    background: rgba(129, 140, 248, 0.14);
    color: #e0e7ff !important;
    font-size: 0.86em;
}
.report-output table {
    width: 100%;
    overflow: hidden;
    border: 1px solid var(--cc-border) !important;
    border-radius: 12px;
    background: rgba(15, 23, 42, 0.55) !important;
}
.report-output th { color: #e0e7ff !important; background: rgba(79, 70, 229, 0.18) !important; }
.report-output td { color: #cbd5e1 !important; border-color: rgba(148, 163, 184, 0.1) !important; }
.report-output tr:hover td { background: rgba(99, 102, 241, 0.06) !important; }

.empty-report { display: grid; place-items: center; min-height: 540px; text-align: center; }
.empty-report-inner { max-width: 440px; }
.empty-report-icon {
    display: grid;
    place-items: center;
    width: 66px;
    height: 66px;
    margin: 0 auto 20px;
    border: 1px solid rgba(129, 140, 248, 0.3);
    border-radius: 20px;
    color: #a5b4fc;
    font-family: var(--cc-display);
    font-weight: 800;
    background: linear-gradient(145deg, rgba(79, 70, 229, 0.2), rgba(14, 165, 233, 0.08));
    box-shadow: inset 0 1px rgba(255, 255, 255, 0.08);
    font-size: 1.4rem;
    animation: cc-float 5s ease-in-out infinite;
}
@keyframes cc-float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-7px); } }
.empty-report h3 { margin: 0 0 8px; color: #e2e8f0; font-family: var(--cc-display); font-size: 1.16rem; }
.empty-report p { margin: 0; color: #7f8da3 !important; font-size: 0.9rem; line-height: 1.65 !important; }

/* ---------- Sections ---------- */
.section-label { margin: 38px 0 16px; }
.section-label h2 { margin: 0 0 5px; color: white; font-family: var(--cc-display); font-size: 1.22rem; }
.section-label p { margin: 0; color: var(--cc-muted); font-size: 0.84rem; }

.workflow-strip {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    overflow: hidden;
    border: 1px solid var(--cc-border);
    border-radius: 20px;
    background: var(--cc-border);
}
.workflow-step {
    position: relative;
    min-height: 128px;
    padding: 22px;
    background: rgba(10, 16, 32, 0.96);
    transition: background 200ms ease;
}
.workflow-step:hover { background: rgba(17, 26, 48, 0.98); }
.step-number {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    margin-bottom: 15px;
    border: 1px solid rgba(129, 140, 248, 0.32);
    border-radius: 10px;
    color: #c7d2fe;
    background: rgba(79, 70, 229, 0.16);
    font-size: 0.74rem;
    font-weight: 800;
}
.workflow-step strong { display: block; margin-bottom: 6px; color: #e2e8f0; font-size: 0.88rem; }
.workflow-step span { color: #77859a; font-size: 0.77rem; line-height: 1.5; }

.architecture-rail {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 14px;
    padding: 16px 18px;
    border: 1px solid rgba(148, 163, 184, 0.1);
    border-radius: 16px;
    color: #718096;
    background: rgba(8, 12, 24, 0.55);
    font-size: 0.75rem;
}
.arch-label { color: #a5b4fc; font-weight: 750; text-transform: uppercase; letter-spacing: 0.09em; }
.arch-node {
    padding: 5px 12px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    border-radius: 999px;
    color: #b6c1d2;
    background: rgba(15, 23, 42, 0.6);
    font-weight: 600;
}
.arch-arrow { color: #4f5f75; }

.examples-card {
    padding: 22px 24px !important;
    border: 1px solid var(--cc-border) !important;
    border-radius: 20px !important;
    background: rgba(10, 16, 32, 0.8) !important;
}
.examples-card table { border-radius: 12px !important; overflow: hidden !important; }
.examples-card th { color: #c7d2fe !important; background: rgba(79, 70, 229, 0.14) !important; }
.examples-card td { color: #b8c2d1 !important; background: rgba(15, 23, 42, 0.62) !important; }
.examples-card tr:hover td { background: rgba(99, 102, 241, 0.08) !important; cursor: pointer; }

/* ---------- Responsive ---------- */
@media (max-width: 980px) {
    .dashboard-grid { flex-direction: column !important; }
    .report-output { min-height: 540px; }
    .workflow-strip { grid-template-columns: repeat(2, 1fr); }
    .cc-stats { grid-template-columns: repeat(2, 1fr); }
}

@media (max-width: 640px) {
    .gradio-container { padding: 14px 12px 36px !important; }
    .cc-hero { padding: 28px 22px; border-radius: 22px; }
    .cc-brand { align-items: flex-start; flex-wrap: wrap; }
    .cc-hero h1 { font-size: 2.55rem; }
    .cc-stats { grid-template-columns: 1fr 1fr; gap: 12px; }
    .input-panel { padding: 20px !important; }
    .report-header { align-items: flex-start; padding: 20px; }
    .report-output { min-height: 460px; padding: 22px 20px !important; }
    .workflow-strip { grid-template-columns: 1fr; }
    .workflow-step { min-height: auto; }
    .arch-arrow { transform: rotate(90deg); }
}
"""

HERO_HTML = """
<section class="cc-hero">
  <div class="cc-brand">
    <div class="cc-logo" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
        <path d="M7 15.5a4.5 4.5 0 0 1 .9-8.91A6 6 0 0 1 19.5 9a3.5 3.5 0 0 1-.5 6.96"/>
        <path d="M8 19h8M10 16.5 8 19l2 2.5M14 16.5l2 2.5-2 2.5"/>
      </svg>
    </div>
    <span class="cc-wordmark">ClearCast</span>
    <span class="cc-kicker">Campaign Intelligence</span>
  </div>
  <h1>Turn forecasts into <span class="cc-gradient-text">campaign advantage.</span></h1>
  <p class="cc-subtitle">Weather-aware campaign timing powered by MCP, LangGraph, and live forecasts.</p>
  <p class="cc-description">ClearCast analyzes upcoming conditions through a marketing lens, then recommends the moments, messages, and mitigations most likely to move your audience.</p>
  <div class="cc-chips">
    <span class="cc-chip">Live weather context</span>
    <span class="cc-chip">Agentic tool calling</span>
    <span class="cc-chip">Ready-to-use ad copy</span>
  </div>
  <div class="cc-stats">
    <div><div class="cc-stat-value">4</div><div class="cc-stat-label">MCP weather tools</div></div>
    <div><div class="cc-stat-value">5-day</div><div class="cc-stat-label">Forecast horizon</div></div>
    <div><div class="cc-stat-value">LangGraph</div><div class="cc-stat-label">Agentic reasoning</div></div>
    <div><div class="cc-stat-value">Live</div><div class="cc-stat-label">OpenWeatherMap data</div></div>
  </div>
</section>
"""

WORKFLOW_HTML = """
<div class="workflow-strip">
  <div class="workflow-step"><div class="step-number">01</div><strong>Read location</strong><span>Resolve the market and campaign context.</span></div>
  <div class="workflow-step"><div class="step-number">02</div><strong>Call MCP weather tools</strong><span>Retrieve live conditions and forecast blocks.</span></div>
  <div class="workflow-step"><div class="step-number">03</div><strong>Reason over forecast</strong><span>LangGraph identifies high-value moments.</span></div>
  <div class="workflow-step"><div class="step-number">04</div><strong>Generate strategy</strong><span>Deliver timing, rationale, copy, and risks.</span></div>
</div>
"""

ARCHITECTURE_HTML = """
<div class="architecture-rail">
  <span class="arch-label">Architecture</span>
  <span class="arch-node">OpenWeatherMap API</span><span class="arch-arrow">&rarr;</span>
  <span class="arch-node">MCP Server</span><span class="arch-arrow">&rarr;</span>
  <span class="arch-node">LangGraph Agent</span><span class="arch-arrow">&rarr;</span>
  <span class="arch-node">Gradio Frontend</span>
</div>
"""

EMPTY_REPORT_HTML = """
<div class="empty-report">
  <div class="empty-report-inner">
    <div class="empty-report-icon">CC</div>
    <h3>Your strategy will appear here</h3>
    <p>Complete the campaign brief and generate a weather-aware plan built for your market.</p>
  </div>
</div>
"""


with gr.Blocks(title="ClearCast", fill_width=True) as app:
    gr.HTML(HERO_HTML)

    with gr.Row(elem_classes=["dashboard-grid"]):
        with gr.Column(
            scale=5,
            min_width=330,
            elem_classes=["glass-panel", "input-panel"],
        ):
            gr.HTML(
                """
                <div class="panel-heading">
                  <div class="panel-eyebrow">Campaign Brief</div>
                  <h2>Plan your next weather moment</h2>
                  <p>Define the market and objective. ClearCast will handle the forecast intelligence.</p>
                </div>
                """
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
                choices=["Friendly", "Urgent", "Playful", "Premium"],
                value="Friendly",
                info="Choose the voice for the suggested campaign copy.",
            )
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
                  Live forecast data is used only to generate this strategy.
                </div>
                """
            )

        with gr.Column(
            scale=8,
            min_width=520,
            elem_classes=["glass-panel", "report-panel"],
        ):
            gr.HTML(
                """
                <div class="report-header">
                  <div class="report-title-wrap">
                    <h2>Campaign Strategy Report</h2>
                    <p>Recommended windows, weather reasoning, ad copy, and risk notes.</p>
                  </div>
                  <span class="report-status">Agent ready</span>
                </div>
                """
            )
            output = gr.Markdown(
                value=EMPTY_REPORT_HTML,
                show_label=False,
                elem_classes=["report-output"],
            )

    gr.HTML(
        """
        <div class="section-label">
          <h2>How it works</h2>
          <p>From location to campaign-ready recommendation in four agentic steps.</p>
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

    submit_btn.click(
        fn=process_request,
        inputs=[location, business_type, campaign_goal, tone],
        outputs=[output],
    )


if __name__ == "__main__":
    # Gradio 6 expects theme and css at launch rather than Blocks creation.
    app.launch(inbrowser=True, theme=CLEARCAST_THEME, css=CLEARCAST_CSS)
