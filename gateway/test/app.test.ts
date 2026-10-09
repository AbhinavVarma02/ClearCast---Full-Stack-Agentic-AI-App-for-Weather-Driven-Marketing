import type { FastifyInstance } from "fastify";
import { afterEach, describe, expect, it } from "vitest";
import { buildApp } from "../src/app.js";
import type { GatewayConfig } from "../src/config.js";
import type { UpstreamOutcome } from "../src/upstream.js";
import {
  authHeaders,
  CLIENT_TOKEN,
  contractCases,
  FakeOrchestrator,
  samplePlanResponse,
  testConfig,
  validBody,
} from "./helpers.js";

let app: FastifyInstance | undefined;

async function make(overrides: Partial<GatewayConfig> = {}, orchestrator = new FakeOrchestrator()) {
  app = await buildApp(testConfig(overrides), { orchestrator, logger: false });
  return { app, orchestrator };
}

afterEach(async () => {
  await app?.close();
  app = undefined;
});

const errorCode = (body: string): string => (JSON.parse(body) as { error: { code: string } }).error.code;

describe("health and readiness", () => {
  it("reports liveness without authentication", async () => {
    const { app } = await make();
    const res = await app.inject({ method: "GET", url: "/health" });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toMatchObject({ status: "ok", service: "clearcast-gateway" });
  });

  it("is ready only when the orchestrator is ready", async () => {
    const orchestrator = new FakeOrchestrator();
    const { app } = await make({}, orchestrator);
    orchestrator.next = () => ({ kind: "response", status: 200, body: { ready: true, tools: ["get_forecast"] }, durationMs: 1 });
    const ready = await app.inject({ method: "GET", url: "/ready" });
    expect(ready.statusCode).toBe(200);
    expect(orchestrator.calls[0]?.path).toBe("/internal/ready");

    orchestrator.next = () => ({ kind: "response", status: 503, body: { ready: false, status: "misconfigured" }, durationMs: 1 });
    expect((await app.inject({ method: "GET", url: "/ready" })).statusCode).toBe(503);

    orchestrator.next = () => ({ kind: "unreachable", durationMs: 1 });
    const down = await app.inject({ method: "GET", url: "/ready" });
    expect(down.statusCode).toBe(503);
    expect(down.json()).toMatchObject({ status: "not_ready", orchestrator: { status: "unreachable" } });
  });
});

describe("authentication", () => {
  it("rejects missing and wrong client tokens before forwarding", async () => {
    const { app, orchestrator } = await make();
    const missing = await app.inject({ method: "POST", url: "/v1/campaign-plans", payload: validBody });
    expect(missing.statusCode).toBe(401);
    expect(errorCode(missing.body)).toBe("unauthorized");
    const wrong = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: { ...authHeaders, "x-clearcast-client-token": `${CLIENT_TOKEN}x` },
      payload: validBody,
    });
    expect(wrong.statusCode).toBe(401);
    expect(orchestrator.calls).toHaveLength(0);
  });
});

describe("request contract (shared fixtures with the Python service)", () => {
  for (const testCase of contractCases) {
    it(`${testCase.name}: gateway_valid=${testCase.gateway_valid}`, async () => {
      const { app, orchestrator } = await make();
      const res = await app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: JSON.stringify(testCase.body) });
      if (testCase.gateway_valid) {
        expect(res.statusCode).toBe(200);
        expect(orchestrator.calls).toHaveLength(1);
        // The gateway forwards the validated payload unchanged.
        expect(orchestrator.calls[0]?.body).toEqual(testCase.body);
      } else {
        expect(res.statusCode).toBe(400);
        expect(errorCode(res.body)).toBe("invalid_request");
        expect(orchestrator.calls).toHaveLength(0);
      }
    });
  }
});

