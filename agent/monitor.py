"""Lightweight aiohttp web server for the frontend monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from agent.belief_check import diff_belief_vs_actual
from agent.plan import read_plan

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
        self._app.router.add_get("/api/plan", self._handle_plan)
        self._app.router.add_get("/api/ws", self._handle_ws)
        self._app.router.add_post("/api/console/run", self._handle_console_run)
        self._app.router.add_post("/api/console/cancel", self._handle_console_cancel)
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
        self.agent.on("plan:update", self._on_plan_update)
        self.agent.queue.on("action:enqueued", self._on_action_event)
        self.agent.queue.on("action:started", self._on_action_event)
        self.agent.queue.on("action:completed", self._on_action_event)
        self.agent.queue.on("subaction:started", self._on_subaction_event)
        self.agent.queue.on("subaction:completed", self._on_subaction_event)

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
        bridge_url = getattr(self.agent.bridge, "base_url", "")
        return web.json_response({
            "conversation": self.agent.messages,
            "queue": self.agent.queue.status(),
            "game": game,
            "plan": read_plan(),
            "video_url": f"{bridge_url}/video/stream?fps=10&quality=50" if bridge_url else None,
        })

    async def _handle_conversation(self, request: web.Request) -> web.Response:
        return web.json_response({"messages": self.agent.messages})

    async def _handle_queue(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.queue.status())

    async def _handle_game(self, request: web.Request) -> web.Response:
        game = await self._get_game_state()
        return web.json_response(game)

    async def _handle_plan(self, request: web.Request) -> web.Response:
        return web.json_response({"plan": read_plan()})

    async def _handle_console_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or not code.strip():
            return web.json_response({"error": "missing 'code' string"}, status=400)
        action = await self.agent.queue.enqueue(code)
        return web.json_response({"action_id": action.id})

    async def _handle_console_cancel(self, request: web.Request) -> web.Response:
        await self.agent.queue.interrupt()
        return web.json_response({"cancelled": True})

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

    async def _on_plan_update(self, event: str, plan: Any) -> None:
        await self._broadcast("plan:update", {"plan": plan if isinstance(plan, str) else ""})

    async def _on_action_event(self, event: str, action: Any, *_extra: Any) -> None:
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
                "subactions": action.subactions,
            }
        })

    async def _on_subaction_event(self, event: str, action: Any, subaction: dict) -> None:
        await self._broadcast(event.replace(":", "_"), {
            "action_id": action.id,
            "subaction": subaction,
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
        last_mismatch_key: tuple | None = None
        while True:
            try:
                game = await self._get_game_state()
                if game:
                    await self._broadcast("game:state", game)
                    belief = getattr(self.agent, "last_injected_status", None)
                    # Skip the check while a newAction is running: the sandbox
                    # reads fresh bridge state directly, so any mismatch is
                    # noise (the injected snapshot is genuinely stale but the
                    # agent isn't acting on it). Real mismatches we care about
                    # show up between Claude iterations.
                    action_in_flight = (
                        hasattr(self.agent, "queue")
                        and hasattr(self.agent.queue, "is_running")
                        and self.agent.queue.is_running()
                    )
                    # Agent is "idle" if no chat or tool activity in the last
                    # 30s AND queue empty. Under idle, natural state drift
                    # (weather, day/night, mobs wandering into inventory range)
                    # is not a meaningful mismatch — Claude isn't deciding.
                    idle = (
                        not action_in_flight
                        and hasattr(self.agent, "last_activity_ts")
                        and (time.monotonic() - self.agent.last_activity_ts) > 30.0
                    )
                    if belief and not action_in_flight and not idle:
                        mismatches = diff_belief_vs_actual(belief, game)
                        if mismatches:
                            # Dedupe repeated identical mismatches (don't spam)
                            key = tuple(
                                (m.get("field"), repr(m.get("delta")), repr(m.get("changes")))
                                for m in mismatches
                            )
                            if key != last_mismatch_key:
                                last_mismatch_key = key
                                logger.info(
                                    f"belief mismatch: {[m['field'] for m in mismatches]}"
                                )
                                await self._broadcast(
                                    "belief:mismatch",
                                    {"mismatches": mismatches, "belief": belief, "actual": game},
                                )
                                sl = getattr(self.agent, "_current_session_logger", None)
                                if sl is not None:
                                    sl.emit(
                                        "belief_mismatch",
                                        mismatches=mismatches,
                                        belief=belief,
                                        actual=game,
                                    )
                        else:
                            last_mismatch_key = None
            except Exception:
                pass
            await asyncio.sleep(2.0)
