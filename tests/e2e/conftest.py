"""Isolated Minecraft + real MCP launcher; no model credentials required."""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def wait_for(url, ready, *, process=None, timeout=600):
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5) as client:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError("MCP launcher exited; inspect runtime.log")
            try:
                response = client.get(url)
                if response.status_code == 200 and ready(response.json()):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(1)
    raise TimeoutError(f"Readiness timeout: {url}")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def mc_stack():
    project = f"mineclaude-e2e-{uuid.uuid4().hex[:10]}"
    evidence = REPO_ROOT / "state" / "e2e" / project
    evidence.mkdir(parents=True)
    command = ["docker", "compose", "-p", project, "-f", str(REPO_ROOT / "docker-compose.yml")]
    if platform.machine().lower() in {"arm64", "aarch64"}:
        command += ["-f", str(REPO_ROOT / "docker-compose.arm64.yml")]
    command += ["-f", str(REPO_ROOT / "tests/e2e/docker-compose.e2e.yml")]

    def compose(*args, **kwargs):
        return subprocess.run(command + list(args), cwd=REPO_ROOT, check=True, **kwargs)

    try:
        with (evidence / "build.log").open("w") as log:
            compose("up", "-d", "--build", stdout=log, stderr=subprocess.STDOUT)
        images = compose("images", "--format", "json", capture_output=True, text=True).stdout
        (evidence / "images.json").write_text(images)
        mods = compose("exec", "-T", "mc-server", "find", "/data/mods", "-maxdepth", "1",
                       "-name", "*.jar", capture_output=True, text=True).stdout
        (evidence / "server-mods.txt").write_text(mods)

        def port(container_port):
            address = compose("port", "mc-client", str(container_port), capture_output=True, text=True).stdout.strip()
            return address.rsplit(":", 1)[1]
        bridge = f"http://127.0.0.1:{port(8081)}"
        # /health alone can succeed while Minecraft is still at the title screen.
        wait_for(bridge + "/status", lambda data: data.get("data", {}).get("health", 0) > 0)
        yield {"compose": compose, "bridge": bridge,
               "ws": f"ws://127.0.0.1:{port(8082)}/events", "evidence": evidence}
    finally:
        with (evidence / "containers.log").open("w") as log:
            subprocess.run(command + ["logs", "--no-color"], cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT)
        # Only this session's uniquely named project is removed, even on failure.
        subprocess.run(command + ["down", "-v", "--remove-orphans"], cwd=REPO_ROOT, check=True)


@pytest.fixture
def live_runtime(mc_stack):
    monitor_port, mcp_port = unused_port(), unused_port()
    while monitor_port == mcp_port:
        mcp_port = unused_port()
    with (mc_stack["evidence"] / "runtime.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "mineclaude.main"], cwd=REPO_ROOT,
            env={**os.environ, "MOCK_BRIDGE": "0", "SESSION_LOG": "0", "SKIN_TEXTURE_URL": "",
                 "BRIDGE_URL": mc_stack["bridge"], "BRIDGE_WS_URL": mc_stack["ws"],
                 "MONITOR_PORT": str(monitor_port), "MCP_HOST": "127.0.0.1", "MCP_PORT": str(mcp_port)},
            stdout=log, stderr=subprocess.STDOUT)
        try:
            monitor = f"http://127.0.0.1:{monitor_port}"
            wait_for(monitor + "/api/state", lambda data: bool(data.get("game")), process=process, timeout=30)
            yield {**mc_stack, "monitor": monitor, "mcp": f"http://127.0.0.1:{mcp_port}/mcp"}
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
