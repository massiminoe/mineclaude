"""Shared player-control helpers for real Minescript actions.

All functions are blocking — called via run_in_executor() from the server layer.
The `minescript` module is only importable inside the Minescript mod's Python runtime.

API names confirmed via probe against Minescript v5.0b11 + container PR:
  - player_set_orientation, player_orientation, player_look_at
  - player_press_attack, player_press_use, player_press_drop
  - player_inventory_select_slot, player_inventory_slot_to_hotbar (broken on MC 1.21.5)
  - player_press_swap_hands, player_press_pick_item
  - container_open, container_close, container_click_slot, container_swap_slots
  - container_get_items, container_get_slot, container_get_info, container_find_item
  - screen_name
"""

from __future__ import annotations

import logging
import math
import time

import minescript

from bridge.minescript_api import _ms

logger = logging.getLogger("bridge")


def look_at_block(x: int, y: int, z: int) -> None:
    """Look at block center. Uses player_look_at if available, else manual math."""
    try:
        # player_look_at takes (x, y, z) world coordinates
        _ms(minescript.player_look_at, x + 0.5, y + 0.5, z + 0.5)
        return
    except (AttributeError, TypeError):
        pass

    # Fallback: manual yaw/pitch calculation
    pos = _ms(minescript.player_position)
    px, py, pz = pos[0], pos[1] + 1.62, pos[2]  # eye height

    dx = (x + 0.5) - px
    dy = (y + 0.5) - py
    dz = (z + 0.5) - pz
    dist_xz = math.sqrt(dx * dx + dz * dz)

    yaw = -math.degrees(math.atan2(dx, dz))
    pitch = -math.degrees(math.atan2(dy, dist_xz))
    _ms(minescript.player_set_orientation, yaw, pitch)


def look_at_position(x: float, y: float, z: float) -> None:
    """Look at an arbitrary world position."""
    try:
        _ms(minescript.player_look_at, x, y, z)
        return
    except (AttributeError, TypeError):
        pass

    pos = _ms(minescript.player_position)
    px, py, pz = pos[0], pos[1] + 1.62, pos[2]

    dx = x - px
    dy = y - py
    dz = z - pz
    dist_xz = math.sqrt(dx * dx + dz * dz)

    yaw = -math.degrees(math.atan2(dx, dz))
    pitch = -math.degrees(math.atan2(dy, dist_xz))
    _ms(minescript.player_set_orientation, yaw, pitch)


def look_at_entity(entity_name: str) -> bool:
    """Find entity by name and look at it. Returns True if found."""
    try:
        ents = _ms(minescript.entities)
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
        inv = _ms(minescript.player_inventory)
    except (AttributeError, TypeError):
        return None

    item_name_lower = item_name.lower().replace("minecraft:", "")
    for i, item in enumerate(inv):
        if item is None:
            continue
        name = getattr(item, "item", "")
        if name and item_name_lower in name.lower().replace("minecraft:", ""):
            slot = getattr(item, "slot", None)
            if slot is not None:
                return slot
            return i  # fallback to enumeration index
    return None


def find_item_in_hotbar(item_name: str) -> int | None:
    """Find an item specifically in the hotbar (slots 0-8). Returns hotbar slot or None."""
    try:
        inv = _ms(minescript.player_inventory)
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


def _slot_to_item_name(mc_slot: int) -> str:
    """/item replace slot name for a Minecraft inventory slot number."""
    if 0 <= mc_slot <= 8:
        return f"hotbar.{mc_slot}"
    elif 9 <= mc_slot <= 35:
        return f"inventory.{mc_slot - 9}"
    elif mc_slot == 40:
        return "weapon.offhand"
    else:
        raise ValueError(f"Unsupported slot: {mc_slot}")


def _find_empty_hotbar_slot() -> int | None:
    """Find an empty hotbar slot (0-8). Returns slot number or None."""
    try:
        inv = _ms(minescript.player_inventory)
    except (AttributeError, TypeError):
        return None

    occupied = set()
    for item in inv:
        if item is None:
            continue
        name = getattr(item, "item", "")
        slot = getattr(item, "slot", None)
        if name and "air" not in name and slot is not None and 0 <= slot <= 8:
            occupied.add(slot)
    for s in range(9):
        if s not in occupied:
            return s
    return None


