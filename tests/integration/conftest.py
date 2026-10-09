"""Helpers for multi-process integration tests (Node.js gateway + Python services)."""

from __future__ import annotations

import os
import shutil
import socket
import time
import urllib.request

import pytest

from tests.helpers import PROJECT_ROOT

GATEWAY_ENTRY = PROJECT_ROOT / "gateway" / "dist" / "index.js"


def require_gateway() -> str:
    """Return the node executable, or skip/fail when the built gateway is unavailable."""
    node = shutil.which("node")
    if node and GATEWAY_ENTRY.exists():
        return node
    message = "Node.js and a built gateway (npm --prefix gateway ci && npm --prefix gateway run build) are required"
    if os.getenv("CLEARCAST_REQUIRE_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, timeout: float = 60.0, expect: int = 200) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == expect:
                    return
        except OSError as exc:  # includes URLError and HTTPError
            last = exc
            if getattr(exc, "code", None) == expect:
                return
        time.sleep(0.25)
    raise TimeoutError(f"{url} not ready: {last!r}")
