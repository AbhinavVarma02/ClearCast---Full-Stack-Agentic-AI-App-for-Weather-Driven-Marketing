"""Internal FastAPI orchestration service (loopback only).

Only the Node.js gateway calls this service. It binds to 127.0.0.1, requires
the shared internal token on every non-health route, disables interactive
docs, and returns a consistent error envelope that never contains secrets.

Run with ``python -m api.main``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent.errors import ClearCastError
from agent.observability import configure_langsmith, configure_logging, redact
from agent.runtime import AgentRuntime
from agent.schemas import (
    CampaignPlanRequest,
    CampaignPlanResponse,
    ErrorBody,
    ErrorDetail,
    ErrorResponse,
    ReviewRequest,
    RevisionRequest,
)
from agent.service import CampaignPlanningService

logger = logging.getLogger("clearcast.api")

TOKEN_HEADER = "x-clearcast-internal-token"  # noqa: S105 - header name, not a secret
REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PLAN_ID_PATH = Path(pattern=r"^[A-Za-z0-9._-]{8,64}$")


def _request_id(request: Request) -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    return supplied if REQUEST_ID_RE.match(supplied) else uuid.uuid4().hex


def _error_response(
    status: int, code: str, message: str, request_id: str | None, *, retryable: bool = False, details=None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=redact(message), request_id=request_id, retryable=retryable, details=details or []
        )
    )
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def create_app(service: CampaignPlanningService | None = None, *, internal_token: str | None = None) -> FastAPI:
    token = internal_token if internal_token is not None else os.getenv("CLEARCAST_INTERNAL_TOKEN", "")
    if not token:
        raise RuntimeError("CLEARCAST_INTERNAL_TOKEN must be set for the internal API.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        tracing = configure_langsmith()
        svc = service or CampaignPlanningService(AgentRuntime())
        app.state.service = svc
        logger.info("internal api starting", extra={"event": "api.start", **tracing})
        # Start the MCP subprocess and discover tools in the background so the
        # process answers health checks immediately; no provider calls happen here.
        warm_up = asyncio.create_task(svc.runtime.warm_up())
        try:
            yield
        finally:
            warm_up.cancel()
            await asyncio.gather(warm_up, return_exceptions=True)
            await svc.aclose()
            logger.info("internal api stopped", extra={"event": "api.stop"})

    app = FastAPI(
        title="ClearCast internal orchestration API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def verify_token(x_clearcast_internal_token: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_clearcast_internal_token.encode(), token.encode()):
            raise ClearCastError("Missing or invalid internal token.", code="unauthorized", http_status=401)

    def get_service(request: Request) -> CampaignPlanningService:
        return request.app.state.service

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            ErrorDetail(
                path="/".join(str(part) for part in error.get("loc", ()) if part != "body"),
                message=str(error.get("msg", "invalid value"))[:200],
            )
            for error in exc.errors()[:20]
        ]
        return _error_response(
            422, "invalid_request", "The request payload is invalid.", _request_id(request), details=details
        )

    @app.exception_handler(ClearCastError)
    async def _clearcast_error(request: Request, exc: ClearCastError) -> JSONResponse:
        return _error_response(exc.http_status, exc.code, exc.message, _request_id(request), retryable=exc.retryable)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.error(
            "unhandled error", extra={"event": "api.error", "request_id": request_id, "error_type": type(exc).__name__}
        )
        return _error_response(500, "internal_error", "An unexpected error occurred.", request_id)

    @app.get("/internal/health")
    async def health() -> dict:
        return {"status": "ok", "service": "clearcast-orchestrator"}

    @app.get("/internal/ready", dependencies=[Depends(verify_token)])
    async def ready(svc: CampaignPlanningService = Depends(get_service)) -> JSONResponse:
        readiness = svc.runtime.readiness()
        return JSONResponse(status_code=200 if readiness["ready"] else 503, content=readiness)

    @app.post(
        "/internal/v1/campaign-plans",
        response_model=CampaignPlanResponse,
        dependencies=[Depends(verify_token)],
    )
    async def create_plan(
        body: CampaignPlanRequest, request: Request, svc: CampaignPlanningService = Depends(get_service)
    ) -> JSONResponse:
        request_id = _request_id(request)
        response = await svc.create_plan(body, request_id=request_id)
        return JSONResponse(content=response.model_dump(mode="json"), headers={REQUEST_ID_HEADER: request_id})

    @app.post(
        "/internal/v1/campaign-plans/{plan_request_id}/review",
        response_model=CampaignPlanResponse,
        dependencies=[Depends(verify_token)],
    )
    async def review_plan(
        body: ReviewRequest,
        request: Request,
        plan_request_id: str = PLAN_ID_PATH,
        svc: CampaignPlanningService = Depends(get_service),
    ) -> JSONResponse:
        response = await svc.review(plan_request_id, body)
        return JSONResponse(content=response.model_dump(mode="json"), headers={REQUEST_ID_HEADER: _request_id(request)})

    @app.post(
        "/internal/v1/campaign-plans/{plan_request_id}/revisions",
        response_model=CampaignPlanResponse,
        dependencies=[Depends(verify_token)],
    )
    async def revise_plan(
        body: RevisionRequest,
        request: Request,
        plan_request_id: str = PLAN_ID_PATH,
        svc: CampaignPlanningService = Depends(get_service),
    ) -> JSONResponse:
        response = await svc.revise(plan_request_id, body)
        return JSONResponse(content=response.model_dump(mode="json"), headers={REQUEST_ID_HEADER: _request_id(request)})

    return app


def main() -> None:
    import uvicorn

    host = os.getenv("CLEARCAST_API_HOST", "127.0.0.1")
    if host not in LOOPBACK_HOSTS:
        raise SystemExit("The internal API must bind to a loopback address.")
    configure_logging()
    uvicorn.run(
        create_app(),
        host=host,
        port=int(os.getenv("CLEARCAST_API_PORT", "8001")),
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
