"""Tiny HTTP server that speaks the Chat Completions API using ScriptedOpenAI.

Used by the full-stack integration test and for offline demos:
    python -m evaluation.fake_openai_server --port 8099
then start ClearCast with OPENAI_BASE_URL=http://127.0.0.1:8099/v1 and
CLEARCAST_WEATHER_FIXTURES=baseline_mild. Never used by the deployed app.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evaluation.fake_openai import ScriptedOpenAI


def make_server(port: int, model: ScriptedOpenAI | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    scripted = model or ScriptedOpenAI()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            length = int(self.headers.get("content-length") or 0)
            status, payload = scripted.respond(json.loads(self.rfile.read(length)))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # keep test output quiet
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = make_server(args.port, host=args.host)
    print(f"Fake OpenAI listening on http://{args.host}:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
