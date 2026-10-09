"""Language-model construction and structured plan drafting."""

from __future__ import annotations

import os
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from agent.schemas import CampaignDraft

DEFAULT_MODEL = "gpt-4o-mini"
MODEL_TIMEOUT_SECONDS = 45.0
# The OpenAI SDK retries 408/409/429/5xx and connection errors with
# exponential backoff; two retries bound the worst case per call.
MODEL_MAX_RETRIES = 2
DRAFTING_TEMPERATURE = 0.2


def model_name() -> str:
    return (os.getenv("CLEARCAST_OPENAI_MODEL") or DEFAULT_MODEL).strip()


def create_chat_model(*, temperature: float | None = None, **kwargs) -> ChatOpenAI:
    """Create the GPT-4o-mini client with explicit timeouts and bounded retries."""
    options = {
        "model": model_name(),
        "timeout": MODEL_TIMEOUT_SECONDS,
        "max_retries": MODEL_MAX_RETRIES,
        **kwargs,
    }
    if temperature is not None:
        options["temperature"] = temperature
    return ChatOpenAI(**options)


@dataclass
class DraftAttempt:
    parsed: CampaignDraft | None
    raw_text: str
    parse_error: str | None
    usage: dict | None


class PlanDrafter:
    """Request a ``CampaignDraft`` with OpenAI strict JSON-schema structured output.

    The schema is sent as a plain ``response_format`` and the reply is parsed
    here. (Passing the Pydantic class would let the OpenAI SDK parse and raise
    before the bounded repair loop could see the malformed output.)
    """

    def __init__(self, llm) -> None:
        self._runnable = llm.bind(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "CampaignDraft", "strict": True, "schema": CampaignDraft.model_json_schema()},
            }
        )

    async def draft(self, messages: list[BaseMessage]) -> DraftAttempt:
        raw = await self._runnable.ainvoke(messages)
        raw_text = raw.content if isinstance(raw, AIMessage) and isinstance(raw.content, str) else ""
        usage = raw.usage_metadata if isinstance(raw, AIMessage) else None
        try:
            parsed, error = CampaignDraft.model_validate_json(raw_text), None
        except ValidationError as exc:
            parsed, error = None, _summarise_parse_error(exc)
        return DraftAttempt(parsed=parsed, raw_text=raw_text, parse_error=error, usage=dict(usage) if usage else None)


def _summarise_parse_error(error: ValidationError) -> str:
    """Return a short, content-free description of a structured-output failure."""
    details = error.errors()
    if any(item.get("type") == "json_invalid" for item in details):
        return "Output was not valid JSON"
    locations = ["/".join(str(part) for part in item.get("loc", ())) or "root" for item in details[:5]]
    return f"Output did not match the CampaignDraft schema at: {', '.join(locations)}"
