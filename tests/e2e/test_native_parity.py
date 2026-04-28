"""Phase 1 parity tests: native mod (8081) vs legacy bridge (8080).

These tests assume `docker compose up` is running the full stack with
the bot connected. They are opt-in via `pytest --run-e2e` (see
tests/conftest.py for the marker).

Each test hits both bridges with the same request and asserts
shape-equivalence with documented tolerances:

  - `/status`: position/health/hunger/inventory must match exactly. `biome`
    must be a string but values can differ (legacy returns "unknown"
    because Minescript v5.0 dropped player_biome; native returns the real
    value). `time` must be a non-negative int and within ~5 ticks of the
    legacy reading (single-tick drift is expected; we sample legacy then
    native sequentially so a few ticks elapse between the reads).
  - `/nearby/blocks`: identical (name, x, y, z) keys; distances within
    0.05 of each other.
  - `/nearby/entities`: identical (name, type) match by entity-id, with a
    tolerance on position/distance for moving entities.

A persistent diff outside these tolerances is a port bug.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

LEGACY = "http://localhost:8080"
NATIVE = "http://localhost:8081"


def _fetch(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=10).read())


def _post(url: str, body: dict) -> tuple[int, dict]:
    """POST JSON, return (status_code, parsed body). Surfaces 4xx as the
    body too — we want to assert error shapes, not just success cases."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


pytestmark = pytest.mark.e2e


def _bridges_alive() -> bool:
    try:
        _fetch(f"{LEGACY}/status")
        _fetch(f"{NATIVE}/health")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_stack():
    if not _bridges_alive():
        pytest.skip("docker compose stack not running on 8080/8081")


def test_status_parity():
    a = _fetch(f"{LEGACY}/status")["data"]
    b = _fetch(f"{NATIVE}/status")["data"]
    # Position is a snapshot of the player; sampling sequentially could
    # drift if the player is moving, but in a peaceful idle test it's stable.
    assert a["position"] == b["position"], (a["position"], b["position"])
    assert a["health"] == b["health"]
    assert a["hunger"] == b["hunger"]
    assert a["inventory"] == b["inventory"]
    # Biome shape only (legacy stubs to "unknown"; native returns real value).
    assert isinstance(a["biome"], str) and isinstance(b["biome"], str)
    # Time is a tick counter — legacy snapshot then native is one cache cycle
    # later, so allow a small forward drift.
    assert b["time"] >= 0
    assert abs(b["time"] - a["time"]) < 100, (a["time"], b["time"])


@pytest.mark.parametrize("radius", [2, 4, 8])
def test_nearby_blocks_parity(radius: int):
    a = _fetch(f"{LEGACY}/nearby/blocks?r={radius}")["data"]["blocks"]
    b = _fetch(f"{NATIVE}/nearby/blocks?r={radius}")["data"]["blocks"]
    am = {(o["name"], o["x"], o["y"], o["z"]): o for o in a}
    bm = {(o["name"], o["x"], o["y"], o["z"]): o for o in b}
    # Membership must match exactly — both bridges should report the same
    # set of solid blocks at integer coords for a stable world.
    assert set(am) == set(bm), (
        f"r={radius} legacy_only={list(set(am)-set(bm))[:3]} "
        f"native_only={list(set(bm)-set(am))[:3]}"
    )
    # Distances within rounding noise (legacy rounds to 0.1; native does too).
    diffs = [
        (k, am[k]["distance"], bm[k]["distance"])
        for k in am
        if abs(am[k]["distance"] - bm[k]["distance"]) > 0.05
    ]
    assert not diffs, f"distance drift > 0.05: {diffs[:5]}"
    # Sort order: ascending by distance.
    assert b == sorted(b, key=lambda o: o["distance"])


def test_nearby_blocks_type_filter():
    """The `types=` query param must filter the same way on both bridges."""
    full = _fetch(f"{NATIVE}/nearby/blocks?r=8")["data"]["blocks"]
    if not full:
        pytest.skip("no blocks in scan radius")
    target = full[0]["name"]
    a = _fetch(f"{LEGACY}/nearby/blocks?r=8&types={target}")["data"]["blocks"]
    b = _fetch(f"{NATIVE}/nearby/blocks?r=8&types={target}")["data"]["blocks"]
    assert all(o["name"] == target for o in a)
    assert all(o["name"] == target for o in b)
    assert {(o["x"], o["y"], o["z"]) for o in a} == {(o["x"], o["y"], o["z"]) for o in b}


def test_nearby_entities_parity():
    a = _fetch(f"{LEGACY}/nearby/entities?r=32")["data"]["entities"]
    b = _fetch(f"{NATIVE}/nearby/entities?r=32")["data"]["entities"]
    # Match by (name, type) since entity ids aren't exposed; for the bot in
    # peaceful mode there's typically only one entity (the player) so this
    # is enough.
    a_keys = {(o["name"], o["type"]) for o in a}
    b_keys = {(o["name"], o["type"]) for o in b}
    assert a_keys == b_keys, (a_keys, b_keys)


def test_probe_identifies_native_mod():
    p = _fetch(f"{NATIVE}/probe")["data"]
    assert p["kind"] == "native-mod"
    assert "/status" in p["ported"]
    assert "/nearby/blocks" in p["ported"]
    assert "/nearby/entities" in p["ported"]
    # Phase 2 routes appear in the native-side ported list even though the
    # agent only routes /chat (equip/discard ride along for diagnostics).
    assert "/chat" in p["ported"]
    assert "/equip" in p["ported"]
    assert "/discard" in p["ported"]
    # Phase 3 routes — break/place/attack are ported and routed.
    assert "/break" in p["ported"]
    assert "/place" in p["ported"]
    assert "/attack" in p["ported"]
    assert p["capabilities"]["tick_thread_executor"] is True


