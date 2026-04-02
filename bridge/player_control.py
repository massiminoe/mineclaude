"""Shared player-control helpers for real Minescript actions.

All functions are blocking — called via run_in_executor() from the server layer.
The `minescript` module is only importable inside the Minescript mod's Python runtime.

API names confirmed via probe against Minescript v5.0b11:
  - player_set_orientation, player_orientation, player_look_at
  - player_press_attack, player_press_use, player_press_drop
  - player_inventory_select_slot, player_inventory_slot_to_hotbar
  - player_press_swap_hands, player_press_pick_item
  - screen_name, container_get_items (read-only, no container_click)
  - NO: container_click, close_screen, player_press_inventory, open_inventory
"""

from __future__ import annotations

import logging
import math
import time

import minescript

logger = logging.getLogger("bridge")


def look_at_block(x: int, y: int, z: int) -> None:
    """Look at block center. Uses player_look_at if available, else manual math."""
    try:
        # player_look_at takes (x, y, z) world coordinates
        minescript.player_look_at(x + 0.5, y + 0.5, z + 0.5)
        return
    except (AttributeError, TypeError):
        pass

    # Fallback: manual yaw/pitch calculation
    pos = minescript.player_position()
    px, py, pz = pos[0], pos[1] + 1.62, pos[2]  # eye height

    dx = (x + 0.5) - px
    dy = (y + 0.5) - py
    dz = (z + 0.5) - pz
    dist_xz = math.sqrt(dx * dx + dz * dz)

    yaw = -math.degrees(math.atan2(dx, dz))
    pitch = -math.degrees(math.atan2(dy, dist_xz))
    minescript.player_set_orientation(yaw, pitch)


def look_at_position(x: float, y: float, z: float) -> None:
    """Look at an arbitrary world position."""
    try:
        minescript.player_look_at(x, y, z)
        return
    except (AttributeError, TypeError):
        pass

    pos = minescript.player_position()
    px, py, pz = pos[0], pos[1] + 1.62, pos[2]

    dx = x - px
    dy = y - py
    dz = z - pz
    dist_xz = math.sqrt(dx * dx + dz * dz)

    yaw = -math.degrees(math.atan2(dx, dz))
    pitch = -math.degrees(math.atan2(dy, dist_xz))
    minescript.player_set_orientation(yaw, pitch)


def look_at_entity(entity_name: str) -> bool:
    """Find entity by name and look at it. Returns True if found."""
    try:
        ents = minescript.entities()
    except (AttributeError, TypeError):
        return False

    for ent in ents:
        name = str(getattr(ent, "name", "")).replace("minecraft:", "")
        etype = str(getattr(ent, "type", "")).replace("minecraft:", "")
        if entity_name.lower() in (name.lower(), etype.lower()):
            ex, ey, ez = ent.position
            look_at_position(ex, ey + 0.9, ez)
            return True
    return False


def find_item_slot(item_name: str) -> int | None:
    """Find an item in inventory by name. Returns slot number or None.

    Minecraft inventory slots:
    - 0-8: hotbar
    - 9-35: main inventory
    - 36-39: armor (boots, legs, chest, head)
    - 40: offhand
    """
    try:
        inv = minescript.player_inventory()
    except (AttributeError, TypeError):
        return None

    item_name_lower = item_name.lower().replace("minecraft:", "")
    for item in inv:
        if item is None:
            continue
        name = getattr(item, "item", "")
        if name and item_name_lower in name.lower().replace("minecraft:", ""):
            slot = getattr(item, "slot", None)
            if slot is not None:
                return slot
    return None


def find_item_in_hotbar(item_name: str) -> int | None:
    """Find an item specifically in the hotbar (slots 0-8). Returns hotbar slot or None."""
    try:
        inv = minescript.player_inventory()
    except (AttributeError, TypeError):
        return None

    item_name_lower = item_name.lower().replace("minecraft:", "")
    for item in inv:
        if item is None:
            continue
        name = getattr(item, "item", "")
        slot = getattr(item, "slot", None)
        if name and slot is not None and 0 <= slot <= 8:
            if item_name_lower in name.lower().replace("minecraft:", ""):
                return slot
    return None


def move_item_to_hotbar(inv_slot: int, hotbar_slot: int = 0) -> bool:
    """Move an item from any inventory slot to the currently selected hotbar slot.

    Uses player_inventory_slot_to_hotbar (v5.0b11 — takes 1 arg: source slot).
    First selects the target hotbar slot, then swaps.
    """
    try:
        minescript.player_inventory_select_slot(hotbar_slot)
        time.sleep(0.05)
        minescript.player_inventory_slot_to_hotbar(inv_slot)
        time.sleep(0.1)
        return True
    except Exception as e:
        logger.warning(f"move_item_to_hotbar failed: {e}")
        return False


