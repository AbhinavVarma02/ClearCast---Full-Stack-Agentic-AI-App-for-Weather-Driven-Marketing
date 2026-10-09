"""Error taxonomy shared by the agent service, the internal API, and the UI.

Every error carries a stable ``code``, an HTTP status for the internal API, and
a message that is safe to show to users (no keys, URLs, or prompt content).
"""

from __future__ import annotations

import asyncio

import httpx


class ClearCastError(Exception):
    """Base error with a stable code and a client-safe message."""

    code = "internal_error"
    http_status = 500
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable


class ServiceUnavailableError(ClearCastError):
    code = "service_unavailable"
    http_status = 503
    retryable = True


class ConfigurationError(ServiceUnavailableError):
    code = "configuration_missing"
    retryable = False


class ToolServiceUnavailableError(ServiceUnavailableError):
    """The MCP weather tool subprocess could not be started or reached."""

    code = "tool_service_unavailable"


class ModelProviderError(ClearCastError):
    code = "model_provider_error"
    http_status = 502


class SessionBusyError(ClearCastError):
    code = "session_busy"
    http_status = 429
    retryable = True


class PlanNotFoundError(ClearCastError):
    code = "plan_not_found"
    http_status = 404


class ReviewConflictError(ClearCastError):
    code = "review_conflict"
    http_status = 409


def classify_model_exception(exc: BaseException) -> ModelProviderError | None:
    """Map OpenAI/httpx/timeout failures to a categorised, client-safe error."""
    import openai

    if isinstance(exc, openai.APITimeoutError | httpx.TimeoutException | asyncio.TimeoutError):
        return ModelProviderError(
            "The language model did not respond in time.",
            code="model_timeout",
            http_status=504,
            retryable=True,
        )
    if isinstance(exc, openai.RateLimitError):
        return ModelProviderError(
            "The language model provider is rate limiting requests.",
            code="model_rate_limited",
            http_status=503,
            retryable=True,
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ModelProviderError(
            "The language model provider rejected the configured credentials.",
            code="model_auth_error",
        )
    if isinstance(exc, openai.APIConnectionError):
        return ModelProviderError(
            "The language model provider could not be reached.",
            code="model_unavailable",
            retryable=True,
        )
    if isinstance(exc, openai.BadRequestError):
        return ModelProviderError("The language model provider rejected the request.", code="model_bad_request")
    if isinstance(exc, openai.APIStatusError):
        return ModelProviderError(
            f"The language model provider returned an error (HTTP {exc.status_code}).",
            retryable=exc.status_code >= 500,
        )
    return None
