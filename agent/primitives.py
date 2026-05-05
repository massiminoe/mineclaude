"""Sandbox primitives — async functions exposed to LLM-generated code."""

from __future__ import annotations

import asyncio
import functools
import inspect
import uuid
from typing import Any, Callable, Coroutine

from agent.bridge import BridgeClient, BridgeResponse

# Shared log buffer, cleared before each sandbox execution
_log_buffer: list[str] = []

# Type for sub-action callback
SubActionCallback = Callable[..., Coroutine[Any, Any, None]]


def _summarize_args(fn: Any, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Convert positional args to a named dict using the function's signature."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.keys())
    summary: dict[str, Any] = {}
    for i, val in enumerate(args):
        key = params[i] if i < len(params) else f"arg{i}"
        # Truncate large results (like block lists)
        if isinstance(val, list) and len(val) > 3:
            summary[key] = f"[{len(val)} items]"
        elif isinstance(val, str) and len(val) > 80:
            summary[key] = val[:80] + "..."
        else:
            summary[key] = val
    for key, val in kwargs.items():
        summary[key] = val
    return summary


def _wrap(name: str, fn: Any, on_subaction: SubActionCallback) -> Any:
    """Wrap an async primitive to emit sub-action events."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        sub_id = uuid.uuid4().hex[:8]
        summarized = _summarize_args(fn, args, kwargs)
        await on_subaction(sub_id, name, summarized, "started")
        try:
            result = await fn(*args, **kwargs)
            await on_subaction(sub_id, name, None, "completed", result=result)
            return result
        except Exception as e:
            await on_subaction(sub_id, name, None, "failed", error=str(e))
            raise
    return wrapper


def _check(resp: BridgeResponse) -> str:
    """Raise on error responses so sandbox code stops on failure.

    Partial successes are surfaced with a `[partial]` prefix so Claude can
    tell when a craft/smelt delivered fewer than requested, or when a place
    succeeded without verification.
    """
    if resp.status == "error":
        raise RuntimeError(resp.message)
    if resp.status == "partial":
        return f"[partial] {resp.message}"
    return resp.message


def make_primitives(
    bridge: BridgeClient,
    on_subaction: SubActionCallback | None = None,
) -> dict[str, Any]:
    """Create a dict of name → async callable primitives, closed over bridge."""

    async def goToPosition(x: float, y: float, z: float) -> str:
        return _check(await bridge.goto(x, y, z))

    async def goToPlayer(player: str, distance: int = 3) -> str:
        return _check(await bridge.follow(player, distance))

    async def followPlayer(player: str, distance: int = 3) -> str:
        return _check(await bridge.follow(player, distance))

    async def stop() -> str:
        return _check(await bridge.stop())

    async def placeBlock(block_type: str, x: int, y: int, z: int, face: str = "top") -> str:
        return _check(await bridge.place(block_type, x, y, z, face))

    async def breakBlockAt(x: int, y: int, z: int) -> str:
        return _check(await bridge.break_block(x, y, z))

    async def collectItems(radius: float = 6) -> str:
        return _check(await bridge.collect(radius))

    async def attack(entity_id: int | str) -> str:
        return _check(await bridge.attack(str(entity_id)))

    async def craft(item: str, count: int = 1) -> str:
        return _check(await bridge.craft(item, count))

    async def furnaceLoad(
        input_item: str,
        input_count: int,
        fuel_item: str,
        fuel_count: int,
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> str:
        return _check(await bridge.furnace_load(
            input_item, input_count, fuel_item, fuel_count, x, y, z,
        ))

    async def furnaceInspect(
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict:
        resp = await bridge.furnace_inspect(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def furnaceExtract(
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict:
        resp = await bridge.furnace_extract(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def equip(item: str, slot: str = "hand") -> str:
        return _check(await bridge.equip(item, slot))

    async def discard(item: str, count: int = 1) -> str:
        return _check(await bridge.discard(item, count))

    async def getStats() -> dict:
        resp = await bridge.get_status()
        return resp.data

    async def getInventory() -> list[dict]:
        resp = await bridge.get_status()
        return resp.data.get("inventory", [])

    async def getNearbyBlocks(range_: int = 16) -> list[dict]:
        resp = await bridge.get_nearby_blocks(range_)
        return resp.data.get("blocks", [])

    async def getNearbyEntities(range_: int = 32) -> list[dict]:
        resp = await bridge.get_nearby_entities(range_)
        return resp.data.get("entities", [])

    async def findBlocks(block_type: str, range_: int = 32, count: int = 10) -> list[dict]:
        resp = await bridge.get_nearby_blocks(range_, block_types=[block_type])
        blocks = resp.data.get("blocks", [])
        return blocks[:count]

    async def findMultipleBlocks(block_types: list[str], range_: int = 32, count: int = 10) -> dict[str, list[dict]]:
        resp = await bridge.get_nearby_blocks(range_, block_types=block_types)
        blocks = resp.data.get("blocks", [])
        result: dict[str, list[dict]] = {t: [] for t in block_types}
        for b in blocks:
            name = b["name"]
            if name in result and len(result[name]) < count:
                result[name].append(b)
        return result

    async def findEntities(entity_type: str, range_: int = 32) -> list[dict]:
        resp = await bridge.get_nearby_entities(range_)
        entities = resp.data.get("entities", [])
        return [e for e in entities if e["type"] == entity_type or e["name"] == entity_type]

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def log(message: str) -> None:
        _log_buffer.append(str(message))

    primitives = {
        "goToPosition": goToPosition,
        "goToPlayer": goToPlayer,
        "followPlayer": followPlayer,
        "stop": stop,
        "placeBlock": placeBlock,
        "breakBlockAt": breakBlockAt,
        "collectItems": collectItems,
        "attack": attack,
        "craft": craft,
        "furnaceLoad": furnaceLoad,
        "furnaceInspect": furnaceInspect,
        "furnaceExtract": furnaceExtract,
        "equip": equip,
        "discard": discard,
        "getStats": getStats,
        "getInventory": getInventory,
        "getNearbyBlocks": getNearbyBlocks,
        "getNearbyEntities": getNearbyEntities,
        "findBlocks": findBlocks,
        "findMultipleBlocks": findMultipleBlocks,
        "findEntities": findEntities,
        "sleep": sleep,
        "log": log,
    }

    # Wrap async primitives with sub-action tracking (skip log and sleep)
    if on_subaction is not None:
        skip = {"log", "sleep"}
        for name, fn in primitives.items():
            if name not in skip and asyncio.iscoroutinefunction(fn):
                primitives[name] = _wrap(name, fn, on_subaction)

    return primitives
