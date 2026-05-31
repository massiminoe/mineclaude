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

    async def goToPosition(x: float, z: float, *, y: float | None = None) -> str:
        """Walk to (x, z). y is optional — when omitted the bridge resolves
        the standable y server-side via the heightmap (feet/head clearance,
        non-replaceable floor, closest to your current y). Pin y explicitly
        only when you mean a specific altitude (e.g. a y you read off
        gameState or a known landmark)."""
        return _check(await bridge.goto(x, z, y))

    async def goToPlayer(player: str, distance: int = 3) -> str:
        return _check(await bridge.follow(player, distance))

    async def followPlayer(player: str, distance: int = 3) -> str:
        return _check(await bridge.follow(player, distance))

    async def stop() -> str:
        return _check(await bridge.stop())

    async def placeBlock(block_type: str, x: int, z: int, *, y: int | None = None) -> str:
        """Place a block at (x, z). y is optional — when omitted the bridge
        places at the standable-y cell at this column, i.e. on the ground
        surface. Pin y explicitly when building above ground level (walls,
        roofs) or when the column is a cave (auto-resolve picks the local
        floor closest to your current y)."""
        return _check(await bridge.place(block_type, x, z, y))

    async def getBlock(x: int, y: int, z: int) -> dict:
        """Inspect a single cell. Returns `{block, replaceable}`.

        `block` is the block id with `minecraft:` stripped (e.g. `"air"`,
        `"oak_planks"`, `"grass_block"`). `replaceable` is the vanilla
        `BlockState.isReplaceable()` flag — same predicate `placeBlock`
        uses to decide whether the cell can be overwritten, so a cell with
        `replaceable=True` can be placed into.
        """
        resp = await bridge.get_block(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def getHeightmap(
        x0: int,
        z0: int,
        w: int,
        h: int,
        near_y: int | None = None,
    ) -> dict:
        """Bulk-scan the standable y at every column in `[x0, x0+w) × [z0, z0+h)`.

        One bridge round-trip per call. Returns:
          - `ys`: 2-D list (h rows × w cols) of int OR None — the y the
            player's feet would occupy; None means no standable column was
            found within ±64 of the reference y.
          - `floor`: 2-D list of block-id strings OR None, parallel to `ys`.
          - `near_y`: the reference y used (your current y if you didn't pass
            one). The search picks the standable y closest to this — so
            indoors / underground you get the local floor, not the surface
            40 blocks above.

        Capped at 1024 cells (e.g. 32×32) per call. For "find the flattest
        N×N building footprint" — fetch one heightmap covering all candidate
        origins and reduce in Python; do NOT call this in a nested loop.
        """
        resp = await bridge.heightmap(x0, z0, w, h, near_y)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

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

    def _normalize_chest_items(items: Any) -> list[dict[str, Any]]:
        """Accept either [(name, count_or_all), ...] tuples or [{name, count}, ...]
        dicts. Tuples are the friendlier sandbox shape; dicts match what the
        bridge receives. Either form is fine."""
        out: list[dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, dict):
                out.append({"name": entry["name"], "count": entry.get("count", "all")})
            elif isinstance(entry, (tuple, list)) and len(entry) == 2:
                out.append({"name": entry[0], "count": entry[1]})
            elif isinstance(entry, str):
                out.append({"name": entry, "count": "all"})
            else:
                raise ValueError(
                    f"chest items entry must be (name, count) or {{name, count}}, got {entry!r}"
                )
        return out

    async def chestStore(x: int, y: int, z: int, items: Any) -> dict:
        resp = await bridge.chest_store(x, y, z, _normalize_chest_items(items))
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def chestTake(x: int, y: int, z: int, items: Any) -> dict:
        resp = await bridge.chest_take(x, y, z, _normalize_chest_items(items))
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def chestInspect(x: int, y: int, z: int) -> dict:
        resp = await bridge.chest_inspect(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def equip(item: str, slot: str = "hand") -> str:
        return _check(await bridge.equip(item, slot))

    async def discard(slot: int, count: int = 1) -> str:
        return _check(await bridge.discard(slot, count))

    async def useItem(item: str) -> str:
        """Right-click in air with `item` held — eat food, drink potion,
        throw snowball/egg/ender pearl, cast fishing rod, charge bow.

        The bridge equips the item to mainhand, then fires the use action
        and holds for whatever duration the item needs (read off the item
        itself — food ≈ 1.7s, dried kelp ≈ 0.9s, potion ≈ 1.7s, bow draws
        to max). Instant-use items (snowball, ender pearl, fishing rod,
        loaded crossbow) don't hold at all. Crossbow first call loads,
        next call fires.

        Each call consumes/throws one item. To "eat to full", loop:
        check `getStats()["hunger"]` and call again until satisfied.
        """
        return _check(await bridge.use_item(item))

    async def interact(x: int, y: int, z: int) -> str:
        """Right-click the block at (x, y, z) — open/close doors, press
        buttons, flip levers, toggle trapdoors and fence gates, sleep in
        a bed, play a note block.

        Auto-paths within reach. Fails on air. If the click opens a screen
        (chest/furnace/crafting table) the bridge closes it and returns
        `[partial]` — use the dedicated chest/furnace/craft primitives
        for those instead.
        """
        return _check(await bridge.interact(x, y, z))

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
        "getHeightmap": getHeightmap,
        "getBlock": getBlock,
        "breakBlockAt": breakBlockAt,
        "collectItems": collectItems,
        "attack": attack,
        "craft": craft,
        "furnaceLoad": furnaceLoad,
        "furnaceInspect": furnaceInspect,
        "furnaceExtract": furnaceExtract,
        "chestStore": chestStore,
        "chestTake": chestTake,
        "chestInspect": chestInspect,
        "equip": equip,
        "discard": discard,
        "useItem": useItem,
        "interact": interact,
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
