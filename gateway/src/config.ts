/**
 * Gateway configuration loaded from environment variables and validated at startup.
 */

export interface GatewayConfig {
  readonly host: string;
  readonly port: number;
  /** Base URL of the internal Python orchestration service (loopback). */
  readonly upstreamUrl: string;
  /** Shared secret sent to the Python service on every internal call. */
  readonly upstreamToken: string;
  /** Shared secret the trusted Gradio app must present to the gateway. */
  readonly clientToken: string;
  readonly bodyLimitBytes: number;
  readonly upstreamTimeoutMs: number;
  readonly readyTimeoutMs: number;
  readonly planRateLimitPerMinute: number;
  readonly globalRateLimitPerMinute: number;
  readonly maxInflightPlans: number;
  readonly logLevel: string;
}

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);

function integer(env: NodeJS.ProcessEnv, name: string, fallback: number, min: number, max: number): number {
  const raw = env[name];
  if (raw === undefined || raw.trim() === "") {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}`);
  }
  return value;
}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`${name} must be set`);
  }
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  const host = env.GATEWAY_HOST?.trim() || "127.0.0.1";
  if (!LOOPBACK_HOSTS.has(host)) {
    // The gateway sits behind the Gradio app inside one container; it is not a public service.
    throw new Error("GATEWAY_HOST must be a loopback address");
  }
  const upstreamUrl = env.CLEARCAST_API_URL?.trim() || "http://127.0.0.1:8001";
  const upstreamHost = new URL(upstreamUrl).hostname.replace(/^\[|\]$/g, "");
  if (!LOOPBACK_HOSTS.has(upstreamHost)) {
    throw new Error("CLEARCAST_API_URL must point to a loopback address");
  }
  return {
    host,
    port: integer(env, "GATEWAY_PORT", 8787, 1, 65535),
    upstreamUrl,
    upstreamToken: required(env, "CLEARCAST_INTERNAL_TOKEN"),
    clientToken: required(env, "CLEARCAST_GATEWAY_TOKEN"),
    bodyLimitBytes: integer(env, "GATEWAY_BODY_LIMIT_BYTES", 16 * 1024, 1024, 1024 * 1024),
    upstreamTimeoutMs: integer(env, "GATEWAY_UPSTREAM_TIMEOUT_MS", 180_000, 1_000, 600_000),
    readyTimeoutMs: integer(env, "GATEWAY_READY_TIMEOUT_MS", 3_000, 100, 60_000),
    planRateLimitPerMinute: integer(env, "GATEWAY_PLAN_RATE_LIMIT_PER_MINUTE", 6, 1, 10_000),
    globalRateLimitPerMinute: integer(env, "GATEWAY_GLOBAL_RATE_LIMIT_PER_MINUTE", 120, 1, 100_000),
    maxInflightPlans: integer(env, "GATEWAY_MAX_INFLIGHT_PLANS", 4, 1, 1_000),
    logLevel: env.GATEWAY_LOG_LEVEL?.trim() || "info",
  };
}
