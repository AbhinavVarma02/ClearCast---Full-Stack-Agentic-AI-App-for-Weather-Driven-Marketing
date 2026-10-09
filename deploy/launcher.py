"""Single-container process supervisor for ClearCast.

Starts three processes in dependency order and waits for each to be healthy:

1. FastAPI orchestrator (127.0.0.1) - owns LangGraph, MCP, providers, validation.
2. Node.js Fastify gateway (127.0.0.1) - typed public contract in front of (1).
3. Gradio UI (the only public port, 7860 on Hugging Face).

Each child receives only the environment it needs: provider secrets go to the
orchestrator alone, and per-boot random tokens authenticate the internal hops.
A crashed child is restarted with backoff up to a limit; SIGTERM/SIGINT stop
all children gracefully. Run with ``python -m deploy.launcher`` (or ``python app.py``).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ENTRY = PROJECT_ROOT / "gateway" / "dist" / "index.js"

PROVIDER_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENWEATHERMAP_API_KEY",
    "CLEARCAST_OPENAI_MODEL",
    "CLEARCAST_WEATHER_FIXTURES",
    "CLEARCAST_WEATHER_CACHE_TTL_SECONDS",
    "CLEARCAST_MODEL_INPUT_USD_PER_1M_TOKENS",
    "CLEARCAST_MODEL_OUTPUT_USD_PER_1M_TOKENS",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_ENDPOINT",
    "LANGCHAIN_PROJECT",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_PROJECT",
    "LANGSMITH_TRACING",
)
# Non-secret runtime variables every child may need (paths, locale, HF metadata).
BASE_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "SPACE_ID",
    "SPACE_HOST",
    "SPACE_AUTHOR_NAME",
    "SPACE_REPO_NAME",
    "GRADIO_TEMP_DIR",
    "HF_HOME",
    "CLEARCAST_LOG_LEVEL",
)
MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 600


def log(event: str, **fields) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "level": "info",
        "logger": "clearcast.launcher",
        "event": event,
        **fields,
    }
    print(json.dumps(record), flush=True)


def _pick(names: tuple[str, ...]) -> dict[str, str]:
    return {name: os.environ[name] for name in names if os.environ.get(name)}


@dataclass
class Service:
    name: str
    command: list[str]
    env: dict[str, str]
    health_url: str
    startup_timeout: float
    process: subprocess.Popen | None = None
    restarts: list[float] = field(default_factory=list)

    def start(self) -> None:
        self.process = subprocess.Popen(self.command, cwd=PROJECT_ROOT, env=self.env)
        log("service.start", service=self.name, pid=self.process.pid)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return False
            try:
                with urllib.request.urlopen(self.health_url, timeout=2) as response:
                    if response.status == 200:
                        log("service.healthy", service=self.name)
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.5)
        return False

    def stop(self, timeout: float = 10.0) -> None:
        if not self.alive():
            return
        assert self.process is not None
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        log("service.stop", service=self.name, returncode=self.process.returncode)


def build_services() -> list[Service]:
    api_port = os.getenv("CLEARCAST_API_PORT", "8001")
    gateway_port = os.getenv("GATEWAY_PORT", "8787")
    ui_host = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    ui_port = os.getenv("GRADIO_SERVER_PORT", "7860")
    internal_token = os.getenv("CLEARCAST_INTERNAL_TOKEN") or secrets.token_urlsafe(32)
    gateway_token = os.getenv("CLEARCAST_GATEWAY_TOKEN") or secrets.token_urlsafe(32)
    base = {**_pick(BASE_ENV), "PYTHONUNBUFFERED": "1"}

    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required to run the ClearCast gateway (node was not found on PATH).")
    if not GATEWAY_ENTRY.exists():
        raise SystemExit("The gateway is not built. Run: npm --prefix gateway ci && npm --prefix gateway run build")
    return [
        Service(
            name="orchestrator",
            command=[sys.executable, "-m", "api.main"],
            env={
                **base,
                **_pick(PROVIDER_ENV),
                "CLEARCAST_API_HOST": "127.0.0.1",
                "CLEARCAST_API_PORT": api_port,
                "CLEARCAST_INTERNAL_TOKEN": internal_token,
            },
            health_url=f"http://127.0.0.1:{api_port}/internal/health",
            startup_timeout=90,
        ),
        Service(
            name="gateway",
            command=[node, str(GATEWAY_ENTRY)],
            env={
                **base,
                "NODE_ENV": "production",
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": gateway_port,
                "CLEARCAST_API_URL": f"http://127.0.0.1:{api_port}",
                "CLEARCAST_INTERNAL_TOKEN": internal_token,
                "CLEARCAST_GATEWAY_TOKEN": gateway_token,
            },
            health_url=f"http://127.0.0.1:{gateway_port}/health",
            startup_timeout=30,
        ),
        Service(
            name="frontend",
            command=[sys.executable, "-m", "frontend.app"],
            env={
                **base,
                "GRADIO_SERVER_NAME": ui_host,
                "GRADIO_SERVER_PORT": ui_port,
                "GRADIO_ANALYTICS_ENABLED": "False",
                "CLEARCAST_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
                "CLEARCAST_GATEWAY_TOKEN": gateway_token,
            },
            health_url=f"http://127.0.0.1:{ui_port}/",
            startup_timeout=90,
        ),
    ]


class Supervisor:
    def __init__(self, services: list[Service]) -> None:
        self.services = services
        self.stopping = False

    def request_stop(self, signum, _frame) -> None:
        log("launcher.signal", signal=signal.Signals(signum).name)
        self.stopping = True

    def stop_all(self) -> None:
        for service in reversed(self.services):
            service.stop()

    def start_all(self) -> bool:
        for service in self.services:
            service.start()
            if not service.wait_healthy():
                log("service.unhealthy", service=service.name)
                return False
        log("launcher.ready", services=[s.name for s in self.services])
        return True

    def restart(self, service: Service) -> bool:
        now = time.monotonic()
        service.restarts = [t for t in service.restarts if now - t < RESTART_WINDOW_SECONDS] + [now]
        if len(service.restarts) > MAX_RESTARTS:
            log("service.restart_limit", service=service.name, restarts=len(service.restarts) - 1)
            return False
        delay = min(2 ** (len(service.restarts) - 1), 10)
        log(
            "service.restart",
            service=service.name,
            delay_seconds=delay,
            returncode=service.process.returncode if service.process else None,
        )
        time.sleep(delay)
        service.start()
        return service.wait_healthy()

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        if hasattr(signal, "SIGBREAK"):  # Windows: CTRL_BREAK_EVENT from a parent process
            signal.signal(signal.SIGBREAK, self.request_stop)
        try:
            if not self.start_all():
                return 1
            while not self.stopping:
                for service in self.services:
                    if not self.stopping and not service.alive() and not self.restart(service):
                        return 1
                time.sleep(1)
            return 0
        finally:
            self.stop_all()


def main() -> int:
    if os.getenv("CLEARCAST_WEATHER_FIXTURES"):
        log("launcher.offline_fixtures", warning="Synthetic weather fixtures are enabled; data is not real.")
    return Supervisor(build_services()).run()


if __name__ == "__main__":
    raise SystemExit(main())
