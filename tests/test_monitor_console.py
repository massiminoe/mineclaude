"""Smoke tests for the console endpoints on the monitor server.

The console panel POSTs to /api/console/run to enqueue a code snippet
on the same action queue Claude uses, and to /api/console/cancel to
interrupt a hung snippet. These tests stand up a MonitorServer with
a stub agent that exposes only the surface the endpoints touch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.action_queue import ActionQueue
from agent.monitor import MonitorServer


@dataclass
class _StubBridge:
    base_url: str = ""

    async def get_status(self) -> Any:
        return _StatusResp()


@dataclass
class _StatusResp:
    data: dict = field(default_factory=dict)


class _StubAgent:
    """Minimal Agent surface used by the console endpoints."""

    def __init__(self) -> None:
        self.bridge = _StubBridge()
        self.queue = ActionQueue()
        self.messages: list[dict[str, Any]] = []
        self.last_injected_status: dict[str, Any] | None = None
        self.last_activity_ts: float = 0.0
        self._callbacks: dict[str, list] = {}

    def on(self, event: str, callback: Any) -> None:
        self._callbacks.setdefault(event, []).append(callback)


async def _echo_executor(code: str) -> str:
    await asyncio.sleep(0.01)
    return f"ran: {code}"


@pytest.fixture
async def client():
    agent = _StubAgent()
    agent.queue.set_executor(_echo_executor)
    agent.queue.start()
    monitor = MonitorServer(agent, port=0)
    server = TestServer(monitor._app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, agent
    finally:
        await client.close()
        await agent.queue.stop()


@pytest.mark.asyncio
async def test_console_run_enqueues_action(client):
    c, agent = client
    resp = await c.post("/api/console/run", json={"code": "await goToPosition(0, 0, y=64)"})
    assert resp.status == 200
    body = await resp.json()
    assert "action_id" in body
    await agent.queue.drain()
    status = agent.queue.status()
    assert any(
        r["id"] == body["action_id"] and r["status"] == "completed"
        for r in status["recent"]
    )


@pytest.mark.asyncio
async def test_console_run_rejects_missing_code(client):
    c, _ = client
    resp = await c.post("/api/console/run", json={})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_console_run_rejects_blank_code(client):
    c, _ = client
    resp = await c.post("/api/console/run", json={"code": "   "})
    assert resp.status == 400


@pytest.mark.asyncio
async def test_console_run_rejects_invalid_json(client):
    c, _ = client
    resp = await c.post(
        "/api/console/run",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_console_cancel_interrupts_queue(client):
    c, agent = client

    async def slow(_code: str) -> str:
        await asyncio.sleep(5)
        return "should not finish"

    agent.queue.set_executor(slow)
    await agent.queue.enqueue("running")
    await agent.queue.enqueue("pending")
    await asyncio.sleep(0.05)  # let the first start

    resp = await c.post("/api/console/cancel", json={})
    assert resp.status == 200
    body = await resp.json()
    assert body == {"cancelled": True}

    status = agent.queue.status()
    assert status["running"] is None
    assert status["pending"] == []
