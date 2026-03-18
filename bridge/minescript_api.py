"""Typed wrapper around minescript.* calls. Returns dicts for JSON serialization.

All functions are blocking — the server layer calls them via run_in_executor().
The `minescript` module is only importable inside the Minescript mod's Python runtime.

Updated for Minescript v5.0b11 API (dataclass returns, not dicts).
"""

from __future__ import annotations

import json
import math

import minescript


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
    """Scan blocks in a sphere around the player. Skip air."""
    pos = minescript.player_position()
    px, py, pz = int(pos[0]), int(pos[1]), int(pos[2])
    blocks = []

    # Try batch API first
    try:
        positions = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist <= radius:
                        positions.append((px + dx, py + dy, pz + dz))

        block_list = minescript.getblocklist([list(p) for p in positions])
        for bpos, name in zip(positions, block_list):
            if name and "air" not in name:
                bx, by, bz = bpos
                dist = math.sqrt((bx - px) ** 2 + (by - py) ** 2 + (bz - pz) ** 2)
                blocks.append({
                    "name": name.replace("minecraft:", ""),
                    "x": bx, "y": by, "z": bz,
                    "distance": round(dist, 1),
                })
        return blocks
    except (AttributeError, TypeError):
        pass

    # Fallback: single getblock calls
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                if dist > radius:
                    continue
                bx, by, bz = px + dx, py + dy, pz + dz
                try:
                    name = minescript.getblock(bx, by, bz)
                    if name and "air" not in name:
                        blocks.append({
                            "name": name.replace("minecraft:", ""),
                            "x": bx, "y": by, "z": bz,
                            "distance": round(dist, 1),
                        })
                except Exception:
                    continue

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
    if message.startswith("#"):
        # Baritone commands must go through chat, not /say
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


def place_block(block: str, x: int, y: int, z: int, face: str = "top") -> dict:
    """Attempt to place a block. Uses execute command as fallback."""
    try:
        minescript.execute(f"/setblock {x} {y} {z} minecraft:{block}")
        return {"placed": True}
    except Exception as e:
        return {"placed": False, "error": str(e)}


def break_block(x: int, y: int, z: int) -> dict:
    """Break a block at coordinates. Uses execute command."""
    try:
        name = minescript.getblock(x, y, z)
        minescript.execute(f"/setblock {x} {y} {z} minecraft:air destroy")
        return {"broken": True, "block": name.replace("minecraft:", "") if name else "unknown"}
    except Exception as e:
        return {"broken": False, "error": str(e)}


def attack_entity(entity_id: str) -> dict:
    """Attack an entity. Limited without direct player action API."""
    try:
        minescript.execute(f"/damage @e[name={entity_id},limit=1,sort=nearest] 5")
        return {"attacked": True}
    except Exception as e:
        return {"attacked": False, "error": str(e)}


def craft_item(item: str, count: int = 1) -> dict:
    """Craft items via server command (MVP — requires op)."""
    try:
        minescript.execute(f"/give @s minecraft:{item} {count}")
        return {"crafted": count}
    except Exception as e:
        return {"crafted": 0, "error": str(e)}


def equip_item(item: str, slot: str = "hand") -> dict:
    """Equip an item to a slot. MVP uses commands."""
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
        return {"equipped": True}
    except Exception as e:
        return {"equipped": False, "error": str(e)}


def discard_item(item: str, count: int = 1) -> dict:
    """Discard items from inventory. MVP uses clear command."""
    try:
        minescript.execute(f"/clear @s minecraft:{item} {count}")
        return {"discarded": count}
    except Exception as e:
        return {"discarded": 0, "error": str(e)}
