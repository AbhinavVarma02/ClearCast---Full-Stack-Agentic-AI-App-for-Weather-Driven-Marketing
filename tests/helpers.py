"""Helpers shared by tests."""

from __future__ import annotations

from pathlib import Path

from agent.schemas import CampaignPlanRequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENWEATHER_KEY = "fake-owm-key-used-only-in-tests"


def plan_request(
    *,
    session_id: str = "session_aaaaaaaaaaaaaaaa",
    client_id: str = "general",
    location: str = "Baltimore, MD",
    business_type: str = "Coffee shop",
    goal: str = "Increase morning visits",
    tone: str = "Friendly",
    constraints: dict | None = None,
) -> CampaignPlanRequest:
    return CampaignPlanRequest(
        session_id=session_id,
        client_id=client_id,
        brief={"location": location, "business_type": business_type, "campaign_goal": goal, "tone": tone},
        constraints=constraints,
    )
