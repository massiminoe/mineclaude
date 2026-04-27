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


def _is_main_hand(item_name: str) -> bool:
    """Return True if the item at Inventory.selected matches item_name.

    Uses `player_inventory()`'s `selected` flag rather than
    `player_hand_items()` — the latter reads a stale `LivingEntity.handItems`
    cache that doesn't update when we set `Inventory.selected` directly via
    `player_inventory_select_slot`. The inventory's `selected` field is the
    same source MC's server-sync uses, so it's the correct ground truth.
    """
    try:
        inv = _ms(minescript.player_inventory)
    except (AttributeError, TypeError):
        return False
    target = item_name.replace("minecraft:", "")
    for it in inv:
        if it is None:
            continue
        if getattr(it, "selected", False):
            name = (getattr(it, "item", "") or "").replace("minecraft:", "")
            return name == target
    return False


def _currently_held_hotbar_slot() -> int | None:
    """Return the hotbar slot whose ItemStack.selected flag is True, or None.

    Source of truth for what MC thinks is held, independent of any packet
    we may or may not have sent. Falls back to None if player_inventory
    isn't available.
    """
    try:
        inv = _ms(minescript.player_inventory)
    except (AttributeError, TypeError):
        return None
    for item in inv:
        if item is None:
            continue
        if getattr(item, "selected", False):
            s = getattr(item, "slot", None)
            if s is not None and 0 <= s <= 8:
                return s
    return None


def _swap_hotbar_slots(src: int, dst: int) -> bool:
    """Lossless swap of two hotbar slots via /item replace.

    Used as a fallback when player_inventory_select_slot no-ops — if we
    can't change which slot is held, we move the item we want INTO the
    currently-held slot instead.
    """
    if src == dst:
        return True
    src_name = _slot_to_item_name(src)
    dst_name = _slot_to_item_name(dst)
    temp = _find_empty_inventory_slot()
    if temp is None:
        logger.warning("_swap_hotbar_slots: inventory full, cannot swap")
        return False
    temp_name = _slot_to_item_name(temp)
    try:
        # Stash dst into temp, copy src over dst, restore temp into src, clear temp
        _ms(minescript.execute, f"/item replace entity @s {temp_name} from entity @s {dst_name}")
        _ms(minescript.execute, f"/item replace entity @s {dst_name} from entity @s {src_name}")
        _ms(minescript.execute, f"/item replace entity @s {src_name} from entity @s {temp_name}")
        _ms(minescript.execute, f"/item replace entity @s {temp_name} with minecraft:air")
        time.sleep(0.05)
        return True
    except Exception as e:
        logger.warning(f"_swap_hotbar_slots: {e}")
        return False


