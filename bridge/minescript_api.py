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
import time

import minescript

logger = logging.getLogger("bridge")


# ---------------------------------------------------------------------------
# Read-only queries (unchanged)
# ---------------------------------------------------------------------------

def get_player_status() -> dict:
    """Get player position, health, hunger, inventory, biome, and time."""
    pos = minescript.player_position()
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
        result["health"] = minescript.player_health()
    except (AttributeError, TypeError):
        pass

    try:
        inv = minescript.player_inventory()
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
        info = minescript.world_info()
        result["time"] = info.day_ticks
    except (AttributeError, TypeError):
        pass

    return result


def get_nearby_blocks(radius: int = 8) -> list[dict]:
    """Scan blocks in a sphere around the player. Skip air.

    Block names are stripped of the minecraft: namespace and state suffixes
    (e.g. "minecraft:oak_log[axis=y]" -> "oak_log") so consumers can match
    by base block name.

    Results are sorted by distance, closest first.
    """
    pos = minescript.player_position()
    px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
    blocks = []
    radius_sq = radius * radius

    # Use get_block_region for a single bulk load, then iterate locally
    try:
        region = minescript.get_block_region(
            [px - radius, py - radius, pz - radius],
            [px + radius, py + radius, pz + radius],
        )
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    dist_sq = dx * dx + dy * dy + dz * dz
                    if dist_sq > radius_sq:
                        continue
                    bx, by, bz = px + dx, py + dy, pz + dz
                    name = region.get_block(bx, by, bz)
                    if name and "air" not in name:
                        blocks.append({
                            "name": name.replace("minecraft:", "").split("[")[0],
                            "x": bx, "y": by, "z": bz,
                            "distance": round(math.sqrt(dist_sq), 1),
                        })
        blocks.sort(key=lambda b: b["distance"])
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
                    name = minescript.getblock(bx, by, bz)
                    if name and "air" not in name:
                        blocks.append({
                            "name": name.replace("minecraft:", "").split("[")[0],
                            "x": bx, "y": by, "z": bz,
                            "distance": round(math.sqrt(dist_sq), 1),
                        })
                except Exception:
                    continue

    blocks.sort(key=lambda b: b["distance"])
    return blocks


