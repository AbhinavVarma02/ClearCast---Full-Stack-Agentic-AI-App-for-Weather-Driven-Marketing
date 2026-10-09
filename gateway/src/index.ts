/**
 * Gateway entry point: load configuration, listen on loopback, shut down cleanly.
 */
import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const app = await buildApp(config);
  await app.listen({ host: config.host, port: config.port });

  let closing = false;
  const shutdown = (signal: NodeJS.Signals) => {
    if (closing) {
      return;
    }
    closing = true;
    app.log.info({ event: "gateway.stop", signal }, "shutting down");
    app.close().then(
      () => process.exit(0),
      () => process.exit(1),
    );
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

main().catch((error: unknown) => {
  // Configuration errors name the missing setting; they never include secret values.
  const message = error instanceof Error ? error.message : "unknown error";
  process.stderr.write(`${JSON.stringify({ level: "fatal", event: "gateway.start_failed", msg: message })}\n`);
  process.exit(1);
});
