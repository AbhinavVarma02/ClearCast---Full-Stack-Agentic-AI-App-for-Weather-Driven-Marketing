/**
 * ClearCast integration gateway (Node.js + TypeScript + Fastify).
 *
 * Responsibilities: the typed public campaign-planning contract, payload
 * validation, body limits, rate limits, request-id propagation, forwarding to
 * the internal Python orchestration service, safe error mapping, health and
 * readiness, and response-contract validation. Campaign logic, LLM calls,
 * weather access, evidence validation, and approval rules stay in Python.
 */
import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import rateLimit from "@fastify/rate-limit";
import { TypeBoxValidatorCompiler, type TypeBoxTypeProvider } from "@fastify/type-provider-typebox";
import Fastify, { type FastifyError, type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import type { GatewayConfig } from "./config.js";
import { createContractValidator, loadContract, type ContractValidator } from "./contracts.js";
import { envelope, isErrorEnvelope } from "./errors.js";
import {
  CampaignPlanRequest,
  ErrorEnvelope,
  PlanParams,
  ReviewRequest,
  RevisionRequest,
} from "./schemas.js";
import { HttpOrchestrator, type Orchestrator, type UpstreamOutcome } from "./upstream.js";

export const GATEWAY_VERSION = "1.0.0";
const REQUEST_ID_RE = /^[A-Za-z0-9._-]{8,64}$/;
const CLIENT_TOKEN_HEADER = "x-clearcast-client-token";
const PASSTHROUGH_STATUSES = new Set([404, 409, 422, 429, 502, 503, 504]);

export interface AppOptions {
  orchestrator?: Orchestrator;
  planValidator?: ContractValidator;
  logger?: boolean;
}

function digest(value: string): Buffer {
  return createHash("sha256").update(value).digest();
}

function stripInternalKeys(schema: unknown): unknown {
  // TypeBox adds bookkeeping keys such as "~kind"; publish plain JSON Schema.
  if (Array.isArray(schema)) {
    return schema.map(stripInternalKeys);
  }
  if (schema && typeof schema === "object") {
    return Object.fromEntries(
      Object.entries(schema).filter(([key]) => !key.startsWith("~")).map(([key, value]) => [key, stripInternalKeys(value)]),
    );
  }
  return schema;
}

export async function buildApp(config: GatewayConfig, options: AppOptions = {}): Promise<FastifyInstance> {
  const orchestrator =
    options.orchestrator ?? new HttpOrchestrator(config.upstreamUrl, config.upstreamToken, config.upstreamTimeoutMs);
  const planValidator = options.planValidator ?? createContractValidator("campaign_plan_response");
  const clientTokenDigest = digest(config.clientToken);
  let inflightPlans = 0;

  const app = Fastify({
    logger: options.logger === false ? false : { level: config.logLevel },
    bodyLimit: config.bodyLimitBytes,
    requestIdHeader: false,
    genReqId: (req) => {
      const supplied = req.headers["x-request-id"];
      return typeof supplied === "string" && REQUEST_ID_RE.test(supplied) ? supplied : randomUUID();
    },
    trustProxy: false,
    return503OnClosing: true,
  }).withTypeProvider<TypeBoxTypeProvider>();
  app.setValidatorCompiler(TypeBoxValidatorCompiler);
  // JSON-only API: anything else is rejected with 415 instead of being parsed.
  app.removeContentTypeParser("text/plain");

  await app.register(rateLimit, {
    global: true,
    max: config.globalRateLimitPerMinute,
    timeWindow: 60_000,
    errorResponseBuilder: (_request, context) =>
      Object.assign(new Error(`Too many requests; retry in ${Math.ceil(context.ttl / 1000)} seconds.`), {
        statusCode: 429,
        code: "RATE_LIMITED",
      }),
  });
  const sessionPlanLimiter = app.createRateLimit({
    max: config.planRateLimitPerMinute,
    timeWindow: 60_000,
    keyGenerator: (request: FastifyRequest) => {
      const body = request.body as { session_id?: unknown } | undefined;
      return `plan:${typeof body?.session_id === "string" ? body.session_id : request.ip}`;
    },
  });

  app.addHook("onSend", async (request, reply, payload) => {
    reply.header("x-request-id", request.id);
    reply.header("cache-control", "no-store");
    reply.header("x-content-type-options", "nosniff");
    return payload;
  });

  const requireClientToken = async (request: FastifyRequest, reply: FastifyReply) => {
    const supplied = request.headers[CLIENT_TOKEN_HEADER];
    const ok = typeof supplied === "string" && timingSafeEqual(digest(supplied), clientTokenDigest);
    if (!ok) {
      return reply.code(401).send(envelope("unauthorized", "Missing or invalid client token.", request.id));
    }
  };

  app.setErrorHandler((error: FastifyError, request, reply) => {
    if (error.validation) {
      const details = error.validation.slice(0, 20).map((issue) => ({
        path: issue.instancePath || "/",
        message: issue.message ?? "invalid value",
      }));
      return reply.code(400).send(envelope("invalid_request", "The request payload is invalid.", request.id, { details }));
    }
    if (error.statusCode === 429) {
      return reply.code(429).send(envelope("rate_limited", error.message, request.id, { retryable: true }));
    }
    if (error.code === "FST_ERR_CTP_BODY_TOO_LARGE") {
      return reply
        .code(413)
        .send(envelope("payload_too_large", `Request bodies are limited to ${config.bodyLimitBytes} bytes.`, request.id));
    }
    if (error.code === "FST_ERR_CTP_INVALID_MEDIA_TYPE") {
      return reply.code(415).send(envelope("unsupported_media_type", "Send application/json.", request.id));
    }
    if (error.statusCode !== undefined && error.statusCode >= 400 && error.statusCode < 500) {
      return reply.code(400).send(envelope("invalid_request", "The request could not be parsed.", request.id));
    }
    request.log.error({ event: "gateway.error", error_type: error.name }, "unhandled gateway error");
    return reply.code(500).send(envelope("gateway_error", "The gateway failed to process the request.", request.id));
  });

  app.setNotFoundHandler((request, reply) => {
    reply.code(404).send(envelope("not_found", "Route not found.", request.id));
  });

  const respond = (request: FastifyRequest, reply: FastifyReply, outcome: UpstreamOutcome, route: string) => {
    request.log.info(
      {
        event: "gateway.upstream",
        route,
        outcome: outcome.kind,
        upstream_status: "status" in outcome ? outcome.status : null,
        upstream_duration_ms: outcome.durationMs,
      },
      "upstream call finished",
    );
    switch (outcome.kind) {
      case "timeout":
        return reply
          .code(504)
          .send(envelope("upstream_timeout", "The orchestration service did not respond in time.", request.id, { retryable: true }));
      case "unreachable":
        return reply
          .code(503)
          .send(envelope("orchestrator_unavailable", "The orchestration service is unavailable.", request.id, { retryable: true }));
      case "invalid_json":
        return reply
          .code(502)
          .send(envelope("upstream_invalid_response", "The orchestration service returned an invalid response.", request.id));
      case "response":
        break;
    }
    const { status, body } = outcome;
    if (status === 200) {
      const check = planValidator.validate(body);
      if (!check.ok) {
        request.log.error({ event: "gateway.contract_violation", errors: check.errors }, "upstream contract violation");
        return reply
          .code(502)
          .send(
            envelope(
              "upstream_contract_violation",
              "The orchestration service returned a response that does not match the published contract.",
              request.id,
            ),
          );
      }
      return reply.code(200).send(body);
    }
    if (status === 401 || status === 403) {
      return reply
        .code(502)
        .send(envelope("gateway_misconfigured", "The gateway could not authenticate with the orchestration service.", request.id));
    }
    if (PASSTHROUGH_STATUSES.has(status) && isErrorEnvelope(body)) {
      const upstream = body.error;
      return reply.code(status).send(
        envelope(upstream.code, upstream.message, upstream.request_id ?? request.id, {
          retryable: upstream.retryable,
          details: Array.isArray(upstream.details) ? upstream.details.slice(0, 20) : [],
        }),
      );
    }
    return reply
      .code(502)
      .send(
        envelope("upstream_error", "The orchestration service failed to process the request.", request.id, {
          retryable: status >= 500,
        }),
      );
  };

  // -- operational endpoints --------------------------------------------------
  app.get("/health", { config: { rateLimit: false } }, (_request, reply) =>
    reply.send({ status: "ok", service: "clearcast-gateway", version: GATEWAY_VERSION }),
  );

  app.get("/ready", { config: { rateLimit: false } }, async (request, reply) => {
    const outcome = await orchestrator.request("GET", "/internal/ready", request.id, undefined, config.readyTimeoutMs);
    const orchestratorState = outcome.kind === "response" ? outcome.body : { status: outcome.kind };
    const ready = outcome.kind === "response" && outcome.status === 200;
    return reply.code(ready ? 200 : 503).send({
      status: ready ? "ready" : "not_ready",
      gateway: "ok",
      orchestrator: orchestratorState,
    });
  });

  const publishedSchemas = {
    campaign_plan_request: stripInternalKeys(CampaignPlanRequest),
    review_request: stripInternalKeys(ReviewRequest),
    revision_request: stripInternalKeys(RevisionRequest),
    error_envelope: stripInternalKeys(ErrorEnvelope),
    campaign_plan_response: loadContract("campaign_plan_response"),
  };
  app.get("/v1/schemas", { onRequest: requireClientToken }, (_request, reply) => reply.send(publishedSchemas));

  // -- campaign planning --------------------------------------------------------
  app.post(
    "/v1/campaign-plans",
    { onRequest: requireClientToken, schema: { body: CampaignPlanRequest } },
    async (request, reply) => {
      const limit = await sessionPlanLimiter(request);
      if (!limit.isAllowed && limit.isExceeded) {
        return reply
          .code(429)
          .header("retry-after", String(limit.ttlInSeconds))
          .send(
            envelope(
              "rate_limited",
              `This session reached ${limit.max} campaign plans per minute; retry in ${limit.ttlInSeconds} seconds.`,
              request.id,
              { retryable: true },
            ),
          );
      }
      if (inflightPlans >= config.maxInflightPlans) {
        return reply
          .code(429)
          .send(envelope("gateway_busy", "ClearCast is handling other requests; try again shortly.", request.id, { retryable: true }));
      }
      inflightPlans += 1;
      let outcome: UpstreamOutcome;
      try {
        outcome = await orchestrator.request("POST", "/internal/v1/campaign-plans", request.id, request.body);
      } finally {
        inflightPlans -= 1;
      }
      return respond(request, reply, outcome, "/v1/campaign-plans");
    },
  );

  app.post(
    "/v1/campaign-plans/:requestId/review",
    { onRequest: requireClientToken, schema: { params: PlanParams, body: ReviewRequest } },
    async (request, reply) => {
      const path = `/internal/v1/campaign-plans/${encodeURIComponent(request.params.requestId)}/review`;
      const outcome = await orchestrator.request("POST", path, request.id, request.body);
      return respond(request, reply, outcome, "/v1/campaign-plans/:requestId/review");
    },
  );

  app.post(
    "/v1/campaign-plans/:requestId/revisions",
    { onRequest: requireClientToken, schema: { params: PlanParams, body: RevisionRequest } },
    async (request, reply) => {
      const path = `/internal/v1/campaign-plans/${encodeURIComponent(request.params.requestId)}/revisions`;
      const outcome = await orchestrator.request("POST", path, request.id, request.body);
      return respond(request, reply, outcome, "/v1/campaign-plans/:requestId/revisions");
    },
  );

  return app;
}
