"""Regenerate contracts/fixtures/sample_plan_response.json from a deterministic offline run.

Usage: python -m evaluation.make_sample_response
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.schemas import CampaignPlanRequest
from evaluation.fake_openai import ScriptedOpenAI
from evaluation.harness import build_service, install_fixture_weather

OUTPUT = Path(__file__).resolve().parents[1] / "contracts" / "fixtures" / "sample_plan_response.json"


async def generate() -> dict:
    install_fixture_weather("baseline_mild")
    service = build_service(ScriptedOpenAI())
    try:
        request = CampaignPlanRequest(
            session_id="sample_session_000000000",
            client_id="demo_outdoor_fitness",
            brief={
                "location": "Austin, TX",
                "business_type": "Outdoor fitness studio",
                "campaign_goal": "Promote outdoor class signups",
                "tone": "Urgent",
            },
        )
        response = await service.create_plan(request, request_id="sample-request-0001")
    finally:
        await service.aclose()
    payload = response.model_dump(mode="json")
    # Durations vary between runs; pin them so the fixture is stable in Git.
    diagnostics = payload["plan"]["diagnostics"]
    for key in ("duration_ms", "graph_duration_ms", "drafting_duration_ms"):
        diagnostics[key] = 0
    return payload


def main() -> None:
    payload = asyncio.run(generate())
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT} (status={payload['plan']['status']})")


if __name__ == "__main__":
    main()