def select_item_in_hotbar(item_name: str) -> bool:
    """Find item, move to hotbar if needed, select it. Returns True on success.

    Verifies via player_hand_items() that the selection actually took effect.
    `player_inventory_select_slot` is unreliable on MC 1.21.5 (same family
    of broken APIs as `player_inventory_slot_to_hotbar`). On verification
    failure, falls back to `/item replace` to swap the target item into
    the currently-held slot — bypasses the broken packet path entirely.
    """
    # Check hotbar first
    hotbar_slot = find_item_in_hotbar(item_name)
    if hotbar_slot is None:
        slot = find_item_slot(item_name)
        if slot is None:
            return False
        if not move_item_to_hotbar(slot, 0):
            return False
        hotbar_slot = 0

    # Primary path: native select_slot API
    for attempt in range(2):
        _ms(minescript.player_inventory_select_slot, hotbar_slot)
        time.sleep(0.1)
        if _is_main_hand(item_name):
            return True
        logger.warning(
            f"select_slot: hotbar {hotbar_slot} select did not take effect "
            f"(attempt {attempt + 1})"
        )

    # Fallback: swap the item INTO whatever slot is currently held
    held = _currently_held_hotbar_slot()
    if held is None:
        logger.error(f"select_slot: can't determine held slot, giving up on {item_name}")
        return False
    if held == hotbar_slot:
        logger.error(
            f"select_slot: {item_name} is in held slot {held} but hand doesn't show it "
            f"(client/server desync?)"
        )
        return False
    logger.info(
        f"select_slot: falling back to /item replace swap "
        f"(move {item_name} from hotbar {hotbar_slot} into held slot {held})"
    )
    if not _swap_hotbar_slots(hotbar_slot, held):
        return False
    if _is_main_hand(item_name):
        return True
    logger.error(f"select_slot: swap fallback did not result in {item_name} held")
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

    # Wait for arrival with timeout. 15s cap — if Baritone can't reach the
    # target in that window it's almost always because of an obstruction
    # (leaves blocking tree-top logs, walled-off ore, etc.); waiting longer
    # just burns the Claude iteration budget on a guaranteed-failure path.
    deadline = time.monotonic() + 15.0
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

    def _player_pos() -> tuple[float, float, float]:
        try:
            pos = _ms(minescript.player_position)
            if pos is None:
                return 0.0, 0.0, 0.0
            return pos[0], pos[1], pos[2]
        except Exception:
            return 0.0, 0.0, 0.0

    def _scan_items(scan_radius: float, verbose: bool = False) -> list[tuple[float, float, float, str, float]]:
        """Return list of (x, y, z, name, dist_from_player) for item entities."""
        px, py, pz = _player_pos()
        try:
            entities = _ms(minescript.entities, max_distance=float(scan_radius))
        except Exception as e:
            logger.warning(f"collect: minescript.entities() raised {type(e).__name__}: {e}")
            return []
        # The fork has been observed returning None instead of raising
        # (suspected long RPC that resolved without a value). Treat as empty —
        # the next iteration's deadline check will bail out cleanly.
        if entities is None:
            logger.warning("collect: minescript.entities() returned None — treating as empty")
            return []

        if verbose:
            logger.info(
                f"collect: scan radius={scan_radius:.1f} player=({px:.1f},{py:.1f},{pz:.1f}) "
                f"returned {len(entities)} entities"
            )

        items = []
        for ent in entities:
            type_str = str(getattr(ent, "type", ""))
            if not type_str.endswith(".item") and type_str != "item":
                continue
            try:
                ex, ey, ez = ent.position
            except Exception:
                continue
            dist = math.sqrt((ex - px) ** 2 + (ey - py) ** 2 + (ez - pz) ** 2)
            name_str = str(getattr(ent, "name", ""))
            if verbose:
                logger.info(
                    f"collect:   item name={name_str!r} pos=({ex:.1f},{ey:.1f},{ez:.1f}) dist={dist:.2f}"
                )
            items.append((ex, ey, ez, name_str, dist))
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
        items = _scan_items(radius, verbose=(iteration == 0))
        if not items:
            if iteration == 0:
                px, py, pz = _player_pos()
                logger.info(
                    f"collect: no item entities within radius={radius} "
                    f"(player at {px:.1f},{py:.1f},{pz:.1f})"
                )
                # Diagnostic wider scan — tells us whether drops exist just
                # out of reach (common after Baritone pathed away from a drop
                # mid-break) vs genuinely no drops spawned.
                wide_radius = radius * 4
                wide = _scan_items(wide_radius, verbose=False)
                if wide:
                    preview = [
                        f"{name}@({x:.1f},{y:.1f},{z:.1f}) d={d:.1f}"
                        for x, y, z, name, d in sorted(wide, key=lambda it: it[4])[:5]
                    ]
                    logger.info(
                        f"collect: wider scan r={wide_radius} found {len(wide)} item(s) "
                        f"outside collect radius — top 5: {preview}"
                    )
                else:
                    logger.info(
                        f"collect: wider scan r={wide_radius} also empty — "
                        f"no drops spawned or they despawned"
                    )
            break

        # Already sorted internally by dist via _scan_items? No — _scan_items
        # returns in entity scan order. Sort by distance field for closest-first.
        items.sort(key=lambda it: it[4])
        target = items[0]
        before_count = len(items)

        logger.info(
            f"collect: targeting {target[3]!r} at ({target[0]:.1f},{target[1]:.1f},{target[2]:.1f}) "
            f"dist={target[4]:.2f} ({before_count} item(s) in range)"
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

        # Re-scan at the same radius; anything we collected reduces the count
        after = _scan_items(radius)
        delta = before_count - len(after)
        if delta <= 0:
            logger.info(f"collect: no progress (before={before_count}, after={len(after)}), stopping")
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
