"""Full stack through the real launcher: Gradio -> Fastify -> FastAPI -> LangGraph -> MCP subprocess.

Offline: synthetic weather fixtures inside the real MCP stdio subprocess and a
local fake Chat Completions server reached via OPENAI_BASE_URL.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from gradio_client import Client

from evaluation.fake_openai_server import make_server
from tests.helpers import PROJECT_ROOT
from tests.integration.conftest import free_port, require_gateway, wait_http

pytestmark = pytest.mark.integration
FAKE_KEY = "sk-offline-fixture-not-a-real-key"


@pytest.fixture(scope="module")
def running_stack(tmp_path_factory):
    require_gateway()
    openai_port = free_port()
    fake_openai = make_server(openai_port)
    threading.Thread(target=fake_openai.serve_forever, daemon=True).start()
    ports = {"ui": free_port(), "gateway": free_port(), "api": free_port()}
    log_path = tmp_path_factory.mktemp("stack") / "launcher.log"
    env = {
        **os.environ,
        "CLEARCAST_WEATHER_FIXTURES": "baseline_mild",
        "OPENAI_API_KEY": FAKE_KEY,
        "OPENAI_BASE_URL": f"http://127.0.0.1:{openai_port}/v1",
        "CLEARCAST_API_PORT": str(ports["api"]),
        "GATEWAY_PORT": str(ports["gateway"]),
        "GRADIO_SERVER_PORT": str(ports["ui"]),
        "GRADIO_SERVER_NAME": "127.0.0.1",
    }
    env.pop("OPENWEATHERMAP_API_KEY", None)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "deploy.launcher"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            wait_http(f"http://127.0.0.1:{ports['ui']}/", timeout=120)
            wait_http(f"http://127.0.0.1:{ports['gateway']}/ready", timeout=60)
            yield {"ports": ports, "process": process, "log": log_path}
        finally:
            process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
            fake_openai.shutdown()


def generate(client: Client, location: str):
    return client.predict(
        "general",
        location,
        "Coffee shop",
        "Increase morning visits",
        "Friendly",
        "",
        None,
        None,
        None,
        None,
        "No limit",
        "",
        api_name="/generate_plan",
    )


def test_two_browser_sessions_generate_review_and_export(running_stack):
    url = f"http://127.0.0.1:{running_stack['ports']['ui']}/"
    alice, bob = Client(url, verbose=False), Client(url, verbose=False)
    outputs = {}
    for name, client, city in (
        ("alice", alice, "Austin, TX"),
        ("bob", bob, "Seattle, WA"),
        ("alice2", alice, "Denver, CO"),
        ("bob2", bob, "Miami, FL"),
    ):
        result = generate(client, city)
        outputs[name] = json.loads(result[3])  # Plan JSON tab
    # Each browser session has one stable, distinct server-side session.
    assert outputs["alice"]["session_ref"] == outputs["alice2"]["session_ref"]
    assert outputs["bob"]["session_ref"] == outputs["bob2"]["session_ref"]
    assert outputs["alice"]["session_ref"] != outputs["bob"]["session_ref"]
    for plan in outputs.values():
        assert plan["status"] == "pending_review"
        assert plan["evidence"]["sources"][0]["source"] == "fixture:baseline_mild"
        assert plan["diagnostics"]["tool_calls"]["get_forecast"] == 1

    approved = bob.predict("Looks right", api_name="/approve_plan")
    assert "Approved" in approved[4]
    # gradio_client downloads the export files and returns their local paths.
    files = [Path(v["value"]) for v in approved if isinstance(v, dict) and isinstance(v.get("value"), str)]
    json_export = next(f for f in files if f.suffix == ".json")
    md_export = next(f for f in files if f.suffix == ".md")
    assert json.loads(json_export.read_text("utf-8"))["status"] == "approved"
    assert "-approved." in json_export.name and "(Approved)" in md_export.read_text("utf-8")
    # Alice's latest plan is untouched by Bob's approval.
    rejected = alice.predict("Not this week", api_name="/reject_plan")
    assert "Rejected" in rejected[4]


def test_internal_services_are_not_reachable_on_non_loopback_addresses(running_stack):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("10.255.255.255", 1))  # no packets are sent; this selects the LAN interface
        lan_ip = probe.getsockname()[0]
    if lan_ip.startswith("127."):
        pytest.skip("no non-loopback interface available")
    for name in ("api", "gateway"):
        with socket.socket() as sock, pytest.raises(OSError):
            sock.settimeout(2)
            sock.connect((lan_ip, running_stack["ports"][name]))


def test_logs_do_not_contain_credentials(running_stack):
    text = Path(running_stack["log"]).read_text("utf-8", errors="replace")
    assert "launcher.ready" in text and "runtime.init.ready" in text
    assert FAKE_KEY not in text
