"""Belief-vs-actual disagreement detector.

Claude decides based on whatever gameState was injected most recently. When
the bridge's live state drifts from that snapshot (e.g. Baritone finished
mining a block that the agent still thinks is there, or an inventory
item was consumed by a craft that the agent hasn't yet observed), the
agent can make decisions on stale information.

This module diffs the most recently injected game state against the
live bridge status on every monitor poll (2s). Significant divergences
are emitted to the session log as `belief_mismatch` events and
broadcast to WebSocket clients so the frontend can surface them in
real time.
"""

from __future__ import annotations

import math
from typing import Any

POSITION_DELTA_BLOCKS = 3.0
HEALTH_DELTA = 4.0


def _flatten_inventory(inv: list[dict] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in inv or []:
        name = entry.get("name") or entry.get("item") or "?"
        count = entry.get("count") or 0
        out[name] = out.get(name, 0) + count
    return out


def _position_delta(a: dict | None, b: dict | None) -> float | None:
    if not a or not b:
        return None
    try:
        ax, ay, az = float(a.get("x", 0)), float(a.get("y", 0)), float(a.get("z", 0))
        bx, by, bz = float(b.get("x", 0)), float(b.get("y", 0)), float(b.get("z", 0))
    except (TypeError, ValueError):
        return None
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)


def diff_belief_vs_actual(
    belief: dict | None,
    actual: dict | None,
    position_threshold: float = POSITION_DELTA_BLOCKS,
    health_threshold: float = HEALTH_DELTA,
) -> list[dict]:
    """Return a list of significant mismatches. Empty list = agreement."""
    if not belief or not actual:
        return []
    mismatches: list[dict] = []

    dpos = _position_delta(belief.get("position"), actual.get("position"))
    if dpos is not None and dpos > position_threshold:
        mismatches.append({
            "field": "position",
            "belief": belief.get("position"),
            "actual": actual.get("position"),
            "delta": round(dpos, 2),
        })

    try:
        bh, ah = belief.get("health"), actual.get("health")
        if bh is not None and ah is not None:
            dh = abs(float(bh) - float(ah))
            if dh > health_threshold:
                mismatches.append({
                    "field": "health",
                    "belief": bh,
                    "actual": ah,
                    "delta": round(dh, 2),
                })
    except (TypeError, ValueError):
        pass

    bi = _flatten_inventory(belief.get("inventory"))
    ai = _flatten_inventory(actual.get("inventory"))
    changes: list[dict] = []
    for item in sorted(set(bi) | set(ai)):
        b_count, a_count = bi.get(item, 0), ai.get(item, 0)
        if b_count != a_count:
            changes.append({"item": item, "belief": b_count, "actual": a_count})
    if changes:
        mismatches.append({"field": "inventory", "changes": changes})

    return mismatches
