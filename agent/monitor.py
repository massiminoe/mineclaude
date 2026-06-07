"""Lightweight aiohttp web server for the frontend monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from agent.session_log import DEFAULT_BASE_DIR as SESSIONS_DIR, IMAGES_DIRNAME

logger = logging.getLogger(__name__)


def _load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"load events failed for {path}: {e}")
    return events


def _summarize_session(path: Path) -> dict[str, Any]:
    """Cheap pass over a session JSONL to extract list-page metadata."""
    summary: dict[str, Any] = {
        "stem": path.stem,
        "size": 0,
        "mtime": 0.0,
        "started_at": None,
        "ended_at": None,
        "turn_count": 0,
        "iteration_count": 0,
        "tool_call_count": 0,
        "screenshot_count": 0,
        "exception_count": 0,
        "first_user_message": None,
        "session_id": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cost_usd": 0.0,
            "calls": 0,
        },
    }
    try:
        st = path.stat()
        summary["size"] = st.st_size
        summary["mtime"] = st.st_mtime
        last_ts: float | None = None
        first_ts: float | None = None
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = e.get("ts")
                if isinstance(ts, (int, float)):
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                ev = e.get("event")
                data = e.get("data") or {}
                if ev == "session_open":
                    summary["session_id"] = data.get("session_id")
                elif ev == "chat_in":
                    summary["turn_count"] += 1
                    if summary["first_user_message"] is None:
                        msg = data.get("message")
                        if isinstance(msg, str):
                            summary["first_user_message"] = msg[:200]
                elif ev == "claude_request":
                    summary["iteration_count"] += 1
                elif ev == "tool_dispatch":
                    summary["tool_call_count"] += 1
                    res = data.get("result")
                    if isinstance(res, dict) and res.get("type") == "image":
                        summary["screenshot_count"] += 1
                elif ev == "exception":
                    summary["exception_count"] += 1
                elif ev == "claude_usage":
                    u = summary["usage"]
                    raw = data.get("usage") or {}
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    ):
                        v = raw.get(k)
                        if isinstance(v, (int, float)):
                            u[k] += int(v)
                    cost = data.get("cost_usd")
                    if isinstance(cost, (int, float)):
                        u["cost_usd"] += float(cost)
                    u["calls"] += 1
        summary["started_at"] = first_ts
        summary["ended_at"] = last_ts
    except Exception as e:
        logger.debug(f"session summary failed for {path}: {e}")
    return summary


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
        self._app.router.add_get("/api/memory", self._handle_memory)
        self._app.router.add_get("/api/ws", self._handle_ws)
        self._app.router.add_get("/api/sessions", self._handle_sessions_list)
        self._app.router.add_get("/api/sessions/{stem}", self._handle_session_detail)
        self._app.router.add_get("/api/sessions/{stem}/images/{name}", self._handle_session_image)
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
        # conversation/plan/memory/usage were brain bus events — the Runtime
        # never emits them. Only reflex + queue/subaction events remain.
        self.agent.on("reflex:fired", self._on_reflex_fired)
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
        return web.json_response({
            # `messages` is brain-only; a Runtime-backed monitor (MCP mode) has
            # no conversation, so degrade to empty rather than AttributeError.
            # conversation/plan/memory/usage were brain-only. Kept as empty keys
            # so the (yet-untouched) frontend contract holds; their panels are
            # removed in the P6 frontend pass.
            "conversation": [],
            "queue": self.agent.queue.status(),
            "game": game,
            "plan": "",
            "memory": "",
            "reflexes": reflexes,
            "usage": getattr(self.agent, "usage_totals", None),
            "video_url": f"{video_base}/video/stream?fps=10&quality=50" if video_base else None,
        })

    async def _handle_conversation(self, request: web.Request) -> web.Response:
        return web.json_response({"messages": []})

    async def _handle_queue(self, request: web.Request) -> web.Response:
        return web.json_response(self.agent.queue.status())

    async def _handle_game(self, request: web.Request) -> web.Response:
        game = await self._get_game_state()
        return web.json_response(game)

    async def _handle_plan(self, request: web.Request) -> web.Response:
        return web.json_response({"plan": ""})

    async def _handle_memory(self, request: web.Request) -> web.Response:
        return web.json_response({"memory": ""})

    async def _handle_sessions_list(self, request: web.Request) -> web.Response:
        sessions_dir = Path(SESSIONS_DIR)
        if not sessions_dir.is_dir():
            return web.json_response({"sessions": []})
        files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        sessions = await asyncio.to_thread(lambda: [_summarize_session(p) for p in files])
        return web.json_response({"sessions": sessions})

    async def _handle_session_detail(self, request: web.Request) -> web.Response:
        stem = request.match_info["stem"]
        path = self._session_path(stem)
        if path is None:
            return web.json_response({"error": "not found"}, status=404)
        events = await asyncio.to_thread(_load_events, path)
        return web.json_response({
            "stem": stem,
            "summary": _summarize_session(path),
            "events": events,
        })

    async def _handle_session_image(self, request: web.Request) -> web.Response:
        stem = request.match_info["stem"]
        name = request.match_info["name"]
        # Block traversal — names must be a single path segment.
        if "/" in name or ".." in name or name.startswith("."):
            return web.Response(status=400)
        if self._session_path(stem) is None:
            return web.Response(status=404)
        img = Path(SESSIONS_DIR) / IMAGES_DIRNAME / stem / name
        if not img.is_file():
            return web.Response(status=404)
        ctype = "image/jpeg" if img.suffix.lower() in (".jpg", ".jpeg") else "application/octet-stream"
        return web.FileResponse(img, headers={"Content-Type": ctype, "Cache-Control": "public, max-age=86400"})

    def _session_path(self, stem: str) -> Path | None:
        """Resolve a session stem to its JSONL path, rejecting traversal."""
        if not stem or "/" in stem or ".." in stem or stem.startswith("."):
            return None
        path = Path(SESSIONS_DIR) / f"{stem}.jsonl"
        return path if path.is_file() else None

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

    async def _on_reflex_fired(self, event: str, entry: Any) -> None:
        # entry is the dict pushed onto reflexes.recent: {type, data, ts}.
        await self._broadcast("reflex:fired", entry if isinstance(entry, dict) else {})

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
