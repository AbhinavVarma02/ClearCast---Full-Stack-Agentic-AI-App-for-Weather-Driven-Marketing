"""Application-level observability: structured logs and per-request metrics.

Logs carry safe metadata only (identifiers, counts, durations, categories).
Prompts, campaign text, tool payloads, and secrets are never logged. Token
usage comes from provider metadata; cost is estimated only when per-token
prices are configured explicitly, because hard-coded prices go stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent.schemas import Diagnostics, TokenUsage

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "color_message"}
SECRET_PATTERNS = [
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}"), "sk-...[redacted]"),
    (re.compile(r"hf_[A-Za-z0-9]{8,}"), "hf_...[redacted]"),
    (re.compile(r"lsv2_[A-Za-z0-9_]{8,}"), "lsv2_...[redacted]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"), "gh_...[redacted]"),
    (re.compile(r"(?i)(appid=)[^&\s]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(api[_-]?key=)[^&\s]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}"), r"\1[redacted]"),
]


def redact(text: str) -> str:
    """Remove credential-shaped substrings from text before it is displayed or logged."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def session_ref(session_id: str) -> str:
    """Short, non-reversible correlation reference for a session identifier."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            # Tracebacks are not logged (they can carry request data); a redacted message is.
            payload["error_message"] = redact(str(record.exc_info[1]))[:300]
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Send JSON logs to stdout. Library loggers that print URLs stay at WARNING."""
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel((level or os.getenv("CLEARCAST_LOG_LEVEL") or "INFO").upper())
    for noisy in ("httpx", "httpcore", "openai", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def configure_langsmith() -> dict:
    """Enable optional LangSmith tracing only when it is requested and credentials exist."""
    key = (os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY") or "").strip()
    requested = any(
        (os.getenv(name) or "").strip().lower() == "true" for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")
    )
    enabled = requested and bool(key)
    if requested and not key:
        # Tracing without a key only produces export errors; turn it off explicitly.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
    if enabled:
        os.environ.setdefault("LANGSMITH_PROJECT", os.getenv("LANGCHAIN_PROJECT") or "clearcast")
    return {"langsmith_tracing": enabled}


def _price(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


@dataclass
class RequestMetrics:
    request_id: str
    session_ref: str
    client_id: str
    model: str
    started: float = field(default_factory=time.perf_counter)
    graph_seconds: float = 0.0
    drafting_seconds: float = 0.0
    llm_calls: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    provider_error_categories: list[str] = field(default_factory=list)
    cache_hits: int = 0
    repair_attempts: int = 0
    repair_reasons: list[str] = field(default_factory=list)
    step_limit_reached: bool = False
    _input_tokens: int = 0
    _output_tokens: int = 0
    _total_tokens: int = 0
    _with_usage: int = 0
    _without_usage: int = 0

    def record_usage(self, usage: dict | None) -> None:
        self.llm_calls += 1
        if not usage:
            self._without_usage += 1
            return
        self._with_usage += 1
        self._input_tokens += int(usage.get("input_tokens") or 0)
        self._output_tokens += int(usage.get("output_tokens") or 0)
        self._total_tokens += int(usage.get("total_tokens") or 0)

    def record_turn(self, turn: list[BaseMessage], categories: list[str], cache_hits: int) -> None:
        for message in turn:
            if isinstance(message, AIMessage):
                self.record_usage(message.usage_metadata)
                self.tool_calls.update(call["name"] for call in message.tool_calls)
            elif isinstance(message, ToolMessage) and message.status == "error":
                self.tool_errors += 1
        self.provider_error_categories.extend(categories)
        self.cache_hits += cache_hits

    def token_usage(self) -> TokenUsage | None:
        if not self._with_usage:
            return None
        return TokenUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._total_tokens,
            calls_with_usage=self._with_usage,
            calls_without_usage=self._without_usage,
        )

    def cost(self) -> tuple[float | None, str]:
        usage = self.token_usage()
        input_price = _price("CLEARCAST_MODEL_INPUT_USD_PER_1M_TOKENS")
        output_price = _price("CLEARCAST_MODEL_OUTPUT_USD_PER_1M_TOKENS")
        if usage is None:
            return None, "Not calculated: the provider reported no token usage."
        if input_price is None or output_price is None:
            return None, "Not calculated: model prices are not configured."
        if usage.calls_without_usage:
            return None, "Not calculated: some model calls reported no token usage."
        cost = (usage.input_tokens * input_price + usage.output_tokens * output_price) / 1_000_000
        return round(cost, 6), "Estimate from reported tokens and configured per-token prices."

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def diagnostics(self, *, validation_status: str, review_status: str, recursion_limit: int) -> Diagnostics:
        cost, note = self.cost()
        return Diagnostics(
            request_id=self.request_id,
            session_ref=self.session_ref,
            client_id=self.client_id,
            model=self.model,
            duration_ms=self.elapsed_ms(),
            graph_duration_ms=int(self.graph_seconds * 1000),
            drafting_duration_ms=int(self.drafting_seconds * 1000),
            llm_calls=self.llm_calls,
            tool_calls=dict(self.tool_calls),
            tool_errors=self.tool_errors,
            provider_error_categories=sorted(set(self.provider_error_categories)),
            cache_hits=self.cache_hits,
            repair_attempts=self.repair_attempts,
            repair_reasons=list(self.repair_reasons),
            recursion_limit=recursion_limit,
            step_limit_reached=self.step_limit_reached,
            validation_status=validation_status,
            review_status=review_status,
            token_usage=self.token_usage(),
            estimated_cost_usd=cost,
            cost_note=note,
        )

    def log_fields(self) -> dict:
        usage = self.token_usage()
        return {
            "request_id": self.request_id,
            "session_ref": self.session_ref,
            "client_id": self.client_id,
            "model": self.model,
            "duration_ms": self.elapsed_ms(),
            "graph_duration_ms": int(self.graph_seconds * 1000),
            "llm_calls": self.llm_calls,
            "tool_calls": dict(self.tool_calls),
            "tool_errors": self.tool_errors,
            "provider_error_categories": sorted(set(self.provider_error_categories)),
            "cache_hits": self.cache_hits,
            "repair_attempts": self.repair_attempts,
            "repair_reasons": list(self.repair_reasons),
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
        }
