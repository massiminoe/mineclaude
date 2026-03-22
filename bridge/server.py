"""HTTP + WebSocket bridge server running inside Minescript's Python runtime.

Single asyncio process on port 8080 using aiohttp.
All blocking minescript.* calls dispatched via run_in_executor().
Chat events polled from Minescript and broadcast to WS clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from aiohttp import web

import re

from bridge import minescript_api, baritone

logger = logging.getLogger("bridge")
_log_handler = logging.FileHandler("/tmp/bridge.log")
_log_handler.setFormatter(logging.Formatter("[bridge] %(asctime)s %(message)s"))
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)

_executor = ThreadPoolExecutor(max_workers=2)
_ws_clients: set[web.WebSocketResponse] = set()


def _ok(data: dict | list | None = None, message: str = "ok") -> dict:
    return {"status": "success", "message": message, "data": data or {}}


def _err(message: str) -> dict:
    return {"status": "error", "message": message, "data": {}}


async def _run(fn, *args):
    """Run a blocking function in the thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, partial(fn, *args))


# ---------------------------------------------------------------------------
# HTTP route handlers
# ---------------------------------------------------------------------------

async def handle_status(request: web.Request) -> web.Response:
    data = await _run(minescript_api.get_player_status)
    return web.json_response(_ok(data, "Status retrieved"))


async def handle_nearby_blocks(request: web.Request) -> web.Response:
    radius = int(request.query.get("r", 8))
    blocks = await _run(minescript_api.get_nearby_blocks, radius)
    return web.json_response(_ok({"blocks": blocks}, f"Found {len(blocks)} blocks"))


async def handle_nearby_entities(request: web.Request) -> web.Response:
    radius = int(request.query.get("r", 32))
    entities = await _run(minescript_api.get_nearby_entities, radius)
    return web.json_response(_ok({"entities": entities}, f"Found {len(entities)} entities"))


async def handle_goto(request: web.Request) -> web.Response:
    body = await request.json()
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    cmd = baritone.goto(x, y, z)
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, f"Going to {x}, {y}, {z}"))


async def handle_mine(request: web.Request) -> web.Response:
    body = await request.json()
    block = body.get("block", "")
    count = body.get("count", 0)
    if not block:
        return web.json_response(_err("Missing 'block' parameter"), status=400)
    cmd = baritone.mine(block, count)
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, f"Mining {count} {block}" if count else f"Mining {block}"))


async def handle_follow(request: web.Request) -> web.Response:
    body = await request.json()
    player = body.get("player", "")
    if not player:
        return web.json_response(_err("Missing 'player' parameter"), status=400)
    cmd = baritone.follow(player)
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, f"Following {player}"))


async def handle_explore(request: web.Request) -> web.Response:
    cmd = baritone.explore()
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, "Exploring"))


async def handle_stop(request: web.Request) -> web.Response:
    cmd = baritone.stop()
    await _run(minescript_api.send_chat, cmd)
    return web.json_response(_ok({"command": cmd}, "Stopped"))


async def handle_place(request: web.Request) -> web.Response:
    body = await request.json()
    block = body.get("block", "")
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    face = body.get("face", "top")
    if not block:
        return web.json_response(_err("Missing 'block' parameter"), status=400)
    result = await _run(minescript_api.place_block, block, x, y, z, face)
    if result.get("placed"):
        return web.json_response(_ok(result, f"Placed {block} at {x}, {y}, {z}"))
    return web.json_response(_err(result.get("error", "Failed to place block")))


async def handle_break(request: web.Request) -> web.Response:
    body = await request.json()
    x, y, z = body.get("x", 0), body.get("y", 0), body.get("z", 0)
    result = await _run(minescript_api.break_block, x, y, z)
    if result.get("broken"):
        return web.json_response(_ok(result, f"Broke block at {x}, {y}, {z}"))
    return web.json_response(_err(result.get("error", "Failed to break block")))


async def handle_attack(request: web.Request) -> web.Response:
    body = await request.json()
    entity_id = body.get("entity_id", "")
    if not entity_id:
        return web.json_response(_err("Missing 'entity_id' parameter"), status=400)
    result = await _run(minescript_api.attack_entity, entity_id)
    if result.get("attacked"):
        return web.json_response(_ok(result, f"Attacked {entity_id}"))
    return web.json_response(_err(result.get("error", "Failed to attack")))


