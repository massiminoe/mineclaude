"""Build the structured GameState snapshot returned by Runtime.get_state().

Shapes a bridge `/status` payload + queue snapshot + recent-reflex buffer into
the typed `models.GameState` the MCP `get_state` surface returns.

Pure shaping — no I/O, no event draining. The Runtime owns the event buffer and
hands the already-assembled `events` list in; here we only fold the bridge
status, the queue snapshot, and the recent-reflex buffer into the dataclass.
"""

from __future__ import annotations

import time
from typing import Any

from mineclaude.models import GameState

# Armor + hand slots, in the order get_state surfaces them. A missing slot
# reads as None so the shape is stable whether or not the bridge reports it.
_EQUIP_SLOTS = ("hand", "head", "chest", "legs", "feet")


def build_game_state(
    status: dict[str, Any],
    queue_status: dict[str, Any],
    *,
    recent_reflexes: list[dict] | None = None,
    events: list[dict] | None = None,
    events_truncated: bool = False,
    now: float | None = None,
) -> GameState:
    """Fold a bridge `/status` payload + queue snapshot + reflex buffer into a
    structured GameState. `events` is the already-drained flushable buffer the
    Runtime assembles (Runtime owns its lifecycle); we just carry it through."""
    if now is None:
        now = time.time()
    pos = status.get("position") or {}
    xp = status.get("experience") or {}
    player = {
        "pos": [pos.get("x"), pos.get("y"), pos.get("z")],
        "health": status.get("health"),
        "hunger": status.get("hunger"),
        "biome": status.get("biome"),
        "time": status.get("time"),
        # Selected hotbar index (0..8) — the "what am I actually holding" read
        # that pairs with equipped.hand for diagnosing wrong-tool mining.
        "held_slot": status.get("held_slot"),
        # Experience level — the spendable currency for anvil + enchanting.
        # None when the bridge doesn't report it (pre-world / old mock).
        "xp_level": xp.get("level"),
        # Fire state — surfaced so the agent reads "I'm burning" directly
        # instead of inferring it from the damage log. `on_fire` is the live
        # flag; `fire_ticks` counts the remaining burn down (~20 ticks = 1
        # damage), so small = about to go out, large = keep burning. The
        # started_burning reflex acts on the rising edge; this is the poll view.
        "on_fire": status.get("on_fire"),
        "fire_ticks": status.get("fire_ticks"),
    }
    inventory = list(status.get("inventory") or [])
    return GameState(
        player=player,
        inventory=inventory,
        inventory_slots=_inventory_slots(inventory),
        equipped=_equipped_view(status.get("equipped")),
        action=_action_view(queue_status or {}, now),
        reflexes_recent=_reflexes_view(recent_reflexes or [], now),
        events=list(events or []),
        events_truncated=events_truncated,
    )


# The 36 main-inventory slots (0..8 hotbar + 9..35 main). Armor (36..39) and
# offhand (40) sit past this and don't count toward "room for a new stack".
_STORAGE_SLOTS = 36


def _inventory_slots(inventory: list[dict[str, Any]]) -> str:
    """Occupied storage slots as "M/36". Counts inventory stacks whose slot is
    in the main 36; an entry missing a slot (mock paths) counts as occupied so
    fullness never reads emptier than it is."""
    used = 0
    for entry in inventory:
        slot = entry.get("slot")
        if slot is None or slot < _STORAGE_SLOTS:
            used += 1
    return f"{used}/{_STORAGE_SLOTS}"


def _equipped_view(equipped: Any) -> dict[str, Any]:
    """Normalize the equipped block to the fixed 5-slot shape, Nones for gaps."""
    view: dict[str, Any] = {slot: None for slot in _EQUIP_SLOTS}
    if isinstance(equipped, dict):
        for slot in _EQUIP_SLOTS:
            if equipped.get(slot):
                view[slot] = equipped[slot]
    return view


def _action_view(queue_status: dict[str, Any], now: float) -> dict[str, Any]:
    """The single-flight slot's state: `running` while an action is in flight,
    else the terminal status of the most recent action, else `idle`."""
    running = queue_status.get("running")
    if running:
        started = running.get("started_at")
        running_for = round(now - started, 1) if started else None
        return {"state": "running", "id": running.get("id"), "running_for_s": running_for}
    recent = queue_status.get("recent") or []
    if recent:
        last = recent[-1]
        return {
            "state": last.get("status"),
            "id": last.get("id"),
            "running_for_s": None,
            "result": last.get("result"),
            "error": last.get("error"),
        }
    return {"state": "idle", "id": None, "running_for_s": None}


def _reflexes_view(recent: list[dict], now: float) -> list[dict[str, Any]]:
    """Render the rolling reflex buffer as age-stamped entries. NOT flushed —
    this mirrors registry.recent, which ages out by capacity, not by read."""
    out: list[dict[str, Any]] = []
    for entry in recent:
        ts = entry.get("ts", now)
        out.append({
            "type": entry.get("type"),
            "age_s": round(max(0.0, now - ts), 1),
            "data": entry.get("data") or {},
        })
    return out
