"""MCP server exposing the Runtime over streamable-HTTP.

Seven tools map onto Runtime methods. The server is stateless + JSON-response so
it co-hosts cleanly with the aiohttp monitor in a single event loop, both
driving ONE shared Runtime instance. That sharing is mandatory, not incidental:
the monitor Console and an MCP `execute` contend for the same single-flight
action slot — two Runtimes over one bridge would be two slots and the exact
race the slot exists to prevent.

`say(message)` is deliberately NOT a tool — talking is something action code
does via the `say()` primitive inside `execute`, not a top-level verb.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from mineclaude.runtime import Runtime

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """Drive a headless Minecraft bot. `execute` runs Python with a
preloaded primitive namespace (movement, mining, crafting, combat, `say(...)`
to talk) and blocks until the action finishes; `get_state` returns a structured
snapshot; `interrupt` is out-of-band and purges the running action. `execute`
is single-flight — for concurrency, run a watcher that polls `get_state` /
`wait_for_event` and calls `interrupt`. Reactions to world events (chat, damage,
lava, ...) are installed with `set_handler`."""


def build_mcp(
    runtime: Runtime,
    *,
    host: str = "127.0.0.1",
    port: int = 5556,
    name: str = "mineclaude",
) -> FastMCP:
    """Build the FastMCP server with the 7 tools wired to `runtime`."""
    mcp = FastMCP(
        name,
        host=host,
        port=port,
        instructions=_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool(
        description=(
            "Run Python with the Minecraft primitive namespace, blocking until "
            "it finishes. Primitives are async (await them); call say('...') to "
            "talk; `return` a value to report a result. Single-flight: returns "
            "status='busy' if an action is already running. Status is one of "
            "completed / failed / cancelled / busy / timeout."
        )
    )
    async def execute(code: str, timeout: float = 300.0) -> dict[str, Any]:
        return asdict(await runtime.execute(code, timeout=timeout))

    @mcp.tool(
        description=(
            "Out-of-band: purge the running/queued action and halt the bridge "
            "(stops Baritone + any attack loop). Always allowed, even mid-execute "
            "— this is how a watcher preempts a worker."
        )
    )
    async def interrupt() -> dict[str, Any]:
        await runtime.interrupt()
        return {"ok": True}

    @mcp.tool(
        description=(
            "Structured world snapshot: player, inventory, equipped, current "
            "action, recent reflex fires, and buffered events. flush=True (the "
            "default) drains the event buffer + world-mutation log into `events`; "
            "flush=False peeks without draining."
        )
    )
    async def get_state(flush: bool = True) -> dict[str, Any]:
        return asdict(await runtime.get_state(flush=flush))

    @mcp.tool(
        structured_output=False,
        description=(
            "Capture the first-person view as an image. Aim with yaw/pitch "
            "(degrees) OR look_at=[x,y,z] (point the eye at a world coord), never "
            "both. Adds ~200ms when aimed."
        ),
    )
    async def screenshot(
        yaw: float | None = None,
        pitch: float | None = None,
        look_at: list[float] | None = None,
    ) -> Image:
        la = (look_at[0], look_at[1], look_at[2]) if look_at else None
        shot = await runtime.screenshot(yaw=yaw, pitch=pitch, look_at=la)
        return Image(data=base64.b64decode(shot.image_base64), format=shot.format)

    @mcp.tool(
        description=(
            "Read the current reaction policy for an event type (e.g. 'chat', "
            "'death', 'damage_taken'): source (default|authored), preempts, "
            "cooldown_s, and the authored code if any."
        )
    )
    def get_handler(event_type: str) -> dict[str, Any]:
        return asdict(runtime.get_handler(event_type))

    @mcp.tool(
        description=(
            "Install an authored reaction body for an event type. `code` is "
            "AST-validated then runs in the sandbox with the primitive namespace "
            "plus `data` (the event payload) and `interrupt`. preempts=True "
            "acquires the action slot before the body runs; otherwise call "
            "interrupt() yourself when the payload warrants it."
        )
    )
    def set_handler(
        event_type: str,
        code: str,
        preempts: bool = False,
        cooldown_s: float = 0.0,
    ) -> dict[str, Any]:
        return asdict(
            runtime.set_handler(event_type, code, preempts=preempts, cooldown_s=cooldown_s)
        )

    @mcp.tool(
        description=(
            "Block until the next event whose type is in `types` (any type if "
            "omitted) is recorded, or `timeout` seconds elapse. Returns "
            "{timed_out, event} where event is {type, data, ts} or null. "
            "Future-only — drain with get_state first, then watch for new events."
        )
    )
    async def wait_for_event(
        types: list[str] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        ev = await runtime.wait_for_event(types, timeout=timeout)
        return {"timed_out": ev is None, "event": asdict(ev) if ev is not None else None}

    return mcp


async def serve(runtime: Runtime, *, host: str = "127.0.0.1", port: int = 5556) -> None:
    """Build + run the MCP server (streamable-HTTP) forever. Awaited as a task
    alongside the monitor and runtime.run_events() in the co-hosted launcher."""
    mcp = build_mcp(runtime, host=host, port=port)
    logger.info("MCP server (streamable-http) listening at http://%s:%d/mcp", host, port)
    await mcp.run_streamable_http_async()