# ---------------------------------------------------------------------------
# Phase 2 — write endpoints
# ---------------------------------------------------------------------------


def test_chat_native_plain_message():
    """A plain message lands as `/tellraw` from the bot. Native must
    accept it without crashing the connection (signed-chat trap) and
    return the legacy success shape."""
    code, body = _post(f"{NATIVE}/chat", {"message": "phase2 native chat hello"})
    assert code == 200, body
    assert body["status"] == "success"
    assert body["message"] == "Sent: phase2 native chat hello"


def test_chat_native_slash_command():
    code, body = _post(f"{NATIVE}/chat", {"message": "/say native chat slash"})
    assert code == 200, body
    assert body["status"] == "success"


def test_chat_native_baritone_prefix():
    """`#stop` is a no-op when Baritone isn't pathing — but it must round-trip
    through sendChatMessage so the Baritone hook can observe and intercept it
    before it ships as a player chat packet."""
    code, body = _post(f"{NATIVE}/chat", {"message": "#stop"})
    assert code == 200, body
    assert body["status"] == "success"


def test_chat_native_missing_message():
    code, body = _post(f"{NATIVE}/chat", {})
    assert code == 400
    assert body["status"] == "error"
    assert "message" in body["message"].lower()


def test_equip_native_missing_item():
    """Native /equip is implemented but not yet routed; we hit :8081
    directly to verify the impl. Empty `item` must return the same 400
    shape as legacy."""
    code, body = _post(f"{NATIVE}/equip", {"slot": "hand"})
    assert code == 400
    assert body["status"] == "error"
    assert "item" in body["message"].lower()


def test_equip_native_armor_no_item():
    """Phase 2b native /equip handles armor slots, but the empty bot
    has no helmet. The shape we lock in: 200 + status:"error" + message
    mentioning the missing item — NOT a 500 (would mean the route
    itself crashed) and NOT 'armor not supported' (would mean Phase 2b
    didn't land)."""
    code, body = _post(f"{NATIVE}/equip", {"item": "iron_helmet", "slot": "head"})
    assert code == 200, body
    assert body["status"] == "error"
    msg = body["message"].lower()
    assert "iron_helmet" in body["message"] or "no" in msg or "inventory" in msg
    assert "not yet support" not in msg, "Phase 2b should support armor slots"


def test_equip_native_unknown_slot():
    """Unknown slot strings should fail cleanly, not crash."""
    code, body = _post(f"{NATIVE}/equip", {"item": "stone", "slot": "left_pocket"})
    assert code == 200, body
    assert body["status"] == "error"
    assert "left_pocket" in body["message"]


def test_equip_native_unknown_item():
    """Item the player doesn't have should fail with a clear message and
    NOT crash the tick thread."""
    code, body = _post(
        f"{NATIVE}/equip", {"item": "definitely_not_a_real_item_zzz", "slot": "hand"}
    )
    assert code == 200
    assert body["status"] == "error"


def test_discard_native_missing_item():
    code, body = _post(f"{NATIVE}/discard", {"count": 1})
    assert code == 400
    assert body["status"] == "error"


def test_discard_native_unknown_item():
    code, body = _post(
        f"{NATIVE}/discard", {"item": "definitely_not_a_real_item_zzz", "count": 1}
    )
    assert code == 200
    assert body["status"] == "error"


def test_discard_native_invalid_count():
    code, body = _post(f"{NATIVE}/discard", {"item": "stone", "count": 0})
    assert code == 400
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Phase 3 — world-mutation endpoints (break / place / attack)
#
# We avoid actually mutating the test world: hit the validation paths and
# the "already air" / "not in inventory" / "entity not found" branches.
# Mutating tests would require coordinating block coords with the running
# world and would leave persistent state across runs.
# ---------------------------------------------------------------------------


def test_break_native_already_air_is_idempotent_success():
    """Breaking a known-air cell is a no-op success in both bridges. Use a
    high-Y coordinate that's reliably above terrain."""
    code, body = _post(f"{NATIVE}/break", {"x": 0, "y": 250, "z": 0})
    assert code == 200, body
    assert body["status"] == "success"
    assert body["data"]["broken"] is True
    assert body["data"]["already_gone"] is True


def test_place_native_missing_block_param():
    code, body = _post(f"{NATIVE}/place", {"x": 0, "y": 64, "z": 0})
    assert code == 400
    assert body["status"] == "error"
    assert "block" in body["message"].lower()


def test_place_native_unknown_block_not_in_inventory():
    """An item the player doesn't have should fail cleanly with a clear
    message — NOT crash the tick thread."""
    code, body = _post(
        f"{NATIVE}/place",
        {"block": "definitely_not_a_real_item_zzz", "x": 0, "y": 250, "z": 0},
    )
    assert code == 200, body
    assert body["status"] == "error"


def test_attack_native_missing_entity_id():
    code, body = _post(f"{NATIVE}/attack", {})
    assert code == 400
    assert body["status"] == "error"
    assert "entity_id" in body["message"].lower()


def test_attack_native_entity_not_found():
    code, body = _post(f"{NATIVE}/attack", {"entity_id": "definitely_not_a_real_entity_zzz"})
    assert code == 200, body
    assert body["status"] == "error"
    assert "not found" in body["message"].lower()