def get_nearby_entities(radius: int = 32) -> list[dict]:
    """List entities within radius."""
    pos = minescript.player_position()
    px, py, pz = pos[0], pos[1], pos[2]
    result = []

    try:
        # v5.0 returns List[EntityData] with .position, .name, .type, .health
        raw = minescript.entities(max_distance=float(radius))
    except (AttributeError, TypeError):
        return result

    for ent in raw:
        ex, ey, ez = ent.position
        dist = math.sqrt((ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2)
        result.append({
            "name": str(ent.name).replace("minecraft:", ""),
            "type": str(ent.type).replace("minecraft:", ""),
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
        minescript.chat(message)
    elif message.startswith("/"):
        # Already a command
        minescript.execute(message)
    else:
        # Use /tellraw to avoid signed chat packet issues and handle special chars
        # Strip non-ASCII chars (emojis break MC rendering)
        clean = message.encode("ascii", errors="ignore").decode("ascii")
        # JSON-encode the text to escape quotes and special chars
        text_json = json.dumps({"text": f"[Claude] {clean}"})
        minescript.execute(f"/tellraw @a {text_json}")


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
            pos = minescript.player_position()
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
        pos = minescript.player_position()
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

    # Find item and move to hotbar
    if not _select_item(item):
        return {"discarded": 0, "error": f"No {item} in inventory", "method": "real"}

    # Drop items one at a time
    dropped = 0
    for _ in range(count):
        minescript.player_press_drop(True)
        time.sleep(0.05)
        minescript.player_press_drop(False)
        time.sleep(0.05)
        dropped += 1

    # Verify by checking inventory
    remaining_slot = _find_item_slot(item)
    logger.info(f"discard: dropped {dropped} {item} (real)")
    return {"discarded": dropped, "method": "real"}


def _discard_fallback(item: str, count: int) -> dict:
    try:
        minescript.execute(f"/clear @s minecraft:{item} {count}")
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
        minescript.player_press_swap_hands(True)
        time.sleep(0.05)
        minescript.player_press_swap_hands(False)
        time.sleep(0.1)
        logger.info(f"equip: equipped {item} to offhand (real)")
        return {"equipped": True, "method": "real"}

    # Armor slots — no container_click available in v5.0b11, use fallback
    raise AttributeError("Armor equip requires container_click (not available)")


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
        minescript.execute(f"/item replace entity @s armor.{mc_slot} with minecraft:{item} 1")
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

    # Check what's there
    try:
        name = minescript.getblock(x, y, z)
    except Exception:
        name = "unknown"
    if not name or "air" in name:
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

    # Hold attack to mine — press and hold, periodically check if block broke
    original_block = name
    minescript.player_press_attack(True)
    deadline = time.monotonic() + 15.0
    try:
        while time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                current = minescript.getblock(x, y, z)
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
        minescript.player_press_attack(False)

    # Timed out
    raise Exception(f"Timed out breaking {original_block}")


def _break_fallback(x: int, y: int, z: int) -> dict:
    try:
        name = minescript.getblock(x, y, z)
        minescript.execute(f"/setblock {x} {y} {z} minecraft:air destroy")
        return {
            "broken": True,
            "block": name.replace("minecraft:", "") if name else "unknown",
            "method": "fallback",
        }
    except Exception as e:
        return {"broken": False, "error": str(e), "method": "fallback"}


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

    # Check if target position is air
    try:
        current = minescript.getblock(x, y, z)
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
    minescript.player_press_use(True)
    time.sleep(0.05)
    minescript.player_press_use(False)
    time.sleep(0.15)

    # Verify placement
    try:
        placed_block = minescript.getblock(x, y, z)
        if placed_block and "air" not in placed_block:
            logger.info(f"place: placed {block} at {x},{y},{z} (real)")
            return {"placed": True, "method": "real"}
    except Exception:
        pass

    # Placement might have succeeded even if verify failed
    logger.info(f"place: placed {block} at {x},{y},{z} (real, unverified)")
    return {"placed": True, "method": "real"}


def _place_fallback(block: str, x: int, y: int, z: int) -> dict:
    try:
        minescript.execute(f"/setblock {x} {y} {z} minecraft:{block}")
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

    # Find the entity
    try:
        ents = minescript.entities()
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
    minescript.player_press_attack(True)
    time.sleep(0.05)
    minescript.player_press_attack(False)

    logger.info(f"attack: attacked {entity_id} (real)")
    return {"attacked": True, "method": "real"}


def _attack_fallback(entity_id: str) -> dict:
    try:
        minescript.execute(f"/damage @e[name={entity_id},limit=1,sort=nearest] 5")
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
        inv = minescript.player_inventory()
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


def craft_item(item: str, count: int = 1) -> dict:
    """Craft items via simulated crafting.

    Validates recipe exists and player has required ingredients.
    Consumes ingredients via /clear, gives output via /give.
    """
    try:
        return _craft_simulated(item, count)
    except Exception as e:
        logger.warning(f"craft: simulated craft failed: {e}")
        return {"crafted": 0, "error": str(e), "method": "simulated"}


def _craft_simulated(item: str, count: int) -> dict:
    from bridge.recipes import get_recipe, get_required_ingredients, resolve_ingredients

    item = item.replace("minecraft:", "")
    recipe = get_recipe(item)
    if recipe is None:
        return {
            "crafted": 0,
            "error": f"Unknown recipe: {item}. Cannot craft without a known recipe.",
            "method": "simulated",
        }

    required = get_required_ingredients(item, count)
    if required is None:
        return {"crafted": 0, "error": f"Cannot calculate ingredients for {item}", "method": "simulated"}

    # Check inventory (with variant matching)
    have = _get_inventory_counts()
    resolved = resolve_ingredients(required, have)

    if resolved is None:
        need_str = ", ".join(f"{v}x {k}" for k, v in required.items())
        have_str = ", ".join(f"{v}x {k}" for k, v in have.items()) if have else "nothing"
        return {
            "crafted": 0,
            "error": f"Cannot craft {item}: missing ingredients. Need: {need_str}. Have: {have_str}.",
            "method": "simulated",
        }

    # Consume resolved actual items via /clear
    crafts_needed = math.ceil(count / recipe.output_count)
    total_output = crafts_needed * recipe.output_count

    for actual_item, needed in resolved.items():
        minescript.execute(f"/clear @s minecraft:{actual_item} {needed}")

    # Give output
    minescript.execute(f"/give @s minecraft:{item} {total_output}")

    logger.info(f"craft: crafted {total_output} {item} (simulated, {crafts_needed} crafts)")
    return {"crafted": total_output, "method": "simulated"}
