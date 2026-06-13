"""Sandbox primitives — async functions exposed to LLM-generated code."""

from __future__ import annotations

import asyncio
import functools
import inspect
import uuid
from typing import Any, Callable, Coroutine

from mineclaude.bridge import BridgeClient, BridgeResponse

# Shared log buffer, cleared before each sandbox execution
_log_buffer: list[str] = []

# Max chars per outgoing chat line. MC chat caps at 256; we split at 240 to
# leave headroom for the `<Name> ` prefix the server prepends.
CHAT_MAX_LEN = 240

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
        gameState or a known landmark).

        Returns where you ACTUALLY ended up, not the target: "Walked to
        (px, py, pz) - <d> from target (tx, ty, tz)" on a real move, or
        "Did not move - already at ... (within arrival range)" when the target
        was already within ~2 blocks (a no-op). So a returned string that names
        coords near your start, or says "Did not move", means you did not
        travel there — read it instead of re-scanning position to confirm."""
        return _check(await bridge.goto(x, z, y))

    async def goToPlayer(player: str, distance: int = 3) -> str:
        """Walk to within `distance` blocks of a named player."""
        return _check(await bridge.follow(player, distance))

    async def followPlayer(player: str, distance: int = 3) -> str:
        """Continuously follow a named player (fire-and-forget; call stop() to end)."""
        return _check(await bridge.follow(player, distance))

    async def stop() -> str:
        """Halt all movement — cancels Baritone pathing / mining / following."""
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

    async def getBlocks(coords: list[tuple[int, int, int]]) -> list[dict]:
        """Batch-inspect many cells in ONE round-trip. Pass a list of
        `(x, y, z)` tuples; get back a list of `{x, y, z, block, replaceable}`
        in the same order — same per-cell shape as `getBlock`.

        This is the scalable form of cell inspection. Each `getBlock` is one
        bridge round-trip and one server tick, so looping it over a coord list
        costs N ticks served serially (the classic 50-cell preflight = ~2.5s+
        of pure wait). `getBlocks` collapses the whole list into a single tick
        and a single round-trip. Reach for it whenever you'd otherwise write
        `for c in coords: await getBlock(*c)` — build-footprint preflight,
        re-checking a set of known ore/coords, verifying a wall is clear, etc.

        Capped at 4096 coords per call. For a contiguous ground sweep prefer
        `getHeightmap`; for a radius scan around the player prefer
        `getNearbyBlocks` / `findBlocks` — those already read in one tick too.
        """
        resp = await bridge.get_blocks(list(coords))
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data["blocks"]

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
        """Mine/break the block at (x, y, z). Self-navigates within reach
        (Baritone, ~15s budget) — don't goToPosition first.

        Auto-selects a tool that can harvest the block before swinging, so you
        don't have to equip one first and a stray torch/block left in hand by a
        prior place/use won't make you mine bare-handed. It picks the BEST
        suitable tool (highest tier → fastest), e.g. your diamond pickaxe over
        a stone one. To be conservative and spare a premium tool, equip the
        cheaper one yourself: a tool you already hold that can harvest the block
        is KEPT, never overridden. If you own no tool that can harvest the
        block, it mines bare-handed (slow, and stone/ore drop nothing)."""
        return _check(await bridge.break_block(x, y, z))

    async def collectItems(radius: float = 6) -> str:
        """Walk to and pick up dropped item entities within `radius`. Call after
        breaking blocks or killing mobs; bump radius to ~10 after a mining run."""
        return _check(await bridge.collect(radius))

    async def attack(entity_id: int | str) -> str:
        """Fight the entity with this numeric id to the death — loops swings,
        auto-paths into melee, ~30s cap. Get ids from getNearbyEntities /
        findEntities. One call per kill, not per swing. Equip a sword first."""
        return _check(await bridge.attack(str(entity_id)))

    async def craft(item: str, count: int = 1) -> str:
        """Craft `count` of the OUTPUT item (not iterations/inputs); returns the
        amount actually produced — read it. 3x3 recipes auto-locate a nearby
        crafting table; only place one if craft fails with 'no crafting table'."""
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
        """Load a nearby furnace's input + fuel slots and start smelting. Returns
        immediately — does NOT wait for the cook (compute fuel yourself: coal=8
        items, planks/logs=1.5, round up). Auto-walks to the furnace."""
        return _check(await bridge.furnace_load(
            input_item, input_count, fuel_item, fuel_count, x, y, z,
        ))

    async def furnaceInspect(
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict:
        """Read furnace state without modifying it: {position, lit, input, fuel,
        output}. Smelting takes ~10s/item; sleep the cook time, then poll."""
        resp = await bridge.furnace_inspect(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def furnaceExtract(
        x: int | None = None,
        y: int | None = None,
        z: int | None = None,
    ) -> dict:
        """Pull everything from the furnace (output, then leftover input + fuel):
        {output, input_left, fuel_left}. Calling mid-cook aborts the cook."""
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
        """Deposit into the chest at (x, y, z). `items` is [(name, count|'all'), ...].
        Coords required. Returns {stored, skipped} — partial success is the shape,
        not an error. Auto-walks to the chest."""
        resp = await bridge.chest_store(x, y, z, _normalize_chest_items(items))
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def chestTake(x: int, y: int, z: int, items: Any) -> dict:
        """Withdraw from the chest at (x, y, z) into inventory. Same `items` shape
        as chestStore; returns {taken, skipped}."""
        resp = await bridge.chest_take(x, y, z, _normalize_chest_items(items))
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def chestInspect(x: int, y: int, z: int) -> dict:
        """Read chest contents without modifying: {size, slots, totals}. Use
        `totals` for 'do I have N of X here?'."""
        resp = await bridge.chest_inspect(x, y, z)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        return resp.data

    async def equip(item: str, slot: str = "hand") -> str:
        """Equip an item to hand (default) or an armor slot. Equip the right tool
        BEFORE mining/fighting — placing/eating swaps your hand off the tool."""
        return _check(await bridge.equip(item, slot))

    async def discard(slot: int, count: int = 1) -> str:
        """Drop `count` items from PI slot (0..8 hotbar, 9..35 main inventory).
        Find the slot via getInventory(). Armor/offhand aren't discardable."""
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

        To sleep in a bed use sleepInBed() instead — a raw interact() can't
        tell whether you actually fell asleep or skipped the night.
        """
        return _check(await bridge.interact(x, y, z))

    async def sleepInBed(x: int, y: int, z: int, wait_s: float | None = None) -> dict:
        """Sleep in the bed at (x, y, z) — the right way to skip a night.

        Confirms you actually fell asleep (a daytime / monsters-nearby /
        obstructed bed fails loudly with the reason), then blocks until you
        wake. Returns {slept, night_skipped, time}: `night_skipped` is True
        only when you wake into morning. If the night doesn't pass within
        `wait_s` (default 20s) — another player awake, or the server's
        playersSleepingPercentage gamerule not met — it leaves the bed and
        returns night_skipped=False rather than hanging.

        Auto-paths within reach. Raises on a hard failure (not a bed, couldn't
        reach it, couldn't fall asleep).
        """
        resp = await bridge.sleep_in_bed(x, y, z, wait_s=wait_s)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        data = dict(resp.data)
        data["message"] = resp.message
        return data

    async def use(
        item: str | None = None,
        *,
        look_at: tuple[float, float, float] | None = None,
        hold_ms: int | None = None,
    ) -> dict:
        """Unified right-click — the one primitive behind every "use an item"
        interaction (it's what a player does pressing the use key).

        Forms:
          - use("bread")                               -> eat (use in air)
          - use("water_bucket", look_at=(x,y,z))       -> pour/place at the aim
          - use("bucket", look_at=(wx,wy,wz))          -> fill from a water/lava source
          - use("torch", look_at=(x+0.5,y+1,z+0.5))    -> place on the face you look at
          - use("flint_and_steel", look_at=(x,y,z))    -> light fire on the hit face
          - use(look_at=(x,y,z))                        -> right-click whatever's held / empty hand

        `look_at` aims the eye at that exact world point and dispatches on the
        REAL raycast: if the ray hits a block it tries interactBlock (door,
        torch, flint & steel, any BlockItem); otherwise it falls through to
        item-use (buckets fill/pour, food). Omit `look_at` for a pure in-air
        use. Auto-navigates within reach; equips `item` first if given.

        To put fire/torch INSIDE or on a specific face, aim at a point on that
        face (e.g. a top-row portal block from the ground gives a downward face
        so fire lands in the interior). Buckets: aim at the source's centre to
        fill, at the target block to pour.

        Returns a dict: `used` (did anything happen), `dispatch` ("block" or
        "item"), `hit` ({block,x,y,z,face}) when a block was struck, and
        `inventory_delta` (e.g. {"water_bucket": 1, "bucket": -1} after a
        fill). Raises only on hard failures (item missing, navigation failed);
        a no-op (aim missed / not usable here) returns `used: False` rather
        than raising — check it.
        """
        resp = await bridge.use(item, look_at=look_at, hold_ms=hold_ms)
        if resp.status == "error":
            raise RuntimeError(resp.message)
        data = dict(resp.data)
        data["message"] = resp.message
        return data

    async def getStats() -> dict:
        """Return {health, hunger, position:{x,y,z}, biome, time}. Position is a
        NESTED dict — use stats['position']['x']."""
        resp = await bridge.get_status()
        return resp.data

    async def getInventory() -> list[dict]:
        """Return the inventory as a list of {name, count, slot}. Tools + armor
        also carry durability:{remaining, max}."""
        resp = await bridge.get_status()
        return resp.data.get("inventory", [])

    async def getNearbyBlocks(range_: int = 16) -> list[dict]:
        """Return blocks within `range_` as {name, x, y, z, distance}, nearest first."""
        resp = await bridge.get_nearby_blocks(range_)
        return resp.data.get("blocks", [])

    async def getNearbyEntities(range_: int = 32) -> list[dict]:
        """Return entities within `range_` as {id, name, type, x, y, z, health, distance}.
        Pass an entity's `id` to attack()."""
        resp = await bridge.get_nearby_entities(range_)
        return resp.data.get("entities", [])

    async def findBlocks(block_type: str, range_: int = 32, count: int = 10) -> list[dict]:
        """Find up to `count` of one block type within `range_` (max 64), nearest first."""
        resp = await bridge.get_nearby_blocks(range_, block_types=[block_type])
        blocks = resp.data.get("blocks", [])
        return blocks[:count]

    async def findMultipleBlocks(block_types: list[str], range_: int = 32, count: int = 10) -> dict[str, list[dict]]:
        """Find several block types in one scan (max range 64). Returns {type: [blocks]}."""
        resp = await bridge.get_nearby_blocks(range_, block_types=block_types)
        blocks = resp.data.get("blocks", [])
        result: dict[str, list[dict]] = {t: [] for t in block_types}
        for b in blocks:
            name = b["name"]
            if name in result and len(result[name]) < count:
                result[name].append(b)
        return result

    async def findEntities(entity_type: str, range_: int = 32) -> list[dict]:
        """Find entities matching a type/name within `range_`. Case-insensitive
        and `minecraft:`-prefix tolerant: "Sheep", "sheep", and "minecraft:sheep"
        all match the sheep whose type is "sheep"/name is "Sheep"."""
        resp = await bridge.get_nearby_entities(range_)
        entities = resp.data.get("entities", [])
        needle = entity_type.removeprefix("minecraft:").casefold()
        return [
            e for e in entities
            if str(e.get("type", "")).removeprefix("minecraft:").casefold() == needle
            or str(e.get("name", "")).casefold() == needle
        ]

    async def say(message: str) -> None:
        """Send a chat message in-game. Splits long text at the 240-char limit,
        preferring a word boundary. The talking primitive — chat is no longer a
        top-level tool; an agent narrates / replies by calling say() inside
        execute()."""
        text = str(message).strip()
        while text:
            if len(text) <= CHAT_MAX_LEN:
                await bridge.chat(text)
                break
            split_at = text.rfind(" ", 0, CHAT_MAX_LEN)
            if split_at == -1:
                split_at = CHAT_MAX_LEN
            await bridge.chat(text[:split_at])
            text = text[split_at:].lstrip()

    async def sleep(seconds: float) -> None:
        """Wait `seconds` (async). Use to let a furnace cook or a mob settle."""
        await asyncio.sleep(seconds)

    def log(message: str) -> None:
        """Append a message to the action's output log (also returned in the result).
        `print(...)` works too. The only non-async primitive."""
        _log_buffer.append(str(message))

    primitives = {
        "goToPosition": goToPosition,
        "goToPlayer": goToPlayer,
        "followPlayer": followPlayer,
        "stop": stop,
        "placeBlock": placeBlock,
        "getHeightmap": getHeightmap,
        "getBlock": getBlock,
        "getBlocks": getBlocks,
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
        "sleepInBed": sleepInBed,
        "use": use,
        "getStats": getStats,
        "getInventory": getInventory,
        "getNearbyBlocks": getNearbyBlocks,
        "getNearbyEntities": getNearbyEntities,
        "findBlocks": findBlocks,
        "findMultipleBlocks": findMultipleBlocks,
        "findEntities": findEntities,
        "say": say,
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
