"""Lightweight aiohttp web server for the frontend monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .runtime import MONITOR_EVENT_TYPES

logger = logging.getLogger(__name__)

# How many recent world events the snapshot serves; matches the frontend cap.
EVENT_LOG_MAX = 30


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
        self._app.router.add_get("/api/queue", self._handle_queue)
        self._app.router.add_get("/api/game", self._handle_game)
        self._app.router.add_get("/api/ws", self._handle_ws)
        # Video MJPEG proxy: streams the bridge's /video/stream through the
        # monitor so the feed shares this server's origin. Without it the
        # frontend's <img> would point at the bridge host (localhost:8081),
        # which is unreachable from any device other than the host itself
        # (e.g. a phone over Tailscale resolves "localhost" to itself). One
        # port (5555) is now the only surface a remote viewer needs.
        self._app.router.add_get("/video/stream", self._handle_video)
        # Static files (production build) — added last so API routes take priority
        dist = Path(__file__).parent.parent / "frontend" / "dist"
        if dist.is_dir():
            # Serve index.html for SPA routing
            self._app.router.add_get("/", self._handle_index)
            self._app.router.add_static("/assets", dist / "assets")
            # Catch-all for SPA routes
            self._app.router.add_get("/{path:.*}", self._handle_spa_fallback)

    def _register_hooks(self) -> None:
        # conversation/plan/memory/usage were brain bus events — the Runtime
        # never emits them. Only reflex + queue/subaction events remain.
        self.agent.on("reflex:fired", self._on_reflex_fired)
        self.agent.on("event:recorded", self._on_event_recorded)
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
        video_base = getattr(self.agent.bridge, "base_url", "")
        # Reflex log: most-recent first to match the WS push semantics in
        # the frontend (which prepends incoming events).
        reflexes = list(reversed(list(self.agent.reflexes.recent)))
        # Curated world events (chat/death/respawn/advancement) from the
        # flushable buffer — same {type, data, ts} shape as reflexes, also
        # most-recent first. The frontend merges the two into one timeline.
        events = [
            {"type": e.type, "data": e.data, "ts": e.ts}
            for e in self.agent._events
            if e.type in MONITOR_EVENT_TYPES
        ]
        events = list(reversed(events))[:EVENT_LOG_MAX]
        return web.json_response({
            "queue": self.agent.queue.status(),
            "game": game,
            "reflexes": reflexes,
            "events": events,
            # Relative, same-origin path served by _handle_video below — so the
            # feed is reachable wherever the monitor is (Tailscale, LAN, tunnel)
            # with no second port and no hardcoded host. video_base only gates
            # whether a bridge exists to proxy at all.
            "video_url": "/video/stream?fps=10&quality=50" if video_base else None,
        })

    async def _handle_queue(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.queue.status())

    async def _handle_game(self, request: web.Request) -> web.Response:
        game = await self._get_game_state()
        return web.json_response(game)

    async def _handle_video(self, request: web.Request) -> web.StreamResponse:
        """Proxy the bridge's MJPEG stream through the monitor's origin.

        Opens a long-lived upstream GET to ``{bridge.base_url}/video/stream``
        and pumps multipart chunks straight to the client. One upstream
        connection (and thus one bridge-side ffmpeg) per viewer, same as
        hitting the bridge directly — we just relay it on this port.
        """
        base = (getattr(self.agent.bridge, "base_url", "") or "").rstrip("/")
        if not base:
            return web.Response(status=503, text="no bridge configured")
        qs = request.query_string
        upstream_url = f"{base}/video/stream" + (f"?{qs}" if qs else "")
        # No read timeout: an MJPEG stream is intentionally open-ended.
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        )
        try:
            upstream = await session.get(upstream_url)
        except Exception as e:
            await session.close()
            logger.warning(f"video proxy connect failed: {e}")
            return web.Response(status=502, text="bridge video unavailable")

        resp = web.StreamResponse(
            status=upstream.status,
            headers={
                "Content-Type": upstream.headers.get(
                    "Content-Type", "multipart/x-mixed-replace"
                ),
                "Cache-Control": "no-cache, no-store",
            },
        )
        await resp.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError):
            pass  # viewer navigated away / connection dropped — expected
        except Exception as e:
            logger.debug(f"video proxy stream ended: {e}")
        finally:
            upstream.release()
            await session.close()
        return resp

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

    async def _on_reflex_fired(self, event: str, entry: Any) -> None:
        # entry is the dict pushed onto reflexes.recent: {type, data, ts}.
        await self._broadcast("reflex:fired", entry if isinstance(entry, dict) else {})

    async def _on_event_recorded(self, event: str, entry: Any) -> None:
        # entry is a curated world event {type, data, ts}: chat/death/respawn/
        # advancement. Hazard reflexes arrive via _on_reflex_fired instead; the
        # frontend merges both into one Events timeline.
        await self._broadcast("event:recorded", entry if isinstance(entry, dict) else {})

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
        while True:
            try:
                game = await self._get_game_state()
                if game:
                    await self._broadcast("game:state", game)
            except Exception:
                pass
            await asyncio.sleep(2.0)
