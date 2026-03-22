"""Lightweight aiohttp web server for the frontend monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


class MonitorServer:
    def __init__(self, agent: Any, host: str = "0.0.0.0", port: int = 5555):
        self.agent = agent
        self.host = host
        self.port = port
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._app = web.Application()
        self._setup_routes()
        self._register_hooks()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/api/state", self._handle_state)
        self._app.router.add_get("/api/conversation", self._handle_conversation)
        self._app.router.add_get("/api/queue", self._handle_queue)
        self._app.router.add_get("/api/game", self._handle_game)
        self._app.router.add_get("/api/ws", self._handle_ws)
        # Static files (production build) — added last so API routes take priority
        dist = Path(__file__).parent.parent / "frontend" / "dist"
        if dist.is_dir():
            # Serve index.html for SPA routing
            self._app.router.add_get("/", self._handle_index)
            self._app.router.add_static("/assets", dist / "assets")
            # Catch-all for SPA routes
            self._app.router.add_get("/{path:.*}", self._handle_spa_fallback)

    def _register_hooks(self) -> None:
        self.agent.on("conversation:update", self._on_conversation_update)
        self.agent.queue.on("action:enqueued", self._on_action_event)
        self.agent.queue.on("action:started", self._on_action_event)
        self.agent.queue.on("action:completed", self._on_action_event)

    async def start(self) -> None:
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info(f"Monitor server running at http://{self.host}:{self.port}")
        asyncio.create_task(self._game_state_loop())

    # --- HTTP handlers ---

    async def _handle_state(self, request: web.Request) -> web.Response:
        game = await self._get_game_state()
        return web.json_response({
            "conversation": self.agent.messages,
            "queue": self.agent.queue.status(),
            "game": game,
        })

    async def _handle_conversation(self, request: web.Request) -> web.Response:
        return web.json_response({"messages": self.agent.messages})

    async def _handle_queue(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.queue.status())

    async def _handle_game(self, request: web.Request) -> web.Response:
        game = await self._get_game_state()
        return web.json_response(game)

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        dist = Path(__file__).parent.parent / "frontend" / "dist"
        return web.FileResponse(dist / "index.html")

    async def _handle_spa_fallback(self, request: web.Request) -> web.FileResponse:
        dist = Path(__file__).parent.parent / "frontend" / "dist"
        path = dist / request.match_info["path"]
        if path.is_file():
            return web.FileResponse(path)
        return web.FileResponse(dist / "index.html")

    # --- WebSocket ---

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info(f"Monitor WebSocket client connected ({len(self._ws_clients)} total)")

        try:
            async for msg in ws:
                pass  # Read-only — ignore client messages
        finally:
            self._ws_clients.discard(ws)
            logger.info(f"Monitor WebSocket client disconnected ({len(self._ws_clients)} total)")

        return ws

    async def _broadcast(self, event_type: str, data: Any) -> None:
        if not self._ws_clients:
            return
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        dead: set[web.WebSocketResponse] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # --- Event callbacks ---

    async def _on_conversation_update(self, event: str, messages: Any) -> None:
        await self._broadcast("conversation:update", {"messages": messages})

    async def _on_action_event(self, event: str, action: Any) -> None:
        await self._broadcast(event.replace(":", "_"), {
            "action": {
                "id": action.id,
                "status": action.status.value,
                "code": action.code,
                "enqueued_at": action.enqueued_at,
                "started_at": action.started_at,
                "finished_at": action.finished_at,
                "result": action.result,
                "error": action.error,
            }
        })

    # --- Game state polling ---

    async def _get_game_state(self) -> dict:
        try:
            resp = await self.agent.bridge.get_status()
            return resp.data
        except Exception as e:
            logger.debug(f"Failed to fetch game state: {e}")
            return {}

    async def _game_state_loop(self) -> None:
        while True:
            try:
                game = await self._get_game_state()
                if game:
                    await self._broadcast("game:state", game)
            except Exception:
                pass
            await asyncio.sleep(2.0)
