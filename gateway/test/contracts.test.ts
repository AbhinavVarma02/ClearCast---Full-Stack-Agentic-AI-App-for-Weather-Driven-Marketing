import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { createContractValidator } from "../src/contracts.js";
import { HttpOrchestrator } from "../src/upstream.js";
import { CONTRACTS_DIR, samplePlanResponse } from "./helpers.js";

describe("response contract generated from Pydantic", () => {
  const validator = createContractValidator("campaign_plan_response", CONTRACTS_DIR);

  it("accepts a plan produced by the Python service", () => {
    expect(validator.validate(samplePlanResponse)).toEqual({ ok: true });
  });

  it("rejects responses with missing or mistyped fields", () => {
    const plan = structuredClone(samplePlanResponse) as { plan: Record<string, unknown> };
    delete plan.plan.validation;
    expect(validator.validate(plan).ok).toBe(false);

    const wrongStatus = structuredClone(samplePlanResponse) as { plan: Record<string, unknown> };
    wrongStatus.plan.status = "published";
    expect(validator.validate(wrongStatus).ok).toBe(false);

    const extra: Record<string, unknown> = structuredClone(samplePlanResponse);
    extra.debug_prompt = "should not be here";
    expect(validator.validate(extra).ok).toBe(false);
  });
});

describe("configuration", () => {
  const base = { CLEARCAST_INTERNAL_TOKEN: "internal-token-123456", CLEARCAST_GATEWAY_TOKEN: "client-token-123456" };

  it("loads loopback defaults", () => {
    const config = loadConfig(base);
    expect(config.host).toBe("127.0.0.1");
    expect(config.port).toBe(8787);
    expect(config.upstreamUrl).toBe("http://127.0.0.1:8001");
  });

  it("refuses public bind addresses and non-loopback upstreams", () => {
    expect(() => loadConfig({ ...base, GATEWAY_HOST: "0.0.0.0" })).toThrow(/loopback/);
    expect(() => loadConfig({ ...base, CLEARCAST_API_URL: "http://example.com:8001" })).toThrow(/loopback/);
  });

  it("requires both shared secrets and never echoes them", () => {
    expect(() => loadConfig({ CLEARCAST_GATEWAY_TOKEN: "x" })).toThrow("CLEARCAST_INTERNAL_TOKEN must be set");
    expect(() => loadConfig({ CLEARCAST_INTERNAL_TOKEN: "x" })).toThrow("CLEARCAST_GATEWAY_TOKEN must be set");
    try {
      loadConfig({ ...base, GATEWAY_PORT: "not-a-port" });
    } catch (error) {
      expect(String(error)).not.toContain(base.CLEARCAST_INTERNAL_TOKEN);
    }
  });
});

describe("HTTP orchestrator client", () => {
  let server: Server | undefined;

  afterEach(async () => {
    await new Promise<void>((resolve) => {
      if (server) {
        server.close(() => {
          resolve();
        });
      } else {
        resolve();
      }
    });
    server = undefined;
  });

  async function listen(handler: (req: IncomingMessage, body: string) => { status: number; body: string; delayMs?: number }) {
    server = createServer((req, res) => {
      let body = "";
      req.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      req.on("end", () => {
        const result = handler(req, body);
        setTimeout(() => {
          res.writeHead(result.status, { "content-type": "application/json" });
          res.end(result.body);
        }, result.delayMs ?? 0);
      });
    });
    await new Promise<void>((resolve) => server?.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    return `http://127.0.0.1:${port}`;
  }

  it("sends the internal token and request id and parses JSON", async () => {
    let seen: IncomingMessage | undefined;
    let seenBody = "";
    const url = await listen((req, body) => {
      seen = req;
      seenBody = body;
      return { status: 200, body: JSON.stringify({ ok: true }) };
    });
    const client = new HttpOrchestrator(url, "internal-secret-xyz", 2_000);
    const outcome = await client.request("POST", "/internal/v1/campaign-plans", "req-00000042", { a: 1 });
    expect(outcome).toMatchObject({ kind: "response", status: 200, body: { ok: true } });
    expect(seen?.headers["x-clearcast-internal-token"]).toBe("internal-secret-xyz");
    expect(seen?.headers["x-request-id"]).toBe("req-00000042");
    expect(JSON.parse(seenBody)).toEqual({ a: 1 });
  });

  it("classifies timeouts, invalid JSON, and unreachable upstreams", async () => {
    const url = await listen((req) =>
      req.url === "/slow" ? { status: 200, body: "{}", delayMs: 500 } : { status: 200, body: "<html>" },
    );
    const client = new HttpOrchestrator(url, "t", 100);
    expect((await client.request("GET", "/slow", "req-00000001")).kind).toBe("timeout");
    expect((await client.request("GET", "/html", "req-00000002")).kind).toBe("invalid_json");
    const dead = new HttpOrchestrator("http://127.0.0.1:9", "t", 1_000);
    expect((await dead.request("GET", "/", "req-00000003")).kind).toBe("unreachable");
  });
});
