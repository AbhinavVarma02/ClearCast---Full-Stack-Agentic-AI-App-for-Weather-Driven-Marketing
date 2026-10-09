import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { GatewayConfig } from "../src/config.js";
import type { Orchestrator, UpstreamOutcome } from "../src/upstream.js";

const here = path.dirname(fileURLToPath(import.meta.url));
export const CONTRACTS_DIR = path.resolve(here, "..", "..", "contracts");

export const CLIENT_TOKEN = "test-client-token-0123456789";

export function testConfig(overrides: Partial<GatewayConfig> = {}): GatewayConfig {
  return {
    host: "127.0.0.1",
    port: 0,
    upstreamUrl: "http://127.0.0.1:9",
    upstreamToken: "test-internal-token-0123456789",
    clientToken: CLIENT_TOKEN,
    bodyLimitBytes: 16 * 1024,
    upstreamTimeoutMs: 2_000,
    readyTimeoutMs: 500,
    planRateLimitPerMinute: 6,
    globalRateLimitPerMinute: 1_000,
    maxInflightPlans: 4,
    logLevel: "silent",
    ...overrides,
  };
}

export function loadJson(...parts: string[]): unknown {
  return JSON.parse(readFileSync(path.join(CONTRACTS_DIR, ...parts), "utf8"));
}

export interface ContractCase {
  name: string;
  body: unknown;
  gateway_valid: boolean;
  api_valid: boolean;
}

export const contractCases = (loadJson("fixtures", "plan_requests.json") as { cases: ContractCase[] }).cases;
export const samplePlanResponse = loadJson("fixtures", "sample_plan_response.json") as Record<string, unknown>;

export interface RecordedCall {
  method: string;
  path: string;
  requestId: string;
  body: unknown;
}

/** Scripted stand-in for the Python service that records every forwarded call. */
export class FakeOrchestrator implements Orchestrator {
  calls: RecordedCall[] = [];
  next: (call: RecordedCall) => Promise<UpstreamOutcome> | UpstreamOutcome = () => ({
    kind: "response",
    status: 200,
    body: samplePlanResponse,
    durationMs: 1,
  });

  request(method: "GET" | "POST", path: string, requestId: string, body?: unknown): Promise<UpstreamOutcome> {
    const call = { method, path, requestId, body };
    this.calls.push(call);
    return Promise.resolve(this.next(call));
  }
}

export const authHeaders = { "x-clearcast-client-token": CLIENT_TOKEN, "content-type": "application/json" };

export const validBody = {
  session_id: "sess_gateway_test_000001",
  brief: { location: "Baltimore, MD", business_type: "Coffee shop" },
};
