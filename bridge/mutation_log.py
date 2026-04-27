"""Bridge-side JSONL log of every mutating HTTP operation.

Each entry captures the request body, the response, and a before/after
snapshot of the world state (pulled from WorldCache, so no extra RPC cost).
Used post-hoc to diagnose disagreements between the agent's belief and
actual MC state, correlated with the agent-side session log via timestamp.

Exposed via GET /mutations for live inspection and via
`/tmp/bridge.log.mutations.jsonl` for replay after the bridge exits.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("bridge")

MUTATION_LOG_PATH = Path("/tmp/bridge.log.mutations.jsonl")
MAX_BUFFER = 1000

_lock = threading.Lock()
_ring: list[dict] = []


def _snapshot_status() -> dict:
    """Summarize WorldCache status for the mutation log. No RPC."""
    try:
        from bridge.server import _world_cache
        data = _world_cache.query_status() or {}
    except Exception:
        return {}
    inv_summary: dict[str, int] = {}
    for entry in data.get("inventory") or []:
        name = entry.get("name") or entry.get("item") or "?"
        count = entry.get("count") or 0
        inv_summary[name] = inv_summary.get(name, 0) + count
    return {
        "position": data.get("position"),
        "health": data.get("health"),
        "hunger": data.get("hunger"),
        "inventory": inv_summary,
    }


def _extract_response_body(response: web.Response) -> Any:
    """Try to extract a JSON body from the response for logging."""
    try:
        if response.content_type == "application/json" and response.body:
            return json.loads(bytes(response.body).decode("utf-8"))
    except Exception:
        pass
    return None


def _write(entry: dict) -> None:
    try:
        line = json.dumps(entry, default=repr)
    except Exception as e:
        logger.warning(f"mutation_log: serialize failed: {e}")
        return
    with _lock:
        _ring.append(entry)
        if len(_ring) > MAX_BUFFER:
            del _ring[: len(_ring) - MAX_BUFFER]
    try:
        with MUTATION_LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning(f"mutation_log: write failed: {e}")


def get_mutations(since: float | None = None, limit: int = 100) -> list[dict]:
    """Return mutation entries from the in-memory ring buffer."""
    with _lock:
        buf = list(_ring)
    if since is not None:
        buf = [e for e in buf if e.get("ts", 0) >= since]
    return buf[-limit:]


def log_mutation(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Decorator: record before/after world state around a mutating handler.

    Reads the request JSON body up front (aiohttp caches it, so the handler's
    own `await request.json()` still works). Captures before-status, awaits
    the handler, captures after-status, and writes a single JSONL line.
    """

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        before = _snapshot_status()
        t0 = time.monotonic()
        exc_info: dict | None = None
        response: web.Response | None = None
        try:
            response = await handler(request)
            return response
        except Exception as e:
            exc_info = {"exc": type(e).__name__, "message": str(e)}
            raise
        finally:
            after = _snapshot_status()
            entry = {
                "ts": time.time(),
                "endpoint": request.path,
                "request": body,
                "status_before": before,
                "response": _extract_response_body(response) if response is not None else None,
                "exception": exc_info,
                "status_after": after,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
            }
            _write(entry)

    return wrapper
