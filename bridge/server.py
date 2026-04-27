"""HTTP + WebSocket bridge server running inside Minescript's Python runtime.

Single asyncio process on port 8080 using aiohttp.
All blocking minescript.* calls dispatched via run_in_executor().
Chat events polled from Minescript and broadcast to WS clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from aiohttp import web

import re

from bridge import minescript_api, baritone, screenshot as screenshot_mod
from bridge.minescript_api import _ms, _ms_lock, RPCTimeout
from bridge.mutation_log import log_mutation, get_mutations
from bridge.recipes import get_recipe, get_required_ingredients
from bridge.world_cache import WorldCache

logger = logging.getLogger("bridge")
_log_handler = logging.FileHandler("/tmp/bridge.log")
_log_handler.setFormatter(logging.Formatter("[bridge] %(asctime)s %(message)s"))
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)

# Single worker — every minescript.* call goes through _ms_lock anyway, so
# adding a second thread only increases contention and load on Minescript's
# stdin channel (which races under sustained pressure).
_executor = ThreadPoolExecutor(max_workers=1)
_ws_clients: set[web.WebSocketResponse] = set()

# Background world state cache — populated by its own scanner thread, read
# by GET handlers. See bridge/world_cache.py for the accuracy model.
_world_cache: WorldCache = WorldCache()

# Count of in-flight `_run()` calls (i.e. commands currently holding the
# executor thread). Read by the WorldCache scanner so it can skip its
# chunked block re-scan while a foreground command is running — keeps the
# big scan from queueing up behind a user command and adding tail latency.
# Protected by a plain Lock because `+= 1` isn't atomic and we need
# consistent reads from the scanner thread.
_executor_busy: int = 0
_executor_busy_lock = threading.Lock()


def _ok(data: dict | list | None = None, message: str = "ok") -> dict:
    return {"status": "success", "message": message, "data": data or {}}


def _err(message: str) -> dict:
    return {"status": "error", "message": message, "data": {}}


def _partial(data: dict | list | None = None, message: str = "partial") -> dict:
    """Response for operations that partially succeeded.

    Use when the agent asked for N but we delivered M<N, or when an action
    completed but couldn't be verified. The agent-side tool dispatcher turns
    this into a `[status=partial ...]` header so Claude can react rather than
    falsely assume full success.
    """
    return {"status": "partial", "message": message, "data": data or {}}


async def _run(fn, *args):
    """Run a blocking function in the thread pool.

    Tracks `_executor_busy` so the WorldCache scanner can yield to
    foreground commands. Any `RPCTimeout` raised inside `fn` propagates
    out — the `@web.middleware` catches it and returns a clean 503.
    """
    global _executor_busy
    with _executor_busy_lock:
        _executor_busy += 1
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, partial(fn, *args))
    finally:
        with _executor_busy_lock:
            _executor_busy -= 1


@web.middleware
async def rpc_timeout_middleware(request: web.Request, handler):
    """Translate RPCTimeout from any handler into a 503 with a clean body.

    With A1 framing in place RPCTimeout indicates a genuine Java-side wedge
    (mod hang, MC freeze, GC pause beyond budget) — rare and worth
    investigating. 503 lets the agent's BridgeClient surface it distinctly
    from a generic 500/traceback.
    """
    try:
        return await handler(request)
    except RPCTimeout as e:
        logger.error(f"RPCTimeout during {request.method} {request.path}: {e}")
        return web.json_response(
            _err(f"Minescript RPC timed out: {e}"),
            status=503,
        )


# ---------------------------------------------------------------------------
# HTTP route handlers
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    """Diagnostic snapshot of the Minescript RPC channel state.

    Returns 200 + healthy body when no RPC is obviously stuck, 503 when a
    single _ms() call has been in flight longer than 2 × RPC_TIMEOUT_S
    (which shouldn't happen in the healthy state — _ms itself would have
    timed out and raised RPCTimeout by then — so 503 here means something
    bypassed the normal wrapper or the wrapper itself is broken).

    Exposes the RPC timeout counter for at-a-glance observability. With A1
    framing the counter should stay at 0 in normal operation; any nonzero
    value points at a real Java-side wedge worth investigating.
    Never touches _ms_lock, so it's always answerable even when the
    executor path is wedged.
    """
    now = time.monotonic()
    start = minescript_api._last_rpc_start_ts
    end = minescript_api._last_rpc_end_ts
    busy = (start > end) if start else False
    in_flight_s = (now - start) if busy else 0.0
    stuck = in_flight_s > (minescript_api.RPC_TIMEOUT_S * 2)

    last_timeout_ts = minescript_api._last_rpc_timeout_ts
    last_timeout_ago = (now - last_timeout_ts) if last_timeout_ts else None

    data = {
        "rpc_timeouts_total": minescript_api._rpc_timeouts_total,
        "last_timeout_fn": minescript_api._last_rpc_timeout_fn or None,
        "last_timeout_s_ago": round(last_timeout_ago, 1) if last_timeout_ago is not None else None,
        "current_rpc_fn": minescript_api._last_rpc_fn or None,
        "current_rpc_thread": minescript_api._last_rpc_thread or None,
        "current_rpc_in_flight_s": round(in_flight_s, 2),
        "executor_busy": _executor_busy,
        "stuck": stuck,
    }

    if stuck:
        return web.json_response(
            {"status": "error", "message": "RPC channel stuck", "data": data},
            status=503,
        )
    return web.json_response(_ok(data, "healthy"))


async def handle_status(request: web.Request) -> web.Response:
    # Fast path: cache (updated every 2s by scanner thread)
    data = _world_cache.query_status()
    if data is None:
        # Cold start: scanner hasn't populated yet. Do a one-shot live read.
        data = await _run(minescript_api.get_player_status)
    return web.json_response(_ok(data, "Status retrieved"))


async def handle_nearby_blocks(request: web.Request) -> web.Response:
    radius = int(request.query.get("r", 8))
    types_raw = request.query.get("types", "")
    block_types = [t.strip() for t in types_raw.split(",") if t.strip()] or None
    # Fast path: cache (pure dict filter, no RPC)
    blocks = _world_cache.query_blocks(radius, block_types)
    if blocks is None:
        # Cold start: scanner hasn't populated yet. Do a one-shot live scan.
        blocks = await _run(minescript_api.get_nearby_blocks, radius, block_types)
    return web.json_response(_ok({"blocks": blocks}, f"Found {len(blocks)} blocks"))


async def handle_nearby_entities(request: web.Request) -> web.Response:
    radius = int(request.query.get("r", 32))
    # Fast path: cache (updated every 2s by scanner thread)
    entities = _world_cache.query_entities(radius)
    if entities is None:
        # Cold start: scanner hasn't populated yet. Do a one-shot live read.
        entities = await _run(minescript_api.get_nearby_entities, radius)
    return web.json_response(_ok({"entities": entities}, f"Found {len(entities)} entities"))


@log_mutation
async def handle_goto(request: web.Request) -> web.Response:
    body = await request.json()
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    timeout = body.get("timeout", 60)
    result = await _run(minescript_api.goto_and_wait, x, y, z, timeout)
    # Position and surrounding blocks have changed — force both to re-scan.
    _world_cache.invalidate_blocks()
    _world_cache.invalidate_status()
    if result.get("arrived"):
        return web.json_response(_ok(result, f"Arrived at {x}, {y}, {z}"))
    return web.json_response(_err(result.get("error", f"Failed to reach {x}, {y}, {z}")))


@log_mutation
async def handle_mine(request: web.Request) -> web.Response:
    body = await request.json()
    block = body.get("block", "")
    count = body.get("count", 0)
    if not block:
        return web.json_response(_err("Missing 'block' parameter"), status=400)
    cmd = baritone.mine(block, count)
    await _run(minescript_api.send_chat, cmd)
    # Baritone is async — it will break blocks and move the player in the
    # background. Invalidate so the scanner picks up the changes ASAP rather
    # than serving 20s-stale data while Baritone is working.
    _world_cache.invalidate_blocks()
    _world_cache.invalidate_status()
    return web.json_response(_ok({"command": cmd}, f"Mining {count} {block}" if count else f"Mining {block}"))


@log_mutation
async def handle_follow(request: web.Request) -> web.Response:
    body = await request.json()
    player = body.get("player", "")
    if not player:
        return web.json_response(_err("Missing 'player' parameter"), status=400)
    cmd = baritone.follow(player)
    await _run(minescript_api.send_chat, cmd)
    _world_cache.invalidate_blocks()
    _world_cache.invalidate_status()
    return web.json_response(_ok({"command": cmd}, f"Following {player}"))


async def handle_explore(request: web.Request) -> web.Response:
    cmd = baritone.explore()
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, "Exploring"))


async def handle_stop(request: web.Request) -> web.Response:
    cmd = baritone.stop()
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, "Stopped"))


@log_mutation
async def handle_place(request: web.Request) -> web.Response:
    body = await request.json()
    block = body.get("block", "")
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    face = body.get("face", "top")
    if not block:
        return web.json_response(_err("Missing 'block' parameter"), status=400)
    result = await _run(minescript_api.place_block, block, x, y, z, face)
    if result.get("placed"):
        # Write-through: block cache updated instantly, status force-refreshed
        # synchronously so the inventory decrement lands before the next query.
        _world_cache.on_block_placed(block, int(x), int(y), int(z))
        await _run(_world_cache.force_refresh_status)
        msg = f"Placed {block} at {x}, {y}, {z}"
        # Tolerant success (getblock verify errored) → agent should know this
        # placement isn't confirmed, so tag it as partial.
        if result.get("verified") is False:
            return web.json_response(_partial(result, msg + " (unverified)"))
        return web.json_response(_ok(result, msg))
    return web.json_response(_err(result.get("error", "Failed to place block")))


@log_mutation
async def handle_break(request: web.Request) -> web.Response:
    body = await request.json()
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    result = await _run(minescript_api.break_block, x, y, z)
    if result.get("broken"):
        # Idempotent no-op (block was already air): cache may still think the
        # block is present from a stale scan, so clear it. No durability or
        # drops to account for — skip force_refresh_status.
        if result.get("already_gone"):
            _world_cache.on_block_broken(int(x), int(y), int(z))
            return web.json_response(_ok(
                result,
                f"Block at {x}, {y}, {z} was already air (no-op)",
            ))
        # Write-through removes the block; force_refresh_status picks up tool
        # durability loss; entities cache refresh picks up the dropped item.
        _world_cache.on_block_broken(int(x), int(y), int(z))
        _world_cache.invalidate_entities()
        await _run(_world_cache.force_refresh_status)
        return web.json_response(_ok(result, f"Broke block at {x}, {y}, {z}"))
    return web.json_response(_err(result.get("error", "Failed to break block")))


@log_mutation
async def handle_collect(request: web.Request) -> web.Response:
    body = await request.json()
    radius = float(body.get("radius", 3))
    result = await _run(minescript_api.collect_items, radius)
    count = result.get("collected", 0)
    # Picked-up items mutate inventory (status) and remove item entities.
    # Force-refresh status synchronously — `/collect` also captures auto-pickups
    # that happened during Baritone travel for preceding breaks, which didn't
    # fire force_refresh at the moment of pickup. Without this, the cache can
    # be 2s behind (next scanner tick) and the next iteration's gameState
    # underreports inventory — Claude then re-does work it already completed.
    _world_cache.invalidate_entities()
    await _run(_world_cache.force_refresh_status)
    msg = f"Collected {count} item(s)" if count else "No items to collect"
    return web.json_response(_ok(result, msg))


@log_mutation
async def handle_attack(request: web.Request) -> web.Response:
    body = await request.json()
    entity_id = body.get("entity_id", "")
    if not entity_id:
        return web.json_response(_err("Missing 'entity_id' parameter"), status=400)
    result = await _run(minescript_api.attack_entity, entity_id)
    # Entity health changed (and we may have damaged a tool → durability).
    _world_cache.invalidate_entities()
    _world_cache.invalidate_status()
    if result.get("attacked"):
        return web.json_response(_ok(result, f"Attacked {entity_id}"))
    return web.json_response(_err(result.get("error", "Failed to attack")))


def _display_ingredient(name: str) -> str:
    """Collapse a canonical ingredient name to its generic form.

    Recipes key on a single representative variant (e.g. `oak_planks`), but
    actually accept any variant with the matching suffix. Reporting "used 3
    oak_planks" when the bot actually consumed spruce_planks is misleading,
    so we display the generic (`planks`) when the ingredient is variant-keyed.
    """
    from bridge.recipes import VARIANT_SUFFIXES
    suffix = VARIANT_SUFFIXES.get(name)
    return suffix.lstrip("_") if suffix else name


def _format_craft_message(item: str, requested: int, crafted: int) -> str:
    """Build the agent-facing success message for /craft.

    Reports actual production (not the requested count) so the agent can
    self-correct when its mental model of `count` was wrong, and lists crafts
    run + ingredients consumed when known.
    """
    clean_item = item.replace("minecraft:", "")
    msg = f"Crafted {crafted} {clean_item}"
    recipe = get_recipe(clean_item)
    if recipe is not None:
        crafts_run = crafted // recipe.output_count
        ingredients = get_required_ingredients(clean_item, crafted) or {}
        used_str = ", ".join(f"{n} {_display_ingredient(ing)}" for ing, n in ingredients.items())
        detail_parts = []
        if crafts_run > 1:
            detail_parts.append(f"{crafts_run} crafts")
        if used_str:
            detail_parts.append(f"used {used_str}")
        if detail_parts:
            msg += f" ({', '.join(detail_parts)})"
    if crafted < requested:
        msg += f" — wanted {requested} but ran out of ingredients"
    return msg


@log_mutation
async def handle_craft(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    count = body.get("count", 1)
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.craft_item, item, count)
    # Force-refresh status synchronously so the very next inventory/status
    # query (likely Claude's next iteration) sees the crafted output.
    await _run(_world_cache.force_refresh_status)
    crafted = result.get("crafted", 0)
    enriched = {**result, "requested": count, "actual": crafted}
    if crafted == 0:
        return web.json_response(_err(result.get("error", "Failed to craft")))
    msg = _format_craft_message(item, count, crafted)
    if crafted < count:
        return web.json_response(_partial(enriched, msg))
    return web.json_response(_ok(enriched, msg))


@log_mutation
async def handle_smelt(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    count = body.get("count", 1)
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.smelt_item, item, count)
    await _run(_world_cache.force_refresh_status)
    smelted = result.get("smelted", 0)
    enriched = {**result, "requested": count, "actual": smelted}
    if smelted == 0:
        return web.json_response(_err(result.get("error", "Failed to smelt")))
    msg = f"Smelted {smelted} {item}"
    if smelted < count:
        return web.json_response(_partial(enriched, msg + f" (wanted {count})"))
    return web.json_response(_ok(enriched, msg))


@log_mutation
async def handle_equip(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    slot = body.get("slot", "hand")
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.equip_item, item, slot)
    await _run(_world_cache.force_refresh_status)
    if result.get("equipped"):
        return web.json_response(_ok(result, f"Equipped {item} to {slot}"))
    return web.json_response(_err(result.get("error", "Failed to equip")))


@log_mutation
async def handle_discard(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    count = body.get("count", 1)
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.discard_item, item, count)
    await _run(_world_cache.force_refresh_status)
    if result.get("discarded", 0) > 0:
        return web.json_response(_ok(result, f"Discarded {count} {item}"))
    return web.json_response(_err(result.get("error", "Failed to discard")))


def _dump_open_menu_slots() -> dict:
    """Dump container_get_info + every reported slot of the currently-open menu."""
    import minescript
    out: dict = {}
    try:
        out["screen_name"] = minescript.screen_name()
    except Exception as e:
        out["screen_name_error"] = str(e)
    try:
        info = minescript.container_get_info()
        # ContainerInfo is a dataclass — extract its fields generically
        info_dict: dict = {}
        for attr in ("type", "title", "size", "rows", "cols"):
            if hasattr(info, attr):
                info_dict[attr] = getattr(info, attr)
        out["container_info"] = info_dict or repr(info)
    except Exception as e:
        out["container_info_error"] = str(e)
    try:
        items = minescript.container_get_items()
        slot_map: list[dict] = []
        for ci in items:
            slot = getattr(ci, "slot", None)
            name = getattr(ci, "item", None)
            count = getattr(ci, "count", None)
            slot_map.append({"slot": slot, "item": name, "count": count})
        out["slots"] = slot_map
        out["slot_count"] = len(slot_map)
    except Exception as e:
        out["slots_error"] = str(e)
    return out


async def handle_probe(request: web.Request) -> web.Response:
    """Run API probe and return results — tests which minescript APIs exist.

    Optional query params for one-shot menu inspection (used to verify slot
    layouts before relying on them in real-crafting code):

      ?inventory=1            — open the player inventory screen and dump slots
      ?craftingtable=x,y,z    — container_open() the table at those coords and dump
    """
    import inspect

    inv_probe = request.query.get("inventory", "").lower() in ("1", "true", "yes")
    table_probe_raw = request.query.get("craftingtable", "")

    def _probe():
        import minescript
        results = {"player_control": {}, "container": {}, "capabilities": {}, "tests": {}}

        for name in [
            "player_set_orientation", "player_orientation",
            "player_press_attack", "player_press_use", "player_press_drop",
            "player_select_slot", "player_press_forward", "player_press_backward",
            "player_press_left", "player_press_right", "player_press_jump",
            "player_press_sneak", "player_press_sprint",
        ]:
            exists = hasattr(minescript, name)
            sig = ""
            if exists:
                try:
                    sig = str(inspect.signature(getattr(minescript, name)))
                except (ValueError, TypeError):
                    sig = "(?)"
            results["player_control"][name] = {"exists": exists, "signature": sig}

        for name in [
            "screen_name", "container_get_items", "container_click",
            "close_screen", "player_press_inventory", "open_inventory",
        ]:
            exists = hasattr(minescript, name)
            sig = ""
            if exists:
                try:
                    sig = str(inspect.signature(getattr(minescript, name)))
                except (ValueError, TypeError):
                    sig = "(?)"
            results["container"][name] = {"exists": exists, "signature": sig}

        # All public APIs
        results["all_apis"] = []
        for name in sorted(dir(minescript)):
            if name.startswith("_"):
                continue
            obj = getattr(minescript, name)
            if callable(obj):
                try:
                    sig = str(inspect.signature(obj))
                except (ValueError, TypeError):
                    sig = "(?)"
                results["all_apis"].append({"name": name, "signature": sig})

        # Safe read-only tests
        try:
            yaw, pitch = _ms(minescript.player_orientation)
            results["tests"]["player_orientation()"] = f"ok: yaw={yaw:.1f}, pitch={pitch:.1f}"
        except Exception as e:
            results["tests"]["player_orientation()"] = str(e)

        try:
            result = _ms(minescript.player_look_at, 0.0, 64.0, 0.0)
            results["tests"]["player_look_at(0,64,0)"] = f"ok: {result}"
        except Exception as e:
            results["tests"]["player_look_at(0,64,0)"] = str(e)

        try:
            _ms(minescript.player_inventory_select_slot, 0)
            results["tests"]["player_inventory_select_slot(0)"] = "ok"
        except Exception as e:
            results["tests"]["player_inventory_select_slot(0)"] = str(e)

        pc = results["player_control"]
        ct = results["container"]
        results["capabilities"] = {
            "break_block": pc.get("player_set_orientation", {}).get("exists") and pc.get("player_press_attack", {}).get("exists"),
            "place_block": pc.get("player_set_orientation", {}).get("exists") and pc.get("player_press_use", {}).get("exists"),
            "attack_entity": pc.get("player_set_orientation", {}).get("exists") and pc.get("player_press_attack", {}).get("exists"),
            "craft_item": ct.get("container_click", {}).get("exists") and ct.get("container_get_items", {}).get("exists"),
            "equip_item": ct.get("container_click", {}).get("exists"),
            "discard_item": pc.get("player_press_drop", {}).get("exists") and pc.get("player_select_slot", {}).get("exists"),
        }

        # Optional: dump player inventory screen slot layout
        if inv_probe:
            inv_dump: dict = {}
            opened_via = None
            try:
                if hasattr(minescript, "player_press_inventory"):
                    minescript.player_press_inventory()
                    opened_via = "player_press_inventory"
                elif hasattr(minescript, "open_inventory"):
                    minescript.open_inventory()
                    opened_via = "open_inventory"
                elif hasattr(minescript, "press_key_bind"):
                    # Try the keybinding by name — MC's mapping is "key.inventory"
                    try:
                        minescript.press_key_bind("key.inventory", True)
                        time.sleep(0.05)
                        minescript.press_key_bind("key.inventory", False)
                        opened_via = "press_key_bind('key.inventory')"
                    except Exception as e:
                        inv_dump["press_key_bind_error"] = str(e)
                else:
                    inv_dump["error"] = "no player_press_inventory, open_inventory, or press_key_bind API"
            except Exception as e:
                inv_dump["open_error"] = str(e)
            inv_dump["opened_via"] = opened_via
            time.sleep(0.5)
            if "error" not in inv_dump:
                inv_dump.update(_dump_open_menu_slots())
                try:
                    if hasattr(minescript, "close_screen"):
                        minescript.close_screen()
                    elif hasattr(minescript, "container_close"):
                        minescript.container_close()
                except Exception as e:
                    inv_dump["close_error"] = str(e)
            results["inventory_screen_probe"] = inv_dump

        # Optional: dump crafting table slot layout
        if table_probe_raw:
            table_dump: dict = {"target": table_probe_raw}
            try:
                parts = [int(p.strip()) for p in table_probe_raw.split(",")]
                if len(parts) != 3:
                    raise ValueError("expected x,y,z")
                tx, ty, tz = parts
                table_dump["coords"] = {"x": tx, "y": ty, "z": tz}
                try:
                    minescript.container_open(tx, ty, tz)
                except Exception as e:
                    table_dump["open_error"] = str(e)
                time.sleep(0.3)
                if "open_error" not in table_dump:
                    table_dump.update(_dump_open_menu_slots())
                    try:
                        minescript.container_close()
                    except Exception as e:
                        table_dump["close_error"] = str(e)
            except Exception as e:
                table_dump["parse_error"] = str(e)
            results["crafting_table_probe"] = table_dump

        return results

    results = await _run(_probe)
    return web.json_response(_ok(results, "API probe complete"))


async def handle_screenshot(request: web.Request) -> web.Response:
    """Capture a screenshot of the game view."""
    fmt = request.query.get("format", "jpeg")
    quality = int(request.query.get("quality", 80))
    raw = request.query.get("raw", "").lower() in ("true", "1")

    try:
        result = await _run(screenshot_mod.capture_screenshot, fmt, quality)
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return web.json_response(_err(f"Screenshot failed: {e}"), status=500)

    if raw:
        import base64
        media_type = "image/png" if fmt == "png" else "image/jpeg"
        return web.Response(
            body=base64.b64decode(result["image"]),
            content_type=media_type,
        )
    return web.json_response(_ok(result, "Screenshot captured"))


async def handle_video_stream(request: web.Request) -> web.StreamResponse:
    """MJPEG video stream via persistent ffmpeg x11grab process.

    Applies an ffmpeg `eq` filter to lift shadows so the monitor view stays
    usable when the bot is mining underground without torches. This is
    deliberately only on the stream path — the agent's `/screenshot`
    endpoint in bridge/screenshot.py stays untouched so Claude's vision
    tool still sees authentic game lighting. Tunable at runtime via the
    `MONITOR_VIDEO_FILTER` env var; set it to an empty string to disable.
    """
    fps = min(int(request.query.get("fps", 10)), 15)
    quality = int(request.query.get("quality", 5))  # ffmpeg q:v scale: 2=best, 31=worst

    video_filter = os.environ.get(
        "MONITOR_VIDEO_FILTER",
        "eq=gamma=2.0:brightness=0.08:contrast=1.15",
    )
    vf_args = ["-vf", video_filter] if video_filter else []

    response = web.StreamResponse()
    response.content_type = "multipart/x-mixed-replace; boundary=frame"
    response.headers["Cache-Control"] = "no-cache"
    await response.prepare(request)

    # Launch one persistent ffmpeg that continuously captures Xvfb → MJPEG pipe
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-f", "x11grab", "-r", str(fps), "-video_size", "854x480", "-i", ":99",
        *vf_args,
        "-vcodec", "mjpeg", "-q:v", str(quality), "-f", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )

    logger.info(f"Video stream client connected (fps={fps}, ffmpeg pid={proc.pid})")
    try:
        buf = b""
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # Extract complete JPEG frames (SOI=FFD8, EOI=FFD9)
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]  # keep partial frame
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                await response.write(
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame + b"\r\n"
                )
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    finally:
        proc.kill()
        await proc.wait()
        logger.info("Video stream client disconnected, ffmpeg killed")
    return response


async def handle_mutations(request: web.Request) -> web.Response:
    """Return recent entries from the bridge mutation log."""
    since_raw = request.query.get("since", "")
    try:
        since_ts = float(since_raw) if since_raw else None
    except ValueError:
        since_ts = None
    try:
        limit = min(int(request.query.get("limit", 100)), 1000)
    except ValueError:
        limit = 100
    entries = get_mutations(since=since_ts, limit=limit)
    return web.json_response(_ok({"mutations": entries}, f"{len(entries)} entries"))


async def handle_chat(request: web.Request) -> web.Response:
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return web.json_response(_err("Missing 'message' parameter"), status=400)
    await _run(minescript_api.send_chat, message)
    return web.json_response(_ok(message=f"Sent: {message}"))


# ---------------------------------------------------------------------------
# WebSocket event streaming
# ---------------------------------------------------------------------------

async def handle_events(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _ws_clients.add(ws)
    logger.info(f"WS client connected ({len(_ws_clients)} total)")
    try:
        async for msg in ws:
            pass  # We only broadcast, don't process incoming messages
    finally:
        _ws_clients.discard(ws)
        logger.info(f"WS client disconnected ({len(_ws_clients)} total)")
    return ws


async def broadcast_event(event: dict) -> None:
    """Send an event to all connected WS clients."""
    if not _ws_clients:
        return
    payload = json.dumps(event)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def death_monitor(app: web.Application) -> None:
    """Background task: detect player death (health <= 0) and broadcast events.

    Reads health from the WorldCache (updated every 2s by the scanner thread)
    instead of polling via _executor. This eliminates the death monitor's
    RPC load entirely — no more _ms calls on _executor every 5s.
    """
    was_dead = False

    while True:
        if not _ws_clients:
            await asyncio.sleep(5)
            continue
        try:
            health = _world_cache.get_health()
            if health is None:
                # Cache not populated yet — skip this cycle
                await asyncio.sleep(5)
                continue
            is_dead = health <= 0

            if is_dead and not was_dead:
                logger.info("Player died! Broadcasting death event.")
                await broadcast_event({
                    "type": "death",
                    "data": {"message": "Player died"},
                })
            elif not is_dead and was_dead:
                logger.info("Player respawned.")
                await broadcast_event({
                    "type": "respawn",
                    "data": {"message": "Player respawned"},
                })

            was_dead = is_dead
        except Exception as e:
            logger.debug(f"Death monitor error: {e}")

        await asyncio.sleep(5)


async def chat_event_poller(app: web.Application) -> None:
    """Background task: listen for Minescript chat events and broadcast to WS clients.

    Chat events come from a Python queue.Queue (no Minescript RPC needed), so
    polling runs on a dedicated daemon thread instead of _executor.  This keeps
    the executor free for actual minescript.* RPC calls during heavy scans.
    """
    import minescript

    loop = asyncio.get_event_loop()
    chat_q: asyncio.Queue = asyncio.Queue()

    def _poll_loop(eq):
        """Blocking loop on a dedicated thread. Pushes events into the async queue."""
        while True:
            try:
                event = eq.queue.get(timeout=0.25)
                loop.call_soon_threadsafe(chat_q.put_nowait, event)
                while not eq.queue.empty():
                    try:
                        loop.call_soon_threadsafe(chat_q.put_nowait, eq.queue.get_nowait())
                    except Exception:
                        break
            except Exception:
                pass  # timeout — no events

    with _ms_lock:
        eq = minescript.EventQueue()
        eq.register_chat_listener()
    logger.info("Chat event listener registered")

    thread = threading.Thread(target=_poll_loop, args=(eq,), daemon=True)
    thread.start()

    try:
        while True:
            try:
                ev = await chat_q.get()
                if isinstance(ev, dict):
                    raw = ev.get("message", str(ev))
                elif hasattr(ev, "message"):
                    raw = ev.message
                else:
                    raw = str(ev)
                # Strip ANSI escape codes and MC formatting codes (§x)
                msg = re.sub(r"\x1b\[[0-9;]*m|§.", "", raw).strip()
                logger.debug(f"Chat event: {msg!r}")
                # Match player chat: optional prefixes like [Not Secure], then <Player> msg
                m = re.search(r"<(\w+)>\s*(.*)", msg)
                if not m:
                    continue
                username = m.group(1)
                text = m.group(2).strip()
                # Skip messages starting with / (commands that leaked through)
                if text.startswith("/"):
                    continue
                await broadcast_event({
                    "type": "chat",
                    "data": {"username": username, "message": text},
                })
            except Exception as e:
                logger.error(f"Chat poller error: {e}")
                await asyncio.sleep(1)
    finally:
        # Unregister listeners on shutdown
        try:
            with _ms_lock:
                for lid in eq.event_listener_ids:
                    minescript.unregister_event_listener(lid)
        except Exception:
            pass


async def start_background_tasks(app: web.Application) -> None:
    _world_cache.start()
    app["chat_poller"] = asyncio.create_task(chat_event_poller(app))
    app["death_monitor"] = asyncio.create_task(death_monitor(app))


async def cleanup_background_tasks(app: web.Application) -> None:
    for task_name in ("chat_poller", "death_monitor"):
        app[task_name].cancel()
        try:
            await app[task_name]
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# App factory and entry point
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application(middlewares=[rpc_timeout_middleware])

    # GET routes
    app.router.add_get("/status", handle_status)
    app.router.add_get("/nearby/blocks", handle_nearby_blocks)
    app.router.add_get("/nearby/entities", handle_nearby_entities)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/mutations", handle_mutations)

    # POST routes
    app.router.add_post("/goto", handle_goto)
    app.router.add_post("/mine", handle_mine)
    app.router.add_post("/follow", handle_follow)
    app.router.add_post("/explore", handle_explore)
    app.router.add_post("/stop", handle_stop)
    app.router.add_post("/place", handle_place)
    app.router.add_post("/break", handle_break)
    app.router.add_post("/collect", handle_collect)
    app.router.add_post("/attack", handle_attack)
    app.router.add_post("/craft", handle_craft)
    app.router.add_post("/smelt", handle_smelt)
    app.router.add_post("/equip", handle_equip)
    app.router.add_post("/discard", handle_discard)
    app.router.add_post("/chat", handle_chat)
    app.router.add_get("/probe", handle_probe)
    app.router.add_get("/screenshot", handle_screenshot)
    app.router.add_get("/video/stream", handle_video_stream)

    # WebSocket
    app.router.add_get("/events", handle_events)

    # Background tasks
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app


def main() -> None:
    """Entry point — call from Minescript script."""
    logger.info("Starting bridge server on 0.0.0.0:8080")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8080, print=lambda msg: logger.info(msg))