def _find_empty_inventory_slot() -> int | None:
    """Find any empty slot in main inventory (9-35). Returns slot number or None."""
    try:
        inv = _ms(minescript.player_inventory)
    except (AttributeError, TypeError):
        return None

    occupied = set()
    for item in inv:
        if item is None:
            continue
        name = getattr(item, "item", "")
        slot = getattr(item, "slot", None)
        if name and "air" not in name and slot is not None and 9 <= slot <= 35:
            occupied.add(slot)
    for s in range(9, 36):
        if s not in occupied:
            return s
    return None


def move_item_to_hotbar(inv_slot: int, hotbar_slot: int = 0) -> bool:
    """Move an item from any inventory slot to a hotbar slot (lossless).

    Uses /item replace commands (bot is opped) since player_inventory_slot_to_hotbar
    is broken on MC 1.21.5.  Slot-to-slot copies preserve full NBT/durability.

    Strategy:
    1. If item already in hotbar, just select it.
    2. Try an empty hotbar slot first (no displacement).
    3. If hotbar full, save the displaced item to an empty inventory slot first.
    4. If inventory 100% full, fail.
    """
    if 0 <= inv_slot <= 8:
        # Already in hotbar
        _ms(minescript.player_inventory_select_slot, inv_slot)
        return True

    if inv_slot < 9 or inv_slot > 35:
        logger.warning(f"move_item_to_hotbar: unsupported slot {inv_slot}")
        return False

    src = _slot_to_item_name(inv_slot)

    # Prefer an empty hotbar slot to avoid displacing anything
    empty_hotbar = _find_empty_hotbar_slot()
    if empty_hotbar is not None:
        hotbar_slot = empty_hotbar

    dst = _slot_to_item_name(hotbar_slot)

    try:
        if empty_hotbar is None:
            # Hotbar full — save the displaced item to an empty inventory slot
            temp = _find_empty_inventory_slot()
            if temp is None:
                logger.warning("move_item_to_hotbar: inventory full, cannot swap")
                return False
            temp_name = _slot_to_item_name(temp)
            # Save hotbar item to temp (copy preserves NBT)
            _ms(minescript.execute, f"/item replace entity @s {temp_name} from entity @s {dst}")

        # Copy source item to hotbar
        _ms(minescript.execute, f"/item replace entity @s {dst} from entity @s {src}")
        # Clear source (from copies, doesn't move)
        _ms(minescript.execute, f"/item replace entity @s {src} with minecraft:air")
        time.sleep(0.05)
        _ms(minescript.player_inventory_select_slot, hotbar_slot)
        return True
    except Exception as e:
        logger.warning(f"move_item_to_hotbar failed: {e}")
        return False


def select_item_in_hotbar(item_name: str) -> bool:
    """Find item, move to hotbar if needed, select it. Returns True on success."""
    # Check hotbar first
    hotbar_slot = find_item_in_hotbar(item_name)
    if hotbar_slot is not None:
        _ms(minescript.player_inventory_select_slot, hotbar_slot)
        return True

    # Check full inventory
    slot = find_item_slot(item_name)
    if slot is None:
        return False

    # Move to hotbar slot 0 and select
    if move_item_to_hotbar(slot, 0):
        _ms(minescript.player_inventory_select_slot, 0)
        return True
    return False


def wait_for_block_change(x: int, y: int, z: int, timeout: float = 10.0) -> str | None:
    """Poll getblock() until value changes. Returns new block or None on timeout."""
    try:
        original = _ms(minescript.getblock, x, y, z)
    except Exception:
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = _ms(minescript.getblock, x, y, z)
            if current != original:
                return current
        except Exception:
            pass
        time.sleep(0.1)
    return None


