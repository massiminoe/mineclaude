"""Typed wrapper around minescript.* calls. Returns dicts for JSON serialization.

All functions are blocking — the server layer calls them via run_in_executor().
The `minescript` module is only importable inside the Minescript mod's Python runtime.

Updated for Minescript v5.0b11 API (dataclass returns, not dicts).
Phase 4: Real player actions with server-command fallbacks.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time

import minescript

logger = logging.getLogger("bridge")

# Minescript's stdin/stdout JSON-RPC channel is not thread-safe — concurrent
# calls from different executor threads interleave their messages and produce
# JSONDecodeError "Extra data" output in MC chat. Serialize all minescript.*
# calls through this lock.
_ms_lock = threading.Lock()


def _ms(fn, *args, **kwargs):
    """Call a minescript.* function under the global RPC lock."""
    with _ms_lock:
        return fn(*args, **kwargs)


# MC runs at 20 TPS = 50 ms per tick. Inventory operations (key.inventory
# toggles, container_click_slot, container_swap_slots, container_close) are
# event-based: MC processes the event on its next tick. Sleeping for whole-tick
# multiples between operations gives MC time to settle, prevents back-to-back
# events from being coalesced, and makes the agent's actions visibly human in
# the game window.
MC_TICK_MS = 50


def _tick_sleep(ticks: int = 1) -> None:
    """Sleep for `ticks` MC game ticks (50 ms each).

    Use after any inventory operation so MC has time to process the event
    before the next call lands. Centralized so the cadence is tunable in one
    place — adjusting MC_TICK_MS or the per-call tick count changes pacing
    everywhere.
    """
    time.sleep(MC_TICK_MS * ticks / 1000.0)


# ---------------------------------------------------------------------------
# Read-only queries (unchanged)
# ---------------------------------------------------------------------------

def get_player_status() -> dict:
    """Get player position, health, hunger, inventory, biome, and time."""
    pos = _ms(minescript.player_position)
    x, y, z = pos[0], pos[1], pos[2]
    result = {
        "position": {"x": x, "y": y, "z": z},
        "health": 20.0,
        "hunger": 20,
        "inventory": [],
        "biome": "unknown",
        "time": 0,
    }

    try:
        result["health"] = _ms(minescript.player_health)
    except (AttributeError, TypeError):
        pass

    try:
        inv = _ms(minescript.player_inventory)
        items = []
        for i, item in enumerate(inv):
            if item is None:
                continue
            # v5.0 returns ItemStack dataclass with .item, .count, .slot
            name = getattr(item, "item", None)
            count = getattr(item, "count", 1)
            if name and "air" not in name:
                items.append({
                    "name": name.replace("minecraft:", ""),
                    "count": count,
                    "slot": getattr(item, "slot", i) or i,
                })
        result["inventory"] = items
    except (AttributeError, TypeError):
        pass

    try:
        info = _ms(minescript.world_info)
        result["time"] = info.day_ticks
    except (AttributeError, TypeError):
        pass

    return result


def get_nearby_blocks(radius: int = 8, block_types: list[str] | None = None) -> list[dict]:
    """Scan blocks in a sphere around the player. Skip air.

    Thin wrapper around scan_blocks_chunked that looks up the player's
    current position. Used as a fallback path when WorldCache is empty and
    directly by tests. Most production reads should go through WorldCache.

    Results are sorted by distance, closest first.
    """
    radius = min(radius, 32)
    pos = _ms(minescript.player_position)
    px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
    return scan_blocks_chunked(px, py, pz, radius, block_types)


def scan_blocks_chunked(
    px: int,
    py: int,
    pz: int,
    radius: int,
    block_types: list[str] | None = None,
) -> list[dict]:
    """Scan blocks in a sphere around (px, py, pz). Skip air.

    Block names are stripped of the minecraft: namespace and state suffixes
    (e.g. "minecraft:oak_log[axis=y]" -> "oak_log") so consumers can match
    by base block name.

    If block_types is provided, only blocks matching those names are returned.

    Uses chunked bulk loads via get_block_region with _tick_sleep pacing
    between chunks to reduce RPC pipe pressure. Paces and lock-releases
    allow command threads to interleave between chunks via _ms_lock.

    Results are sorted by distance, closest first.
    """
    radius = min(radius, 32)
    blocks: list[dict] = []
    radius_sq = radius * radius
    type_set = set(block_types) if block_types else None

    # Wait for chunks in the horizontal scan area to fully load before
    # querying. Without this, get_block_region can return phantom data for
    # partially-loaded chunks (observed: scan reports a log at a position
    # that getblock a moment later says is air, right after container
    # restart while chunks are still syncing).
    if hasattr(minescript, "await_loaded_region"):
        try:
            _ms(
                minescript.await_loaded_region,
                px - radius, pz - radius,
                px + radius, pz + radius,
            )
            logger.info(
                f"scan: await_loaded_region({px - radius},{pz - radius}"
                f",{px + radius},{pz + radius}) ok, player=({px},{py},{pz})"
            )
        except Exception as e:
            logger.warning(f"scan: await_loaded_region failed: {type(e).__name__}: {e}")
    else:
        logger.warning("scan: minescript.await_loaded_region NOT AVAILABLE")

    # Chunked bulk loads via get_block_region. A single large region (e.g.
    # radius=32 → 274k blocks) produces multi-MB JSON on Minescript's stdin
    # pipe, which occasionally drops a byte mid-stream and crashes the mod's
    # parser. Splitting into ~20-wide sub-regions keeps each response well
    # under 200KB.
    CHUNK_SIDE = 20
    try:
        if not hasattr(minescript, "get_block_region"):
            raise AttributeError("get_block_region missing")

        scan_start = time.monotonic()
        for ox in range(-radius, radius + 1, CHUNK_SIDE):
            for oy in range(-radius, radius + 1, CHUNK_SIDE):
                for oz in range(-radius, radius + 1, CHUNK_SIDE):
                    min_x = px + ox
                    min_y = py + oy
                    min_z = pz + oz
                    max_x = min(px + radius, min_x + CHUNK_SIDE - 1)
                    max_y = min(py + radius, min_y + CHUNK_SIDE - 1)
                    max_z = min(pz + radius, min_z + CHUNK_SIDE - 1)

                    region = _ms(
                        minescript.get_block_region,
                        [min_x, min_y, min_z],
                        [max_x, max_y, max_z],
                    )
                    for bx in range(min_x, max_x + 1):
                        for by in range(min_y, max_y + 1):
                            for bz in range(min_z, max_z + 1):
                                dx, dy, dz = bx - px, by - py, bz - pz
                                dist_sq = dx * dx + dy * dy + dz * dz
                                if dist_sq > radius_sq:
                                    continue
                                name = region.get_block(bx, by, bz)
                                if name and "air" not in name:
                                    clean = name.replace("minecraft:", "").split("[")[0]
                                    if type_set and clean not in type_set:
                                        continue
                                    blocks.append({
                                        "name": clean,
                                        "x": bx, "y": by, "z": bz,
                                        "distance": round(math.sqrt(dist_sq), 1),
                                    })
                    # Yield briefly between chunk RPCs to reduce pipe pressure
                    # and let command threads interleave via _ms_lock.
                    _tick_sleep(1)
        scan_duration = time.monotonic() - scan_start
        logger.info(f"scan: {len(blocks)} blocks in {scan_duration:.1f}s (radius={radius})")
        blocks.sort(key=lambda b: b["distance"])
        _verify_scan_blocks(blocks)
        return blocks
    except (AttributeError, TypeError):
        pass

    # Fallback: single getblock calls
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                dist_sq = dx * dx + dy * dy + dz * dz
                if dist_sq > radius_sq:
                    continue
                bx, by, bz = px + dx, py + dy, pz + dz
                try:
                    name = _ms(minescript.getblock, bx, by, bz)
                    if name and "air" not in name:
                        clean = name.replace("minecraft:", "").split("[")[0]
                        if type_set and clean not in type_set:
                            continue
                        blocks.append({
                            "name": clean,
                            "x": bx, "y": by, "z": bz,
                            "distance": round(math.sqrt(dist_sq), 1),
                        })
                except Exception:
                    continue

    blocks.sort(key=lambda b: b["distance"])
    _verify_scan_blocks(blocks)
    return blocks


def _verify_scan_blocks(blocks: list[dict]) -> None:
    """Diagnostic: re-query each reported block via getblock and log any
    mismatches. Gated behind BRIDGE_VERIFY_SCAN=1 env var — doubles the
    RPC count per scan so only enable when debugging phantom blocks.
    """
    if os.environ.get("BRIDGE_VERIFY_SCAN") != "1":
        return
    mismatches = 0
    checked = 0
    for b in blocks:
        try:
            live = _ms(minescript.getblock, b["x"], b["y"], b["z"])
        except Exception as e:
            logger.warning(f"scan-verify getblock error at ({b['x']},{b['y']},{b['z']}): {e}")
            continue
        checked += 1
        if not live or "air" in live:
            mismatches += 1
            logger.warning(
                f"scan-verify MISMATCH: reported {b['name']} at "
                f"({b['x']},{b['y']},{b['z']}) but getblock says {live!r}"
            )
    logger.info(f"scan-verify: checked {checked} blocks, {mismatches} mismatches")


def _clean_entity_type(raw) -> str:
    """Strip Minescript prefixes from an entity type/name string.

    Minescript v5.0 returns types like 'entity.minecraft.zombie' (with dots)
    rather than 'minecraft:zombie' (with colon). Both forms appear in the wild,
    so handle both defensively.
    """
    s = str(raw)
    if s.startswith("entity.minecraft."):
        return s[len("entity.minecraft."):]
    if s.startswith("minecraft:"):
        return s[len("minecraft:"):]
    return s


def get_nearby_entities(radius: int = 32) -> list[dict]:
    """List entities within radius."""
    pos = _ms(minescript.player_position)
    px, py, pz = pos[0], pos[1], pos[2]
    result = []

    try:
        # v5.0 returns List[EntityData] with .position, .name, .type, .health
        raw = _ms(minescript.entities, max_distance=float(radius))
    except (AttributeError, TypeError):
        return result

    for ent in raw:
        ex, ey, ez = ent.position
        dist = math.sqrt((ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2)
        result.append({
            "name": _clean_entity_type(ent.name),
            "type": _clean_entity_type(ent.type),
            "x": ex, "y": ey, "z": ez,
            "distance": round(dist, 1),
            "health": ent.health or 0,
        })

    return result


def send_chat(message: str) -> None:
    """Send a chat message or command.

    Regular messages use /say to bypass signed chat (avoids disconnect
    with ONLINE_MODE=false). Baritone commands (#goto etc.) go through
    minescript.chat() since they're intercepted client-side.
    """
    if message.startswith("#") or message.startswith("\\"):
        # Baritone (#) and Minescript (\) commands are intercepted client-side
        _ms(minescript.chat, message)
    elif message.startswith("/"):
        # Already a command
        _ms(minescript.execute, message)
    else:
        # Use /tellraw to avoid signed chat packet issues and handle special chars
        # Strip non-ASCII chars (emojis break MC rendering)
        clean = message.encode("ascii", errors="ignore").decode("ascii")
        # JSON-encode the text to escape quotes and special chars
        text_json = json.dumps({"text": f"[Claude] {clean}"})
        _ms(minescript.execute, f"/tellraw @a {text_json}")


# ---------------------------------------------------------------------------
# Blocking navigation
# ---------------------------------------------------------------------------

def goto_and_wait(x: float, y: float, z: float, timeout: float = 60.0, threshold: float = 2.0) -> dict:
    """Send Baritone #goto and block until player arrives or timeout.

    Returns dict with arrived (bool), final position, and distance.
    """
    from bridge.baritone import goto
    cmd = goto(x, y, z)
    send_chat(cmd)
    logger.info(f"goto: sent '{cmd}', waiting for arrival (timeout={timeout}s)")

    deadline = time.monotonic() + timeout
    stale_count = 0
    last_pos = None

    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            pos = _ms(minescript.player_position)
            px, py, pz = pos[0], pos[1], pos[2]
        except Exception:
            continue

        dist = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - z) ** 2)

        if dist <= threshold:
            logger.info(f"goto: arrived at ({px:.0f}, {py:.0f}, {pz:.0f}), dist={dist:.1f}")
            return {
                "arrived": True,
                "position": {"x": px, "y": py, "z": pz},
                "distance": round(dist, 1),
            }

        # Detect if stuck (position hasn't changed for 5s)
        current_pos = (round(px, 1), round(py, 1), round(pz, 1))
        if current_pos == last_pos:
            stale_count += 1
            if stale_count >= 10:  # 10 * 0.5s = 5s stuck
                logger.warning(f"goto: stuck at ({px:.0f}, {py:.0f}, {pz:.0f}), dist={dist:.1f}")
                return {
                    "arrived": False,
                    "error": f"Stuck at distance {dist:.1f} from target",
                    "position": {"x": px, "y": py, "z": pz},
                    "distance": round(dist, 1),
                }
        else:
            stale_count = 0
            last_pos = current_pos

    # Timeout
    logger.warning(f"goto: timed out after {timeout}s")
    try:
        pos = _ms(minescript.player_position)
        px, py, pz = pos[0], pos[1], pos[2]
        dist = math.sqrt((px - x) ** 2 + (py - y) ** 2 + (pz - z) ** 2)
        return {
            "arrived": False,
            "error": f"Timed out after {timeout}s, distance={dist:.1f}",
            "position": {"x": px, "y": py, "z": pz},
            "distance": round(dist, 1),
        }
    except Exception:
        return {"arrived": False, "error": f"Timed out after {timeout}s"}


# ---------------------------------------------------------------------------
# Helpers (shared by real implementations)
# ---------------------------------------------------------------------------

def _has_api(*names: str) -> bool:
    """Check if all named minescript APIs exist."""
    return all(hasattr(minescript, n) for n in names)


def _look_at_block(x: int, y: int, z: int) -> None:
    """Set player orientation to look at block center."""
    from bridge.player_control import look_at_block
    look_at_block(x, y, z)


def _look_at_entity_pos(entity_name: str) -> bool:
    """Look at an entity by name. Returns True if found."""
    from bridge.player_control import look_at_entity
    return look_at_entity(entity_name)


def _select_item(item_name: str) -> bool:
    """Find item, move to hotbar, select it. Returns True on success."""
    from bridge.player_control import select_item_in_hotbar
    return select_item_in_hotbar(item_name)


def _is_within_reach(x: float, y: float, z: float, reach: float = 4.5) -> bool:
    from bridge.player_control import is_within_reach
    return is_within_reach(x, y, z, reach)


def _navigate_near(x: float, y: float, z: float, reach: float = 3.5) -> bool:
    from bridge.player_control import navigate_near
    return navigate_near(x, y, z, reach)


def _find_item_slot(item_name: str) -> int | None:
    from bridge.player_control import find_item_slot
    return find_item_slot(item_name)


# ---------------------------------------------------------------------------
# Phase 1: Simple actions — discard, equip
# ---------------------------------------------------------------------------

def discard_item(item: str, count: int = 1) -> dict:
    """Discard items from inventory.

    Real: select item in hotbar, press drop key.
    Fallback: /clear command.
    """
    try:
        return _discard_real(item, count)
    except AttributeError:
        logger.info(f"discard: API not available, using fallback for {item}")
        return _discard_fallback(item, count)
    except Exception as e:
        logger.warning(f"discard: real impl failed ({e}), using fallback")
        return _discard_fallback(item, count)


def _discard_real(item: str, count: int) -> dict:
    if not _has_api("player_press_drop", "player_inventory_select_slot"):
        raise AttributeError("Required APIs missing")

    _ensure_no_screen_open()

    # Find item and move to hotbar
    if not _select_item(item):
        return {"discarded": 0, "error": f"No {item} in inventory", "method": "real"}

    # Drop items one at a time
    dropped = 0
    for _ in range(count):
        _ms(minescript.player_press_drop, True)
        _tick_sleep(1)
        _ms(minescript.player_press_drop, False)
        _tick_sleep(1)
        dropped += 1

    # Verify by checking inventory
    remaining_slot = _find_item_slot(item)
    logger.info(f"discard: dropped {dropped} {item} (real)")
    return {"discarded": dropped, "method": "real"}


def _discard_fallback(item: str, count: int) -> dict:
    try:
        _ms(minescript.execute, f"/clear @s minecraft:{item} {count}")
        return {"discarded": count, "method": "fallback"}
    except Exception as e:
        return {"discarded": 0, "error": str(e), "method": "fallback"}


def equip_item(item: str, slot: str = "hand") -> dict:
    """Equip an item to a slot.

    Real: inventory manipulation via container_click for armor,
          hotbar select for hand.
    Fallback: /item replace command.
    """
    try:
        return _equip_real(item, slot)
    except AttributeError:
        logger.info(f"equip: API not available, using fallback for {item}")
        return _equip_fallback(item, slot)
    except Exception as e:
        logger.warning(f"equip: real impl failed ({e}), using fallback")
        return _equip_fallback(item, slot)


def _equip_real(item: str, slot: str) -> dict:
    item = item.replace("minecraft:", "")
    if slot == "hand" or slot == "mainhand":
        if not _has_api("player_inventory_select_slot"):
            raise AttributeError("player_inventory_select_slot missing")
        if not _select_item(item):
            return {"equipped": False, "error": f"No {item} in inventory", "method": "real"}
        logger.info(f"equip: equipped {item} to hand (real)")
        return {"equipped": True, "method": "real"}

    if slot == "offhand":
        # Select item in hotbar, then swap hands
        if not _has_api("player_inventory_select_slot", "player_press_swap_hands"):
            raise AttributeError("APIs missing for offhand equip")
        if not _select_item(item):
            return {"equipped": False, "error": f"No {item} in inventory", "method": "real"}
        _ms(minescript.player_press_swap_hands, True)
        _tick_sleep(1)
        _ms(minescript.player_press_swap_hands, False)
        _tick_sleep(2)
        logger.info(f"equip: equipped {item} to offhand (real)")
        return {"equipped": True, "method": "real"}

    # Armor slots — open the player inventory screen and swap into the armor slot
    if slot in _INVENTORY_MENU_ARMOR_SLOTS:
        return _equip_armor_real(item, slot)

    raise AttributeError(f"Unknown equip slot: {slot}")


def _equip_armor_real(item: str, slot: str) -> dict:
    """Equip armor by opening the player inventory and swapping into the armor slot."""
    if not _has_api("container_swap_slots", "container_get_items"):
        raise AttributeError("container_swap_slots / container_get_items missing")
    if not _has_api("press_key_bind") and not _has_api("player_press_inventory") and not _has_api("open_inventory"):
        raise AttributeError("no API to open player inventory screen")

    target_slot = _INVENTORY_MENU_ARMOR_SLOTS[slot]
    inv_lo, inv_hi = _INVENTORY_MENU_INV_RANGE

    open_err = _open_player_inventory_screen()
    if open_err is not None:
        return {"equipped": False, "error": open_err, "method": "real"}
    try:
        # Locate the armor item in the player inventory portion of the menu
        items = _ms(minescript.container_get_items)
        source_slot = None
        for ci in items:
            ci_slot = getattr(ci, "slot", None)
            ci_name = (getattr(ci, "item", "") or "").replace("minecraft:", "")
            ci_count = getattr(ci, "count", 0)
            if ci_slot is None or ci_count <= 0:
                continue
            if ci_slot < inv_lo or ci_slot > inv_hi:
                continue
            if ci_name == item:
                source_slot = ci_slot
                break
        if source_slot is None:
            return {"equipped": False, "error": f"No {item} in inventory", "method": "real"}

        try:
            _ms(minescript.container_swap_slots, source_slot, target_slot)
        except Exception as e:
            return {"equipped": False, "error": f"swap_slots failed: {e}", "method": "real"}
        _tick_sleep(2)  # let MC process the swap before verifying

        # Verify
        try:
            slot_info = _ms(minescript.container_get_slot, target_slot)
            actual = (getattr(slot_info, "item", "") or "").replace("minecraft:", "") if slot_info else ""
        except Exception:
            actual = ""
        if actual != item:
            return {
                "equipped": False,
                "error": f"verification failed: armor slot {target_slot} shows {actual!r}",
                "method": "real",
            }

        logger.info(f"equip: equipped {item} to {slot} via armor slot {target_slot} (real)")
        return {"equipped": True, "item": item, "slot": slot, "method": "real"}
    finally:
        _close_open_screen()


def _equip_fallback(item: str, slot: str) -> dict:
    slot_map = {
        "hand": "mainhand",
        "offhand": "offhand",
        "head": "head",
        "chest": "chest",
        "legs": "legs",
        "feet": "feet",
    }
    mc_slot = slot_map.get(slot, "mainhand")
    try:
        _ms(minescript.execute, f"/item replace entity @s armor.{mc_slot} with minecraft:{item} 1")
        return {"equipped": True, "method": "fallback"}
    except Exception as e:
        return {"equipped": False, "error": str(e), "method": "fallback"}


# ---------------------------------------------------------------------------
# Phase 2: Medium actions — break, place, attack
# ---------------------------------------------------------------------------

def break_block(x: int, y: int, z: int) -> dict:
    """Break a block at coordinates.

    Real: look at block, hold attack until broken.
    Fallback: /setblock ... air destroy.
    """
    try:
        return _break_real(x, y, z)
    except AttributeError:
        logger.info(f"break: API not available, using fallback at {x},{y},{z}")
        return _break_fallback(x, y, z)
    except Exception as e:
        logger.warning(f"break: real impl failed ({e}), using fallback")
        return _break_fallback(x, y, z)


def _break_real(x: int, y: int, z: int) -> dict:
    if not _has_api("player_set_orientation", "player_press_attack"):
        raise AttributeError("Required APIs missing")

    _ensure_no_screen_open()

    # Check what's there. getblock can briefly lag chunk updates after a
    # neighboring block was just broken (observed: breaking a tree's y=68 log
    # then immediately reading y=69 returned "air"), so tolerate one stale
    # read before declaring no block.
    try:
        name = _ms(minescript.getblock, x, y, z)
    except Exception:
        name = "unknown"
    if not name or "air" in name:
        time.sleep(0.1)
        try:
            name = _ms(minescript.getblock, x, y, z)
        except Exception:
            name = "unknown"
    if not name or "air" in name:
        logger.info(f"break: no block at {x},{y},{z} (got {name!r})")
        return {"broken": False, "error": "No block at position", "method": "real"}

    # Check player is close enough (fail fast instead of 15s timeout)
    from bridge.player_control import get_player_distance
    dist = get_player_distance(float(x), float(y), float(z))
    if dist > 6.0:
        return {
            "broken": False,
            "error": f"Too far from block ({dist:.1f} blocks away, need <6)",
            "method": "real",
        }

    # Navigate within reach if needed
    if not _is_within_reach(x, y, z):
        if not _navigate_near(x, y, z, reach=3.5):
            raise Exception("Could not navigate within reach")

    # Look at the block
    _look_at_block(x, y, z)
    time.sleep(0.1)

    # Hold attack to mine — press and hold, periodically check if block broke.
    # We sleep 0.25s between iterations and only poll getblock every 3rd
    # iteration (~0.75s cadence) to reduce RPC traffic on Minescript's stdin
    # channel. Sustained high-frequency polling was observed to race the mod's
    # stdout writer and crash its JSON parser with "Extra data" errors.
    original_block = name
    _ms(minescript.player_press_attack, True)
    deadline = time.monotonic() + 15.0
    iteration = 0
    try:
        while time.monotonic() < deadline:
            time.sleep(0.25)
            iteration += 1
            if iteration % 3 != 0:
                continue
            try:
                current = _ms(minescript.getblock, x, y, z)
                if current != original_block:
                    logger.info(f"break: broke {original_block} at {x},{y},{z} (real)")
                    return {
                        "broken": True,
                        "block": original_block.replace("minecraft:", ""),
                        "method": "real",
                    }
            except Exception:
                pass
    finally:
        _ms(minescript.player_press_attack, False)

    # Timed out
    raise Exception(f"Timed out breaking {original_block}")


def _break_fallback(x: int, y: int, z: int) -> dict:
    try:
        name = _ms(minescript.getblock, x, y, z)
        _ms(minescript.execute, f"/setblock {x} {y} {z} minecraft:air destroy")
        return {
            "broken": True,
            "block": name.replace("minecraft:", "") if name else "unknown",
            "method": "fallback",
        }
    except Exception as e:
        return {"broken": False, "error": str(e), "method": "fallback"}


def collect_items(radius: float = 3.0) -> dict:
    """Walk to and pick up dropped item entities near the player."""
    from bridge.player_control import collect_nearby_items
    count = collect_nearby_items(radius)
    return {"collected": count}


def place_block(block: str, x: int, y: int, z: int, face: str = "top") -> dict:
    """Place a block at coordinates.

    Real: select block in hotbar, look at adjacent face, right-click.
    Fallback: /setblock command.
    """
    try:
        return _place_real(block, x, y, z, face)
    except AttributeError:
        logger.info(f"place: API not available, using fallback for {block}")
        return _place_fallback(block, x, y, z)
    except Exception as e:
        logger.warning(f"place: real impl failed ({e}), using fallback")
        return _place_fallback(block, x, y, z)


def _place_real(block: str, x: int, y: int, z: int, face: str) -> dict:
    if not _has_api("player_set_orientation", "player_press_use", "player_inventory_select_slot"):
        raise AttributeError("Required APIs missing")

    _ensure_no_screen_open()

    # Check if target position is air
    try:
        current = _ms(minescript.getblock, x, y, z)
        if current and "air" not in current:
            return {"placed": False, "error": f"Block already at {x},{y},{z}: {current}", "method": "real"}
    except Exception:
        pass

    # Find block in inventory and select it
    if not _select_item(block):
        return {"placed": False, "error": f"No {block} in inventory", "method": "real"}

    # Navigate within reach if needed
    if not _is_within_reach(x, y, z):
        if not _navigate_near(x, y, z, reach=3.5):
            raise Exception("Could not navigate within reach")

    # Find an adjacent solid block to click against
    from bridge.player_control import find_adjacent_solid_block
    adjacent = find_adjacent_solid_block(x, y, z)
    if adjacent is None:
        raise Exception("No adjacent solid block to place against")

    adj_x, adj_y, adj_z, click_face = adjacent

    # Look at the face of the adjacent block
    # Aim at the center of the face that borders our target position
    _look_at_block(adj_x, adj_y, adj_z)
    time.sleep(0.1)

    # Right-click to place
    _ms(minescript.player_press_use, True)
    time.sleep(0.05)
    _ms(minescript.player_press_use, False)
    time.sleep(0.15)

    # Verify placement. Three distinct outcomes:
    #   - getblock raises → unknown, return tolerant True
    #   - getblock returns the placed block → confirmed True
    #   - getblock returns "air" → press_use definitively did nothing → False
    try:
        placed_block = _ms(minescript.getblock, x, y, z)
    except Exception as e:
        logger.info(f"place: placed {block} at {x},{y},{z} (real, verify errored: {e})")
        return {"placed": True, "method": "real"}

    if placed_block and "air" not in placed_block:
        logger.info(f"place: placed {block} at {x},{y},{z} (real)")
        return {"placed": True, "method": "real"}

    logger.warning(f"place: press_use did not place {block} at {x},{y},{z} (still air)")
    return {
        "placed": False,
        "error": "press_use did not place block (target still air); is a GUI open?",
        "method": "real",
    }


def _place_fallback(block: str, x: int, y: int, z: int) -> dict:
    try:
        _ms(minescript.execute, f"/setblock {x} {y} {z} minecraft:{block}")
        return {"placed": True, "method": "fallback"}
    except Exception as e:
        return {"placed": False, "error": str(e), "method": "fallback"}


def attack_entity(entity_id: str) -> dict:
    """Attack an entity.

    Real: look at entity, left-click.
    Fallback: /damage command.
    """
    try:
        return _attack_real(entity_id)
    except AttributeError:
        logger.info(f"attack: API not available, using fallback for {entity_id}")
        return _attack_fallback(entity_id)
    except Exception as e:
        logger.warning(f"attack: real impl failed ({e}), using fallback")
        return _attack_fallback(entity_id)


def _attack_real(entity_id: str) -> dict:
    if not _has_api("player_set_orientation", "player_press_attack"):
        raise AttributeError("Required APIs missing")

    _ensure_no_screen_open()

    # Find the entity
    try:
        ents = _ms(minescript.entities)
    except (AttributeError, TypeError):
        raise AttributeError("entities() not available")

    target = None
    for ent in ents:
        name = str(getattr(ent, "name", "")).replace("minecraft:", "")
        etype = str(getattr(ent, "type", "")).replace("minecraft:", "")
        if entity_id.lower() in (name.lower(), etype.lower()):
            target = ent
            break

    if target is None:
        return {"attacked": False, "error": f"Entity {entity_id} not found", "method": "real"}

    ex, ey, ez = target.position

    # Check melee reach (3.0 blocks)
    from bridge.player_control import get_player_distance
    dist = get_player_distance(ex, ey, ez)
    if dist > 3.5:
        # Navigate closer
        if not _navigate_near(ex, ey, ez, reach=2.5):
            raise Exception("Could not navigate within melee range")

    # Look at entity and attack
    if not _look_at_entity_pos(entity_id):
        raise Exception(f"Could not look at {entity_id}")

    time.sleep(0.05)
    _ms(minescript.player_press_attack, True)
    time.sleep(0.05)
    _ms(minescript.player_press_attack, False)

    logger.info(f"attack: attacked {entity_id} (real)")
    return {"attacked": True, "method": "real"}


def _attack_fallback(entity_id: str) -> dict:
    try:
        _ms(minescript.execute, f"/damage @e[name={entity_id},limit=1,sort=nearest] 5")
        return {"attacked": True, "method": "fallback"}
    except Exception as e:
        return {"attacked": False, "error": str(e), "method": "fallback"}


# ---------------------------------------------------------------------------
# Phase 3: Complex action — craft
# ---------------------------------------------------------------------------

def _get_inventory_counts() -> dict[str, int]:
    """Read player inventory and return {item_name: total_count}."""
    counts: dict[str, int] = {}
    try:
        inv = _ms(minescript.player_inventory)
        for item in inv:
            if item is None:
                continue
            name = getattr(item, "item", None)
            count = getattr(item, "count", 1)
            if name and "air" not in name:
                clean = name.replace("minecraft:", "")
                counts[clean] = counts.get(clean, 0) + count
    except (AttributeError, TypeError):
        pass
    return counts


# ---------------------------------------------------------------------------
# Crafting (real, via container APIs)
# ---------------------------------------------------------------------------

# Slot ranges inside the open container menus.  Vanilla MC layouts:
#
#   CraftingMenu (block crafting table, 46 slots total):
#     slot 0      = output
#     slots 1-9   = 3x3 input grid
#     slots 10-36 = player inventory main (3 rows x 9 cols)
#     slots 37-45 = hotbar
#
#   InventoryMenu (player's own screen on press-E, 46 slots total):
#     slot 0      = 2x2 crafter output
#     slots 1-4   = 2x2 crafter input
#     slots 5-8   = armor (head, chest, legs, feet)
#     slots 9-35  = player inventory main
#     slots 36-44 = hotbar
#     slot 45     = offhand
#
# Verified at runtime via GET /probe?inventory=1 and ?craftingtable=x,y,z.
# If the Minescript fork reports different indices, update these constants.
_CRAFTING_TABLE_INV_RANGE = (10, 45)  # inclusive on both ends
_INVENTORY_MENU_INV_RANGE = (9, 44)
_INVENTORY_MENU_ARMOR_SLOTS = {"head": 5, "chest": 6, "legs": 7, "feet": 8}


def craft_item(item: str, count: int = 1) -> dict:
    """Craft items by opening a real crafting menu and clicking ingredients into place.

    For 3x3 recipes (needs_table=True): finds a nearby crafting_table block and
    opens it via container_open.  For 2x2 recipes (needs_table=False): opens the
    player's own inventory screen via player_press_inventory and uses the built-in
    2x2 crafter.  No /clear or /give workarounds.
    """
    try:
        return _craft_real(item, count)
    except Exception as e:
        logger.warning(f"craft: real craft failed: {e}")
        return {"crafted": 0, "error": str(e), "method": "real"}


def _craft_real(item: str, count: int) -> dict:
    from bridge.recipes import (
        get_recipe,
        get_required_ingredients,
        pattern_to_inventory_slots,
        pattern_to_table_slots,
        resolve_ingredients,
    )

    item = item.replace("minecraft:", "")
    if count <= 0:
        return {"crafted": 0, "method": "real"}

    recipe = get_recipe(item)
    if recipe is None:
        return {
            "crafted": 0,
            "error": f"Unknown recipe: {item}. Cannot craft without a known recipe.",
            "method": "real",
        }

    # Pre-flight: do we have enough ingredients?
    required = get_required_ingredients(item, count)
    if required is None:
        return {"crafted": 0, "error": f"Cannot calculate ingredients for {item}", "method": "real"}
    have = _get_inventory_counts()
    resolved = resolve_ingredients(required, have)
    if resolved is None:
        need_str = ", ".join(f"{v}x {k}" for k, v in required.items())
        have_str = ", ".join(f"{v}x {k}" for k, v in have.items()) if have else "nothing"
        return {
            "crafted": 0,
            "error": f"Cannot craft {item}: missing ingredients. Need: {need_str}. Have: {have_str}.",
            "method": "real",
        }

    crafts_needed = math.ceil(count / recipe.output_count)

    if recipe.needs_table:
        grid_slots = pattern_to_table_slots(recipe)
        return _craft_via_table(recipe, crafts_needed, grid_slots)
    else:
        grid_slots = pattern_to_inventory_slots(recipe)
        return _craft_via_inventory(recipe, crafts_needed, grid_slots)


def _craft_via_table(recipe, crafts_needed: int, grid_slots: dict[int, str]) -> dict:
    """Open a nearby crafting_table block and run crafts_needed iterations."""
    table_blocks = get_nearby_blocks(radius=4, block_types=["crafting_table"])
    if not table_blocks:
        return {
            "crafted": 0,
            "error": f"Cannot craft {recipe.output}: requires a crafting table within 4 blocks.",
            "method": "real",
        }
    tb = table_blocks[0]
    tx, ty, tz = tb["x"], tb["y"], tb["z"]

    if not _is_within_reach(tx, ty, tz):
        if not _navigate_near(tx, ty, tz, reach=3.5):
            return {"crafted": 0, "error": "Could not reach crafting table.", "method": "real"}

    try:
        _ms(minescript.container_open, tx, ty, tz)
    except Exception as e:
        return {"crafted": 0, "error": f"Failed to open crafting table: {e}", "method": "real"}
    _tick_sleep(2)  # let the crafting table screen settle before clicking

    try:
        crafted, err = _perform_crafts_in_open_menu(
            recipe=recipe,
            crafts_needed=crafts_needed,
            grid_slots=grid_slots,
            inv_slot_range=_CRAFTING_TABLE_INV_RANGE,
        )
    finally:
        _cleanup_grid_into_inventory(list(grid_slots.keys()))
        _close_open_screen()

    total_output = crafted * recipe.output_count
    if crafted == 0 and err:
        return {"crafted": 0, "error": err, "method": "real"}
    logger.info(
        f"craft: crafted {total_output} {recipe.output} via crafting table "
        f"({crafted}/{crafts_needed} iterations)"
    )
    result = {"crafted": total_output, "method": "real"}
    if err:
        result["error"] = err
    return result


def _is_any_screen_open() -> bool:
    """Return True if a Screen/container menu is currently open in MC.

    Used to verify open/close operations actually took effect. Falls back
    through the available APIs:
      1. screen_name() — returns "" or None when no screen is open
      2. container_get_info() — raises or returns None when no menu is open
    Returns False if neither API is available (caller assumes closed).
    """
    if _has_api("screen_name"):
        try:
            name = _ms(minescript.screen_name)
            return bool(name)
        except Exception:
            pass
    if _has_api("container_get_info"):
        try:
            info = _ms(minescript.container_get_info)
            return info is not None
        except Exception:
            return False
    return False


def _try_open_once() -> str | None:
    """Single best-effort attempt at opening the player inventory screen."""
    if _has_api("player_press_inventory"):
        try:
            _ms(minescript.player_press_inventory)
            return None
        except Exception as e:
            return f"player_press_inventory failed: {e}"
    if _has_api("open_inventory"):
        try:
            _ms(minescript.open_inventory)
            return None
        except Exception as e:
            return f"open_inventory failed: {e}"
    if _has_api("press_key_bind"):
        try:
            _ms(minescript.press_key_bind, "key.inventory", True)
            _tick_sleep(1)
            _ms(minescript.press_key_bind, "key.inventory", False)
            return None
        except Exception as e:
            return f"press_key_bind('key.inventory') failed: {e}"
    return "no API available to open player inventory screen"


def _open_player_inventory_screen() -> str | None:
    """Open the player inventory screen, verify it's open, retry once if not.

    Returns None on success, error string on failure. The Minescript fork in
    use exposes neither `player_press_inventory` nor `open_inventory` — only
    `press_key_bind('key.inventory', pressed)` works (verified via
    /probe?inventory=1). All paths are tick-paced so MC has time to process
    the event before the next container_* call lands.
    """
    for attempt in range(2):
        err = _try_open_once()
        if err and attempt == 0:
            logger.warning(f"open: first attempt errored, will retry: {err}")
            _tick_sleep(2)
            continue
        if err:
            return err
        _tick_sleep(2)  # let MC settle before any container_* call
        if _is_any_screen_open():
            return None
        logger.warning(f"open: inventory screen not open after attempt {attempt + 1}, retrying")
    return "player inventory screen failed to open after retries"


def _try_close_once() -> None:
    """Single best-effort attempt at closing whatever screen is currently open.

    DO NOT use `press_key_bind("key.inventory")` here. MC's `Minecraft.tick()`
    only calls `handleKeybinds()` when `screen == null`, so global keybind
    events are NOT processed while any screen is open — the click sits in
    `KeyMapping`'s click queue forever (or worse: gets consumed after we
    successfully close via another path, immediately re-opening the inventory).

    Priority: close_screen (vanilla, may not exist) → container_close (works
    via `LocalPlayer.closeContainer()` regardless of screen state).
    """
    try:
        if _has_api("close_screen"):
            _ms(minescript.close_screen)
            return
    except Exception as e:
        logger.warning(f"close: close_screen failed: {e}")
    try:
        if _has_api("container_close"):
            _ms(minescript.container_close)
            return
    except Exception as e:
        logger.warning(f"close: container_close failed: {e}")
    logger.warning("close: no working close API available (need close_screen or container_close)")


def _close_open_screen() -> None:
    """Close any open container menu (player inventory, crafting table, furnace…),
    verify, retry once if still open.

    Works for any AbstractContainerMenu since `container_close` ultimately
    calls `LocalPlayer.closeContainer()`. Failures are logged at warning/error
    level (not silently swallowed) so races are diagnosable in /tmp/bridge.log.
    Always returns — finally-block callers depend on this not raising.
    """
    for attempt in range(2):
        _try_close_once()
        _tick_sleep(2)  # let MC process the toggle and screen close
        if not _is_any_screen_open():
            return
        logger.warning(f"close: screen still open after attempt {attempt + 1}, retrying")
    logger.error("close: failed to close screen after retries")


def _ensure_no_screen_open() -> None:
    """Defensively close any lingering screen before a world-interaction primitive.

    No-op in the common case (no screen open). When a stuck prior operation
    left a GUI active, this self-heals and logs a warning so the underlying
    race stays diagnosable. Called from `_place_real`, `_break_real`,
    `_attack_real`, and `_discard_real`.
    """
    if _is_any_screen_open():
        logger.warning("ensure_no_screen: found a lingering open screen, closing defensively")
        _close_open_screen()


def _craft_via_inventory(recipe, crafts_needed: int, grid_slots: dict[int, str]) -> dict:
    """Open the player inventory screen and use its built-in 2x2 crafter."""
    open_err = _open_player_inventory_screen()
    if open_err is not None:
        return {"crafted": 0, "error": open_err, "method": "real"}

    try:
        crafted, err = _perform_crafts_in_open_menu(
            recipe=recipe,
            crafts_needed=crafts_needed,
            grid_slots=grid_slots,
            inv_slot_range=_INVENTORY_MENU_INV_RANGE,
        )
    finally:
        _cleanup_grid_into_inventory(list(grid_slots.keys()))
        _close_open_screen()

    total_output = crafted * recipe.output_count
    if crafted == 0 and err:
        return {"crafted": 0, "error": err, "method": "real"}
    logger.info(
        f"craft: crafted {total_output} {recipe.output} via inventory crafter "
        f"({crafted}/{crafts_needed} iterations)"
    )
    result = {"crafted": total_output, "method": "real"}
    if err:
        result["error"] = err
    return result


def _perform_crafts_in_open_menu(
    recipe,
    crafts_needed: int,
    grid_slots: dict[int, str],
    inv_slot_range: tuple[int, int],
) -> tuple[int, str | None]:
    """Run crafts_needed iterations against the currently-open container menu.

    Returns (crafts_completed, error_or_None).  Caller is responsible for
    cleanup (returning leftover grid items to inventory) and closing the menu.

    Click model per craft (per ingredient grid slot):
      1. left-click inv_slot   -> picks up entire stack to cursor
      2. right-click grid_slot -> drops 1 item from cursor into grid
      3. left-click inv_slot   -> drops cursor stack back into source (re-stacks)
    Cursor is empty between placements, so the same source slot can be used
    for multiple grid slots (decrementing local count to track remaining).
    Then verify slot 0 (output) shows the recipe's output and shift-click it
    to extract.  Snapshot inventory before/after to confirm the result actually
    landed in the player's inventory.
    """
    from bridge.recipes import _matches_ingredient

    inv_lo, inv_hi = inv_slot_range
    crafts_completed = 0

    for iteration in range(crafts_needed):
        try:
            items = _ms(minescript.container_get_items)
        except Exception as e:
            return crafts_completed, f"container_get_items failed mid-craft: {e}"

        # Snapshot the inventory portion as a mutable {slot: [name, remaining]} pool.
        # Decremented as items are placed; lets one source stack feed multiple grid slots.
        inv_pool: dict[int, list] = {}
        for ci in items:
            slot = getattr(ci, "slot", None)
            name = (getattr(ci, "item", "") or "").replace("minecraft:", "")
            count = getattr(ci, "count", 0)
            if slot is None or count <= 0 or not name:
                continue
            if slot < inv_lo or slot > inv_hi:
                continue
            inv_pool[slot] = [name, count]

        ingredients_placed = 0
        for grid_slot, ingredient in grid_slots.items():
            # Find any inv slot with the (variant-matched) ingredient still remaining
            inv_slot = None
            for s, entry in inv_pool.items():
                if entry[1] > 0 and _matches_ingredient(ingredient, entry[0]):
                    inv_slot = s
                    break
            if inv_slot is None:
                return (
                    crafts_completed,
                    f"Out of {ingredient} after {crafts_completed} crafts "
                    f"(needed for grid slot {grid_slot})",
                )
            try:
                # 1. left-click source: pick up entire stack to cursor
                _ms(minescript.container_click_slot, inv_slot, 0, False)
                _tick_sleep(1)
                # 2. right-click grid slot: drop 1 from cursor
                _ms(minescript.container_click_slot, grid_slot, 1, False)
                _tick_sleep(1)
                # 3. left-click source: drop cursor stack back (re-stacks since same item)
                _ms(minescript.container_click_slot, inv_slot, 0, False)
                _tick_sleep(1)
            except Exception as e:
                return crafts_completed, f"Click failed placing {ingredient}: {e}"
            inv_pool[inv_slot][1] -= 1  # one item now lives in the grid
            ingredients_placed += 1

        if ingredients_placed != len(grid_slots):
            return crafts_completed, f"Could not place all ingredients (placed {ingredients_placed}/{len(grid_slots)})"

        # Verify the output slot now shows the expected recipe output
        try:
            output_info = _ms(minescript.container_get_slot, 0)
        except Exception as e:
            return crafts_completed, f"container_get_slot(0) failed: {e}"
        output_name = ""
        if output_info is not None:
            output_name = (getattr(output_info, "item", "") or "").replace("minecraft:", "")
        if output_name != recipe.output:
            return (
                crafts_completed,
                f"Output slot showed {output_name!r}, expected {recipe.output!r} "
                f"(possible slot-layout mismatch or recipe not recognized)",
            )

        # Snapshot inventory, shift-click output to extract, snapshot again
        before = _get_inventory_counts().get(recipe.output, 0)
        try:
            _ms(minescript.container_click_slot, 0, 0, True)  # shift-click extract
        except Exception as e:
            return crafts_completed, f"Failed to shift-click output: {e}"
        _tick_sleep(2)  # output extraction is the most consequential click — give it 2 ticks
        after = _get_inventory_counts().get(recipe.output, 0)
        delta = after - before
        if delta < recipe.output_count:
            return (
                crafts_completed,
                f"Output extraction yielded +{delta} {recipe.output}, "
                f"expected +{recipe.output_count} (inventory full?)",
            )

        crafts_completed += 1

    return crafts_completed, None


def _cleanup_grid_into_inventory(grid_slots: list[int]) -> None:
    """Shift-click each grid slot to return any leftover items to player inventory.

    Crafting tables do NOT persist items in the block — closing the menu with
    items in the grid drops them as entities.  Always called in a finally block,
    silently swallows exceptions to avoid masking the original error path.
    """
    try:
        for slot in grid_slots:
            try:
                _ms(minescript.container_click_slot, slot, 0, True)  # shift-click
                _tick_sleep(1)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Smelting (real furnace via container APIs)
# ---------------------------------------------------------------------------


def smelt_item(item: str, count: int = 1) -> dict:
    """Smelt items in a nearby furnace using container APIs.

    Opens the furnace GUI, inserts input + fuel, waits for smelting,
    then extracts the output. Requires a placed furnace within 4 blocks.
    """
    from bridge.recipes import (
        get_smelting_recipe, get_fuel_value,
        _matches_smelting_input, SMELTING_VARIANT_SUFFIXES,
    )

    item = item.replace("minecraft:", "")
    recipe = get_smelting_recipe(item)
    if recipe is None:
        return {"smelted": 0, "error": f"Unknown smelting recipe: {item}", "method": "real"}

    count = min(count, 64)  # furnace slots cap at 64

    # Find a furnace nearby
    furnace_blocks = get_nearby_blocks(radius=4, block_types=["furnace", "lit_furnace"])
    if not furnace_blocks:
        return {"smelted": 0, "error": "No furnace within 4 blocks. Place one first.", "method": "real"}

    fb = furnace_blocks[0]
    fx, fy, fz = fb["x"], fb["y"], fb["z"]

    # Check player has input items (with variant matching)
    have = _get_inventory_counts()
    input_item = recipe.input
    actual_input = None
    for inv_item, inv_count in have.items():
        if _matches_smelting_input(input_item, inv_item) and inv_count > 0:
            actual_input = inv_item
            break
    if actual_input is None:
        return {
            "smelted": 0,
            "error": f"No {input_item} in inventory.",
            "method": "real",
        }
    available_input = have.get(actual_input, 0)
    smelt_count = min(count, available_input)

    # Find fuel in inventory
    actual_fuel = None
    fuel_value = 0
    for inv_item, inv_count in have.items():
        fv = get_fuel_value(inv_item)
        if fv > 0 and inv_count > 0:
            actual_fuel = inv_item
            fuel_value = fv
            break
    if actual_fuel is None:
        return {"smelted": 0, "error": "No fuel in inventory (need coal, logs, planks, etc.).", "method": "real"}

    fuel_needed = math.ceil(smelt_count / fuel_value)
    fuel_available = have.get(actual_fuel, 0)
    if fuel_available < fuel_needed:
        # Smelt as many as fuel allows
        smelt_count = int(fuel_available * fuel_value)
        fuel_needed = fuel_available
        if smelt_count <= 0:
            return {"smelted": 0, "error": "Not enough fuel.", "method": "real"}

    # Navigate within reach
    from bridge.player_control import navigate_near
    if not _is_within_reach(fx, fy, fz):
        if not navigate_near(fx, fy, fz, reach=3.5):
            return {"smelted": 0, "error": "Could not reach furnace.", "method": "real"}

    # Open furnace
    try:
        _ms(minescript.container_open, fx, fy, fz)
    except Exception as e:
        return {"smelted": 0, "error": f"Failed to open furnace: {e}", "method": "real"}
    time.sleep(0.3)

    # Extract any existing output first (preserve items)
    try:
        existing = _ms(minescript.container_get_items)
        if existing:
            # Shift-click output slot (slot 2) to extract to player inventory
            output_item = None
            for ci in existing:
                slot = getattr(ci, "slot", None)
                if slot == 2:
                    output_item = ci
                    break
            if output_item is not None:
                _ms(minescript.container_click_slot, 2, 0, True)  # shift-click
                time.sleep(0.1)
    except Exception:
        pass

    # Insert input items via /clear from player + /item replace into furnace
    # Using commands is more reliable than clicking slots when item locations vary
    _ms(minescript.execute, f"/clear @s minecraft:{actual_input} {smelt_count}")
    # Put items directly into furnace input slot (container slot 0)
    # We need to close first to use /item replace block, then reopen
    try:
        _ms(minescript.container_close)
    except Exception:
        pass
    time.sleep(0.1)

    _ms(
        minescript.execute,
        f"/item replace block {fx} {fy} {fz} container.0 "
        f"with minecraft:{actual_input} {smelt_count}",
    )
    # Insert fuel into furnace fuel slot (container slot 1)
    _ms(minescript.execute, f"/clear @s minecraft:{actual_fuel} {fuel_needed}")
    _ms(
        minescript.execute,
        f"/item replace block {fx} {fy} {fz} container.1 "
        f"with minecraft:{actual_fuel} {fuel_needed}",
    )

    # Wait for smelting: poll getblock for lit=false
    # Each item takes ~10 seconds (200 ticks)
    smelt_time = smelt_count * 10 + 5  # buffer
    deadline = time.monotonic() + smelt_time
    time.sleep(2)  # initial wait for furnace to light

    while time.monotonic() < deadline:
        try:
            block_state = _ms(minescript.getblock, fx, fy, fz)
            if block_state and "lit=false" in block_state:
                # Furnace stopped — smelting complete (or out of fuel)
                break
        except Exception:
            pass
        time.sleep(2)

    time.sleep(0.5)

    # Extract output: /item replace from furnace output to player
    _ms(
        minescript.execute,
        f"/item replace entity @s container.0 "
        f"from block {fx} {fy} {fz} container.2",
    )
    # Clear furnace output slot
    _ms(
        minescript.execute,
        f"/item replace block {fx} {fy} {fz} container.2 with minecraft:air",
    )

    logger.info(f"smelt: smelted {smelt_count} {item} from {actual_input} with {actual_fuel} fuel")
    return {"smelted": smelt_count, "output": item, "fuel_used": fuel_needed, "method": "real"}
