"""Generate the JSON Schema contracts shared with the Node.js gateway.

Run ``python -m api.export_contracts`` after changing ``agent/schemas.py``;
``python -m api.export_contracts --check`` fails if the committed files drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from agent.schemas import (
    CampaignPlanRequest,
    CampaignPlanResponse,
    ErrorResponse,
    ReviewRequest,
    RevisionRequest,
)

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"
CONTRACTS: dict[str, tuple[type[BaseModel], str]] = {
    "campaign_plan_request.schema.json": (CampaignPlanRequest, "validation"),
    "campaign_plan_response.schema.json": (CampaignPlanResponse, "serialization"),
    "review_request.schema.json": (ReviewRequest, "validation"),
    "revision_request.schema.json": (RevisionRequest, "validation"),
    "error_response.schema.json": (ErrorResponse, "serialization"),
}


def render(model: type[BaseModel], mode: str, name: str) -> str:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://clearcast.local/contracts/{name}",
        **model.model_json_schema(mode=mode),
    }
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    CONTRACTS_DIR.mkdir(exist_ok=True)
    drift = []
    for name, (model, mode) in CONTRACTS.items():
        path = CONTRACTS_DIR / name
        content = render(model, mode, name)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(name)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    if drift:
        print(f"Contract drift detected; run python -m api.export_contracts: {', '.join(drift)}")
        return 1
    print("Contracts are up to date." if check else f"Wrote {len(CONTRACTS)} contracts to {CONTRACTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
