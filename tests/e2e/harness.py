"""E2E test harness for mineclaude.

Provides:
  - `BridgeHealthWaiter` — polls GET /health until the bridge answers 200
  - `Scenario` — thin wrapper that runs an in-process Agent against a live
    bridge, drives it with a chat, and waits for the turn to complete
  - `Rcon` — very small RCON client for setting up world state

The harness intentionally runs the Agent in-process (not in its own
container) so scenario assertions have direct access to `agent.messages`,
session log, and last_injected_status. The real bridge (and real MC
server) come from docker-compose.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from dataclasses import dataclass

import httpx


BRIDGE_URL = "http://localhost:8081"
RCON_HOST = "localhost"
RCON_PORT = 25575
RCON_PASSWORD = "mineclaude"


async def wait_for_bridge(timeout_s: float = 180.0) -> None:
    """Block until GET /health on the bridge returns 200."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{BRIDGE_URL}/health")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    raise TimeoutError(f"Bridge did not become healthy in {timeout_s}s")


class Rcon:
    """Minimal synchronous RCON client for scenario setup.

    Protocol ref: https://wiki.vg/RCON — simple length-prefixed packets
    with auth (type 3), command (type 2), response (type 0).
    """

    def __init__(self, host: str = RCON_HOST, port: int = RCON_PORT, password: str = RCON_PASSWORD):
        self.host = host
        self.port = port
        self.password = password
        self._sock: socket.socket | None = None
        self._req_id = 0

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=10.0)
        self._send(3, self.password)
        resp_id, _ = self._recv()
        if resp_id == -1:
            raise ConnectionError("RCON auth failed")

    def cmd(self, command: str) -> str:
        if self._sock is None:
            self.connect()
        self._send(2, command)
        _, body = self._recv()
        return body

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _send(self, type_: int, body: str) -> None:
        assert self._sock is not None
        self._req_id += 1
        payload = struct.pack("<ii", self._req_id, type_) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv(self) -> tuple[int, str]:
        assert self._sock is not None
        length = struct.unpack("<i", self._recvall(4))[0]
        raw = self._recvall(length)
        resp_id, _ = struct.unpack("<ii", raw[:8])
        body = raw[8:-2].decode("utf-8", errors="replace")
        return resp_id, body

    def _recvall(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("RCON connection closed")
            buf.extend(chunk)
        return bytes(buf)


@dataclass
class TurnResult:
    """Snapshot of what happened in one chat → response cycle."""
    text_sent: str | None
    iterations: int
    tool_calls: list[dict]
    session_log_path: str | None
    messages: list[dict]
    last_injected_status: dict | None


class Scenario:
    """Drive an in-process Agent through one chat and capture the result."""

    def __init__(self, agent):
        self.agent = agent
        self._sent_chats: list[str] = []
        # Capture chat_out by shimming _send_chat
        self._original_send_chat = agent._send_chat

        async def _capture(text: str) -> None:
            self._sent_chats.append(text)

        agent._send_chat = _capture  # type: ignore[method-assign]

    async def say(self, username: str, message: str, timeout_s: float = 300.0) -> TurnResult:
        """Send a chat from a fake player and wait for the agent's reply."""
        pre_count = len(self._sent_chats)
        task = asyncio.create_task(self.agent._handle_chat({"username": username, "message": message}))
        deadline = time.monotonic() + timeout_s
        while not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if not task.done():
            task.cancel()
            raise TimeoutError(f"Agent did not finish turn within {timeout_s}s")
        await task

        text_sent = self._sent_chats[pre_count] if len(self._sent_chats) > pre_count else None

        # Extract tool calls from messages after this chat started
        tool_calls: list[dict] = []
        for msg in self.agent.messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name"), "input": block.get("input")})

        session_path = None
        sl = getattr(self.agent, "_current_session_logger", None)
        if sl is not None:
            session_path = str(sl.path)

        # Iterations ≈ number of gamestate_auto_N injections in messages.
        iterations = sum(
            1
            for m in self.agent.messages
            if isinstance(m.get("content"), list)
            and any(
                isinstance(b, dict) and str(b.get("id", "")).startswith("gamestate_auto_")
                for b in m["content"]
            )
        )

        return TurnResult(
            text_sent=text_sent,
            iterations=iterations,
            tool_calls=tool_calls,
            session_log_path=session_path,
            messages=list(self.agent.messages),
            last_injected_status=self.agent.last_injected_status,
        )

    def read_session_log(self) -> list[dict]:
        """Parse the session log into a list of event dicts."""
        sl = getattr(self.agent, "_current_session_logger", None)
        if sl is None or not sl.path.exists():
            return []
        with sl.path.open() as f:
            return [json.loads(line) for line in f if line.strip()]