def select_item_in_hotbar(item_name: str) -> bool:
    """Find item, move to hotbar if needed, select it. Returns True on success."""
    # Check hotbar first
    hotbar_slot = find_item_in_hotbar(item_name)
    if hotbar_slot is not None:
        minescript.player_inventory_select_slot(hotbar_slot)
        return True

    # Check full inventory
    slot = find_item_slot(item_name)
    if slot is None:
        return False

    # Move to hotbar slot 0 and select
    if move_item_to_hotbar(slot, 0):
        minescript.player_inventory_select_slot(0)
        return True
    return False


def wait_for_block_change(x: int, y: int, z: int, timeout: float = 10.0) -> str | None:
    """Poll getblock() until value changes. Returns new block or None on timeout."""
    try:
        original = minescript.getblock(x, y, z)
    except Exception:
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = minescript.getblock(x, y, z)
            if current != original:
                return current
        except Exception:
            pass
        time.sleep(0.1)
    return None


def is_within_reach(x: float, y: float, z: float, reach: float = 4.5) -> bool:
    """Check if coordinates are within player's reach distance."""
    pos = minescript.player_position()
    px, py, pz = pos[0], pos[1], pos[2]
    dist = math.sqrt((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2)
    return dist <= reach


def get_player_distance(x: float, y: float, z: float) -> float:
    """Euclidean distance from player to coordinates."""
    pos = minescript.player_position()
    px, py, pz = pos[0], pos[1], pos[2]
    return math.sqrt((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2)


def navigate_near(x: float, y: float, z: float, reach: float = 3.5) -> bool:
    """Use Baritone to navigate within reach of target. Blocks until arrival or timeout."""
    if is_within_reach(x, y, z, reach):
        return True

    minescript.chat(f"#goto {int(x)} {int(y)} {int(z)}")

    # Wait for arrival with timeout
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if is_within_reach(x, y, z, reach):
            minescript.chat("#stop")
            time.sleep(0.2)
            return True
    minescript.chat("#stop")
    return False


def collect_nearby_item(
    near_x: float, near_y: float, near_z: float,
    search_radius: float = 5.0,
    timeout: float = 4.0,
) -> bool:
    """Find a dropped item entity near coordinates and walk to pick it up."""
    # Brief pause for item entity to spawn
    time.sleep(0.2)

    # Find closest item entity near the expected drop position
    try:
        entities = minescript.entities(max_distance=search_radius)
    except Exception:
        return False

    best = None
    best_dist = float("inf")
    for ent in entities:
        if str(ent.type).replace("minecraft:", "") != "item":
            continue
        ex, ey, ez = ent.position
        dist = math.sqrt((ex - near_x) ** 2 + (ey - near_y) ** 2 + (ez - near_z) ** 2)
        if dist < best_dist:
            best_dist = dist
            best = (ex, ey, ez)

    if best is None:
        return False

    # Already close enough to pick up
    if is_within_reach(best[0], best[1], best[2], 1.0):
        return True

    # Walk to the item entity's actual position
    minescript.chat(f"#goto {int(best[0])} {int(best[1])} {int(best[2])}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if is_within_reach(best[0], best[1], best[2], 1.5):
            minescript.chat("#stop")
            time.sleep(0.2)
            return True
    minescript.chat("#stop")
    return False


def find_adjacent_solid_block(x: int, y: int, z: int) -> tuple[int, int, int, str] | None:
    """Find a solid block adjacent to (x,y,z) that we can click to place against.

    Returns (adj_x, adj_y, adj_z, face) where face is the click face,
    or None if no solid neighbor found.
    """
    neighbors = [
        ((0, -1, 0), "top"),     # block below → click its top face
        ((0, 1, 0), "bottom"),   # block above → click its bottom face
        ((-1, 0, 0), "east"),    # block to west → click its east face
        ((1, 0, 0), "west"),     # block to east → click its west face
        ((0, 0, -1), "south"),   # block to north → click its south face
        ((0, 0, 1), "north"),    # block to south → click its north face
    ]

    for (dx, dy, dz), face in neighbors:
        ax, ay, az = x + dx, y + dy, z + dz
        try:
            block = minescript.getblock(ax, ay, az)
            if block and "air" not in block:
                return (ax, ay, az, face)
        except Exception:
            continue
    return None