describe("payload handling", () => {
  it("enforces the body limit", async () => {
    const { app } = await make({ bodyLimitBytes: 1024 });
    const res = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: authHeaders,
      payload: JSON.stringify({ ...validBody, brief: { ...validBody.brief, campaign_goal: "x".repeat(2000) } }),
    });
    expect(res.statusCode).toBe(413);
    expect(errorCode(res.body)).toBe("payload_too_large");
  });

  it("rejects non-JSON content types and malformed JSON", async () => {
    const { app } = await make();
    const text = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: { ...authHeaders, "content-type": "text/plain" },
      payload: "hello",
    });
    expect(text.statusCode).toBe(415);
    const broken = await app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: "{not json" });
    expect(broken.statusCode).toBe(400);
    expect(errorCode(broken.body)).toBe("invalid_request");
  });

  it("propagates valid request ids and replaces unsafe ones", async () => {
    const { app, orchestrator } = await make();
    const kept = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: { ...authHeaders, "x-request-id": "req-abc-12345" },
      payload: validBody,
    });
    expect(kept.headers["x-request-id"]).toBe("req-abc-12345");
    expect(orchestrator.calls[0]?.requestId).toBe("req-abc-12345");

    const replaced = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: { ...authHeaders, "x-request-id": "bad id\nwith newline" },
      payload: validBody,
    });
    expect(replaced.headers["x-request-id"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(orchestrator.calls[1]?.requestId).toBe(replaced.headers["x-request-id"]);
  });

  it("returns unknown routes as an error envelope", async () => {
    const { app } = await make();
    const res = await app.inject({ method: "GET", url: "/v1/nope", headers: authHeaders });
    expect(res.statusCode).toBe(404);
    expect(errorCode(res.body)).toBe("not_found");
  });
});

describe("upstream error mapping", () => {
  const cases: { name: string; outcome: UpstreamOutcome; status: number; code: string }[] = [
    { name: "timeout", outcome: { kind: "timeout", durationMs: 1 }, status: 504, code: "upstream_timeout" },
    { name: "unreachable", outcome: { kind: "unreachable", durationMs: 1 }, status: 503, code: "orchestrator_unavailable" },
    { name: "invalid json", outcome: { kind: "invalid_json", status: 200, durationMs: 1 }, status: 502, code: "upstream_invalid_response" },
    {
      name: "upstream auth failure is not exposed",
      outcome: { kind: "response", status: 401, body: { error: { code: "unauthorized", message: "bad token" } }, durationMs: 1 },
      status: 502,
      code: "gateway_misconfigured",
    },
    {
      name: "unexpected 500",
      outcome: { kind: "response", status: 500, body: { detail: "Traceback ..." }, durationMs: 1 },
      status: 502,
      code: "upstream_error",
    },
    {
      name: "contract violation",
      outcome: { kind: "response", status: 200, body: { plan: { request_id: 1 } }, durationMs: 1 },
      status: 502,
      code: "upstream_contract_violation",
    },
  ];
  for (const testCase of cases) {
    it(testCase.name, async () => {
      const orchestrator = new FakeOrchestrator();
      orchestrator.next = () => testCase.outcome;
      const { app } = await make({}, orchestrator);
      const res = await app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: validBody });
      expect(res.statusCode).toBe(testCase.status);
      expect(errorCode(res.body)).toBe(testCase.code);
      expect(res.body).not.toContain("Traceback");
      expect(res.body).not.toContain("bad token");
    });
  }

  for (const status of [409, 422, 429, 502, 503, 504]) {
    it(`passes through upstream ${status} error envelopes`, async () => {
      const orchestrator = new FakeOrchestrator();
      orchestrator.next = () => ({
        kind: "response",
        status,
        body: { error: { code: `code_${status}`, message: "safe message", request_id: "req-upstream-1", retryable: true, details: [] } },
        durationMs: 1,
      });
      const { app } = await make({}, orchestrator);
      const res = await app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: validBody });
      expect(res.statusCode).toBe(status);
      expect(res.json()).toEqual({
        error: { code: `code_${status}`, message: "safe message", request_id: "req-upstream-1", retryable: true, details: [] },
      });
    });
  }

  it("returns a contract-valid plan unchanged", async () => {
    const { app } = await make();
    const res = await app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: validBody });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual(samplePlanResponse);
  });
});