async def handle_craft(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    count = body.get("count", 1)
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.craft_item, item, count)
    if result.get("crafted", 0) > 0:
        return web.json_response(_ok(result, f"Crafted {count} {item}"))
    return web.json_response(_err(result.get("error", "Failed to craft")))


async def handle_equip(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    slot = body.get("slot", "hand")
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.equip_item, item, slot)
    if result.get("equipped"):
        return web.json_response(_ok(result, f"Equipped {item} to {slot}"))
    return web.json_response(_err(result.get("error", "Failed to equip")))


async def handle_discard(request: web.Request) -> web.Response:
    body = await request.json()
    item = body.get("item", "")
    count = body.get("count", 1)
    if not item:
        return web.json_response(_err("Missing 'item' parameter"), status=400)
    result = await _run(minescript_api.discard_item, item, count)
    if result.get("discarded", 0) > 0:
        return web.json_response(_ok(result, f"Discarded {count} {item}"))
    return web.json_response(_err(result.get("error", "Failed to discard")))


async def handle_probe(request: web.Request) -> web.Response:
    """Run API probe and return results — tests which minescript APIs exist."""
    import inspect

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
            yaw, pitch = minescript.player_orientation()
            results["tests"]["player_orientation()"] = f"ok: yaw={yaw:.1f}, pitch={pitch:.1f}"
        except Exception as e:
            results["tests"]["player_orientation()"] = str(e)

        try:
            result = minescript.player_look_at(0.0, 64.0, 0.0)
            results["tests"]["player_look_at(0,64,0)"] = f"ok: {result}"
        except Exception as e:
            results["tests"]["player_look_at(0,64,0)"] = str(e)

        try:
            minescript.player_inventory_select_slot(0)
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

        return results

    results = await _run(_probe)
    return web.json_response(_ok(results, "API probe complete"))


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
    """Background task: detect player death (health <= 0) and broadcast events."""
    import minescript

    loop = asyncio.get_event_loop()
    was_dead = False

    while True:
        try:
            health = await loop.run_in_executor(_executor, minescript.player_health)
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

        await asyncio.sleep(1)


async def chat_event_poller(app: web.Application) -> None:
    """Background task: listen for Minescript chat events and broadcast to WS clients."""
    import minescript

    loop = asyncio.get_event_loop()

    def _poll_chat_queue(eq):
        """Blocking call — runs in executor. Returns a batch of ChatEvents."""
        events = []
        try:
            # Block up to 0.25s for the first event
            event = eq.queue.get(timeout=0.25)
            events.append(event)
            # Drain any additional queued events without blocking
            while not eq.queue.empty():
                try:
                    events.append(eq.queue.get_nowait())
                except Exception:
                    break
        except Exception:
            pass  # timeout — no events
        return events

    eq = minescript.EventQueue()
    eq.register_chat_listener()
    logger.info("Chat event listener registered")

    try:
        while True:
            try:
                events = await loop.run_in_executor(_executor, _poll_chat_queue, eq)
                for ev in events:
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
            for lid in eq.event_listener_ids:
                minescript.unregister_event_listener(lid)
        except Exception:
            pass


async def start_background_tasks(app: web.Application) -> None:
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
    app = web.Application()

    # GET routes
    app.router.add_get("/status", handle_status)
    app.router.add_get("/nearby/blocks", handle_nearby_blocks)
    app.router.add_get("/nearby/entities", handle_nearby_entities)

    # POST routes
    app.router.add_post("/goto", handle_goto)
    app.router.add_post("/mine", handle_mine)
    app.router.add_post("/follow", handle_follow)
    app.router.add_post("/explore", handle_explore)
    app.router.add_post("/stop", handle_stop)
    app.router.add_post("/place", handle_place)
    app.router.add_post("/break", handle_break)
    app.router.add_post("/attack", handle_attack)
    app.router.add_post("/craft", handle_craft)
    app.router.add_post("/equip", handle_equip)
    app.router.add_post("/discard", handle_discard)
    app.router.add_post("/chat", handle_chat)
    app.router.add_get("/probe", handle_probe)

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
