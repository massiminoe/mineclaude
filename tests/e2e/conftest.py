"""Fixtures for end-to-end tests.

All tests here are implicitly marked `e2e` — run with `pytest --run-e2e`.

The `mc_stack` fixture brings up docker-compose (session-scoped, so one
startup per test run) and waits for the bridge /health endpoint. The
`agent` fixture builds an in-process Agent pointed at the live bridge.

Requires:
  - docker + docker compose on the host
  - ANTHROPIC_API_KEY in the environment (for real Claude calls)
  - Ports 8081 (bridge HTTP), 8082 (bridge WS), 25565 (MC), 25575 (RCON) free
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from tests.e2e.harness import Scenario, wait_for_bridge

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = [
    "-f", str(REPO_ROOT / "docker-compose.yml"),
    "-f", str(REPO_ROOT / "tests" / "e2e" / "docker-compose.e2e.yml"),
]


@pytest.fixture(scope="session", autouse=True)
def _mark_all_e2e(request):
    """Apply the `e2e` marker to every test in this directory."""
    for item in request.session.items:
        if str(Path(item.fspath)).startswith(str(REPO_ROOT / "tests" / "e2e")):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def mc_stack():
    """Start the docker-compose stack for the duration of the test session."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    subprocess.run(
        ["docker", "compose", *COMPOSE_FILES, "up", "-d", "--build"],
        check=True,
        cwd=REPO_ROOT,
    )
    try:
        asyncio.run(wait_for_bridge(timeout_s=240.0))
        yield
    finally:
        subprocess.run(
            ["docker", "compose", *COMPOSE_FILES, "down", "-v"],
            check=False,
            cwd=REPO_ROOT,
        )


@pytest_asyncio.fixture
async def agent(mc_stack):
    """In-process Agent connected to the live bridge."""
    from agent.agent import Agent
    from agent.bridge import RealBridgeClient
    from agent.claude import ClaudeClient

    bridge = RealBridgeClient(base_url="http://localhost:8081")
    claude = ClaudeClient(api_key=os.environ["ANTHROPIC_API_KEY"])
    ag = Agent(bridge=bridge, claude=claude, bot_name="Mineclaw")
    ag.queue.set_executor(ag._execute_action)
    ag.queue.start()
    try:
        yield ag
    finally:
        await ag.queue.interrupt()
        await bridge.close()


@pytest.fixture
def scenario(agent):
    return Scenario(agent)
