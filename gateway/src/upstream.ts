/**
 * HTTP client for the internal Python orchestration service.
 */

export type UpstreamOutcome =
  | { kind: "response"; status: number; body: unknown; durationMs: number }
  | { kind: "invalid_json"; status: number; durationMs: number }
  | { kind: "timeout"; durationMs: number }
  | { kind: "unreachable"; durationMs: number };

export interface Orchestrator {
  request(
    method: "GET" | "POST",
    path: string,
    requestId: string,
    body?: unknown,
    timeoutMs?: number,
  ): Promise<UpstreamOutcome>;
}

export class HttpOrchestrator implements Orchestrator {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly timeoutMs: number,
  ) {}

  async request(
    method: "GET" | "POST",
    path: string,
    requestId: string,
    body?: unknown,
    timeoutMs: number = this.timeoutMs,
  ): Promise<UpstreamOutcome> {
    const started = performance.now();
    const elapsed = () => Math.round(performance.now() - started);
    let response: Response;
    try {
      response = await fetch(new URL(path, this.baseUrl), {
        method,
        headers: {
          "content-type": "application/json",
          "x-clearcast-internal-token": this.token,
          "x-request-id": requestId,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      const name = error instanceof Error ? error.name : "";
      if (name === "TimeoutError" || name === "AbortError") {
        return { kind: "timeout", durationMs: elapsed() };
      }
      return { kind: "unreachable", durationMs: elapsed() };
    }
    let text: string;
    try {
      text = await response.text();
    } catch {
      return { kind: "timeout", durationMs: elapsed() };
    }
    try {
      return { kind: "response", status: response.status, body: text ? JSON.parse(text) : null, durationMs: elapsed() };
    } catch {
      return { kind: "invalid_json", status: response.status, durationMs: elapsed() };
    }
  }
}
