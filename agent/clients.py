"""Client profiles: validated configuration for one shared agent.

Profiles live in ``agent/config/client_profiles.json`` and are validated with
Pydantic when first loaded. Demo clients are fictional; they show how one
agentic application is configured per customer without copying the agent.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from agent.schemas import ClientConstraints, ClientProfile, ClientSnapshot

PROFILES_PATH = Path(__file__).resolve().parent / "config" / "client_profiles.json"


class _ProfileFile(BaseModel):
    profiles: list[ClientProfile]


@lru_cache(maxsize=1)
def load_profiles(path: Path = PROFILES_PATH) -> dict[str, ClientProfile]:
    """Load and validate every profile; invalid configuration fails fast."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("_comment", None)
    profiles = _ProfileFile.model_validate(raw).profiles
    by_id = {profile.client_id: profile for profile in profiles}
    if len(by_id) != len(profiles):
        raise ValueError("Duplicate client_id in client profile configuration")
    if "general" not in by_id:
        raise ValueError("Client profile configuration must define the 'general' profile")
    return by_id


def get_profile(client_id: str) -> ClientProfile:
    profiles = load_profiles()
    if client_id not in profiles:
        raise KeyError(f"Unknown client profile: {client_id}")
    return profiles[client_id]


def client_snapshot(profile: ClientProfile, override: ClientConstraints | None) -> ClientSnapshot:
    """Return the effective client configuration used for one request."""
    return ClientSnapshot(
        client_id=profile.client_id,
        display_name=profile.display_name,
        fictional=profile.fictional,
        business_objective=profile.business_objective,
        constraints=override if override is not None else profile.constraints,
        constraints_source="request_override" if override is not None else "profile_default",
        preferences=profile.preferences,
    )