def is_within_reach(x: float, y: float, z: float, reach: float = 4.5) -> bool:
    """Check if coordinates are within player's reach distance."""
    pos = _ms(minescript.player_position)
    px, py, pz = pos[0], pos[1], pos[2]
    dist = math.sqrt((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2)
    return dist <= reach


def get_player_distance(x: float, y: float, z: float) -> float:
    """Euclidean distance from player to coordinates."""
    pos = _ms(minescript.player_position)
    px, py, pz = pos[0], pos[1], pos[2]
    return math.sqrt((x - px) ** 2 + (y - py) ** 2 + (z - pz) ** 2)


def navigate_near(x: float, y: float, z: float, reach: float = 3.5) -> bool:
    """Use Baritone to navigate within reach of target. Blocks until arrival or timeout."""
    if is_within_reach(x, y, z, reach):
        return True

    _ms(minescript.chat, f"#goto {int(x)} {int(y)} {int(z)}")

    # Wait for arrival with timeout
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if is_within_reach(x, y, z, reach):
            _ms(minescript.chat, "#stop")
            time.sleep(0.2)
            return True
    _ms(minescript.chat, "#stop")
    return False


def collect_nearby_items(radius: float = 3.0, max_iterations: int = 4) -> int:
    """Walk to and pick up all dropped item entities within radius of player.
    Returns count of items collected (entities that disappeared)."""

    def _scan_items(verbose: bool = False) -> list[tuple[float, float, float]]:
        try:
            entities = _ms(minescript.entities, max_distance=float(radius))
        except Exception as e:
            logger.warning(f"collect: minescript.entities() raised {type(e).__name__}: {e}")
            return []

        if verbose:
            type_summary = [str(getattr(e, "type", "?")) for e in entities[:10]]
            logger.info(
                f"collect: scan returned {len(entities)} entities; "
                f"first types: {type_summary}"
            )

        items = []
        for ent in entities:
            type_str = str(getattr(ent, "type", ""))
            # Minescript v5.0 format: "entity.minecraft.item"
            if not type_str.endswith(".item") and type_str != "item":
                continue
            try:
                ex, ey, ez = ent.position
            except Exception:
                continue
            if verbose:
                name_str = str(getattr(ent, "name", ""))
                logger.info(
                    f"collect:   item entity name={name_str!r} type={type_str!r} pos=({ex:.1f},{ey:.1f},{ez:.1f})"
                )
            items.append((ex, ey, ez))
        return items

    # Total time budget — must stay well under the 30s bridge HTTP timeout
    overall_deadline = time.monotonic() + 18.0

    # Initial settle for any in-flight item entities
    time.sleep(0.2)

    collected = 0
    for iteration in range(max_iterations):
        if time.monotonic() >= overall_deadline:
            logger.info("collect: overall time budget exhausted")
            break

        # Verbose logging on first iteration so we can diagnose empty results
        items = _scan_items(verbose=(iteration == 0))
        if not items:
            if iteration == 0:
                logger.info(f"collect: no item entities found within radius={radius}")
            break

        # Pick closest to player
        pos = _ms(minescript.player_position)
        px, py, pz = pos[0], pos[1], pos[2]
        items.sort(key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2 + (p[2] - pz) ** 2)
        target = items[0]
        before_count = len(items)

        logger.info(
            f"collect: targeting item at {target[0]:.1f},{target[1]:.1f},{target[2]:.1f} "
            f"({before_count} item(s) in range)"
        )

        # If already in pickup range, just wait briefly for auto-collect
        if is_within_reach(target[0], target[1], target[2], 1.0):
            time.sleep(0.3)
        else:
            walk_budget = min(3.0, overall_deadline - time.monotonic())
            if walk_budget <= 0:
                break
            _ms(minescript.chat, f"#goto {int(target[0])} {int(target[1])} {int(target[2])}")
            deadline = time.monotonic() + walk_budget
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if is_within_reach(target[0], target[1], target[2], 1.5):
                    break
            _ms(minescript.chat, "#stop")
            time.sleep(0.3)

        # Re-scan: anything we collected reduces the count
        after = _scan_items()
        delta = before_count - len(after)
        if delta <= 0:
            logger.info("collect: no progress, stopping")
            break
        collected += delta

    logger.info(f"collect: picked up {collected} item(s)")
    return collected


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
            block = _ms(minescript.getblock, ax, ay, az)
            if block and "air" not in block:
                return (ax, ay, az, face)
        except Exception:
            continue
    return None