describe("rate limiting and concurrency", () => {
  it("limits plan requests per session", async () => {
    const { app } = await make({ planRateLimitPerMinute: 2 });
    const send = (session: string) =>
      app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: { ...validBody, session_id: session } });
    expect((await send("sess_limit_aaaaaaaaaaaa")).statusCode).toBe(200);
    expect((await send("sess_limit_aaaaaaaaaaaa")).statusCode).toBe(200);
    const limited = await send("sess_limit_aaaaaaaaaaaa");
    expect(limited.statusCode).toBe(429);
    expect(errorCode(limited.body)).toBe("rate_limited");
    expect(limited.headers["retry-after"]).toBeDefined();
    // A different session has its own budget.
    expect((await send("sess_limit_bbbbbbbbbbbb")).statusCode).toBe(200);
  });

  it("applies the global limit to every route", async () => {
    const { app } = await make({ globalRateLimitPerMinute: 2 });
    const send = () => app.inject({ method: "GET", url: "/v1/schemas", headers: authHeaders });
    expect((await send()).statusCode).toBe(200);
    expect((await send()).statusCode).toBe(200);
    const limited = await send();
    expect(limited.statusCode).toBe(429);
    expect(errorCode(limited.body)).toBe("rate_limited");
    // Health checks are exempt so the supervisor can always probe the gateway.
    expect((await app.inject({ method: "GET", url: "/health" })).statusCode).toBe(200);
  });

  it("caps in-flight plan generations", async () => {
    const orchestrator = new FakeOrchestrator();
    let release: (value: UpstreamOutcome) => void = () => undefined;
    orchestrator.next = () =>
      new Promise<UpstreamOutcome>((resolve) => {
        release = resolve;
      });
    const { app } = await make({ maxInflightPlans: 1 }, orchestrator);
    const first = app.inject({ method: "POST", url: "/v1/campaign-plans", headers: authHeaders, payload: validBody });
    await new Promise((resolve) => setTimeout(resolve, 50));
    const second = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans",
      headers: authHeaders,
      payload: { ...validBody, session_id: "sess_other_session_00001" },
    });
    expect(second.statusCode).toBe(429);
    expect(errorCode(second.body)).toBe("gateway_busy");
    release({ kind: "response", status: 200, body: samplePlanResponse, durationMs: 1 });
    expect((await first).statusCode).toBe(200);
  });
});

describe("review and revision routes", () => {
  const hash = "a".repeat(64);

  it("forwards review decisions to the plan-specific internal route", async () => {
    const { app, orchestrator } = await make();
    const res = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans/req-12345678/review",
      headers: authHeaders,
      payload: { session_id: validBody.session_id, decision: "approve", plan_hash: hash },
    });
    expect(res.statusCode).toBe(200);
    expect(orchestrator.calls[0]?.path).toBe("/internal/v1/campaign-plans/req-12345678/review");
  });

  it("validates path parameters and decisions", async () => {
    const { app, orchestrator } = await make();
    const badId = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans/..%2F..%2Fadmin/review",
      headers: authHeaders,
      payload: { session_id: validBody.session_id, decision: "approve", plan_hash: hash },
    });
    expect(badId.statusCode).toBe(400);
    const badDecision = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans/req-12345678/review",
      headers: authHeaders,
      payload: { session_id: validBody.session_id, decision: "publish", plan_hash: hash },
    });
    expect(badDecision.statusCode).toBe(400);
    expect(orchestrator.calls).toHaveLength(0);
  });

  it("forwards ad-copy revisions", async () => {
    const { app, orchestrator } = await make();
    const res = await app.inject({
      method: "POST",
      url: "/v1/campaign-plans/req-12345678/revisions",
      headers: authHeaders,
      payload: { session_id: validBody.session_id, base_plan_hash: hash, ad_copy: { w1: ["New copy"] } },
    });
    expect(res.statusCode).toBe(200);
    expect(orchestrator.calls[0]?.path).toBe("/internal/v1/campaign-plans/req-12345678/revisions");
  });
});

describe("schema documentation", () => {
  it("publishes request and response schemas", async () => {
    const { app } = await make();
    const res = await app.inject({ method: "GET", url: "/v1/schemas", headers: authHeaders });
    expect(res.statusCode).toBe(200);
    const body = res.json<Record<string, unknown>>();
    expect(Object.keys(body).sort()).toEqual(
      ["campaign_plan_request", "campaign_plan_response", "error_envelope", "review_request", "revision_request"].sort(),
    );
    expect(res.body).not.toContain('"~');
  });
});
