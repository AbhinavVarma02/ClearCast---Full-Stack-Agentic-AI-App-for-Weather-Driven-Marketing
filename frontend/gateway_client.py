"""HTTP client the Gradio app uses to reach the Node.js gateway.

The gateway is the only path from the UI to campaign generation in normal use;
the UI never imports or invokes the agent directly.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"
TOKEN_HEADER = "x-clearcast-client-token"  # noqa: S105 - header name, not a secret
# Longer than the gateway's upstream timeout so the gateway reports timeouts itself.
TIMEOUT = httpx.Timeout(200.0, connect=5.0)


class GatewayError(Exception):
    """A gateway response or transport failure with a client-safe message."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        retryable: bool = False,
        details: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retryable = retryable
        self.details = details or []


@dataclass
class GatewayClient:
    base_url: str
    token: str
    transport: httpx.BaseTransport | None = None

    @classmethod
    def from_env(cls) -> GatewayClient:
        return cls(
            base_url=(os.getenv("CLEARCAST_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/"),
            token=os.getenv("CLEARCAST_GATEWAY_TOKEN", ""),
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=TIMEOUT, transport=self.transport)

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        request_id = f"ui-{uuid.uuid4().hex[:24]}"
        headers = {TOKEN_HEADER: self.token, "x-request-id": request_id}
        try:
            with self._client() as client:
                response = client.request(method, path, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise GatewayError(
                504,
                "gateway_timeout",
                "The campaign service took too long to respond.",
                request_id=request_id,
                retryable=True,
            ) from None
        except httpx.TransportError:
            raise GatewayError(
                503,
                "gateway_unavailable",
                "The campaign gateway is not reachable. It may still be starting.",
                request_id=request_id,
                retryable=True,
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            raise GatewayError(
                response.status_code,
                str(error.get("code", "error")),
                str(error.get("message", "The request failed.")),
                request_id=error.get("request_id") or response.headers.get("x-request-id") or request_id,
                retryable=bool(error.get("retryable")),
                details=error.get("details") if isinstance(error.get("details"), list) else [],
            )
        raise GatewayError(
            response.status_code,
            "unexpected_response",
            "The campaign gateway returned an unexpected response.",
            request_id=request_id,
        )

    def create_plan(self, payload: dict) -> dict[str, Any]:
        return self._request("POST", "/v1/campaign-plans", payload)

    def review(self, request_id: str, payload: dict) -> dict[str, Any]:
        return self._request("POST", f"/v1/campaign-plans/{request_id}/review", payload)

    def revise(self, request_id: str, payload: dict) -> dict[str, Any]:
        return self._request("POST", f"/v1/campaign-plans/{request_id}/revisions", payload)

    def readiness(self) -> tuple[bool, dict]:
        """Return (ready, body) from the gateway's readiness endpoint."""
        try:
            with self._client() as client:
                response = client.get("/ready", timeout=5.0)
            body = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError):
            return False, {"status": "gateway_unreachable"}
        return response.status_code == 200, body if isinstance(body, dict) else {}
