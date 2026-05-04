"""Tests for the reflex layer infrastructure + default handlers.

Covers:
  * ReflexRegistry dispatch (cooldown, recent buffer, slog/emit, preempt)
  * The action_queue pre-interrupt hook
  * The gameState rendering helper
  * The three custom handlers (damage_taken branching, lava + drowning escape)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import pytest

from agent.action_queue import ActionQueue
from agent.bridge import BridgeResponse
from agent.prompt import format_game_state
from agent.reflexes import (
    REFLEX_EVENT_TYPES,
    ReflexHandler,
    ReflexRegistry,
    damage_taken_handler,
    entered_lava_handler,
    format_recent,
    register_default_handlers,
    started_drowning_handler,
    stub_handler,
)


class _FakeBridge:
    """Records the bridge calls a reflex handler makes."""

    def __init__(self, status: dict | None = None, blocks: list[dict] | None = None):
        self._status = status or {
            "position": {"x": 10.0, "y": 64.0, "z": 5.0},
            "health": 18.0,
        }
        self._blocks = blocks or []
        self.goto_calls: list[tuple[float, float, float]] = []
        self.attack_calls: list[str] = []
        self.stop_calls = 0
        self.nearby_blocks_radii: list[int] = []

    async def get_status(self) -> BridgeResponse:
        return BridgeResponse("success", "ok", dict(self._status))

    async def get_nearby_blocks(self, radius: int = 8, block_types=None) -> BridgeResponse:
        self.nearby_blocks_radii.append(radius)
        # Mirror the real endpoint: sorted by distance ascending.
        sorted_blocks = sorted(self._blocks, key=lambda b: b.get("distance", 0.0))
        return BridgeResponse("success", "ok", {"blocks": sorted_blocks})

    async def goto(self, x, y, z):
        self.goto_calls.append((x, y, z))
        return BridgeResponse("success", "ok", {})

    async def attack(self, entity_id):
        self.attack_calls.append(entity_id)
        return BridgeResponse("success", "ok", {})

    async def stop(self):
        self.stop_calls += 1
        return BridgeResponse("success", "ok", {})


def _block(name: str, x: int, y: int, z: int, distance: float = 1.0) -> dict:
    return {"name": name, "x": x, "y": y, "z": z, "distance": distance}


class FakeAgent:
    """Minimal Agent stand-in. The registry only touches `_slog`,
    `_emit`, `_preempt`, and (via handlers) `bridge`."""

    def __init__(self, bridge: _FakeBridge | None = None):
        self.slog_calls: list[tuple[str, dict]] = []
        self.emit_calls: list[tuple[str, dict]] = []
        self.preempt_calls = 0
        self.queue = _FakeQueue()
        self.bridge = bridge or _FakeBridge()

    def _slog(self, event: str, **data) -> None:
        self.slog_calls.append((event, data))

    async def _emit(self, event: str, data) -> None:
        self.emit_calls.append((event, data))

    async def _preempt(self) -> None:
        self.preempt_calls += 1


class _FakeQueue:
    def __init__(self):
        self.interrupt_calls = 0

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


# --- ReflexRegistry --------------------------------------------------------


async def test_dispatch_unknown_type_is_noop():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    await reg.dispatch("never_registered", {})
    assert agent.slog_calls == []
    assert agent.emit_calls == []
    assert reg.recent == deque()


async def test_dispatch_records_to_recent_buffer_and_slog():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="damage_taken", handle=stub_handler))

    await reg.dispatch("damage_taken", {"amount": 3.0})
    assert len(reg.recent) == 1
    entry = reg.recent[0]
    assert entry["type"] == "damage_taken"
    assert entry["data"] == {"amount": 3.0}
    assert "ts" in entry

    assert agent.slog_calls == [("reflex_fired", {"type": "damage_taken", "data": {"amount": 3.0}})]
    assert len(agent.emit_calls) == 1
    assert agent.emit_calls[0][0] == "reflex:fired"


async def test_dispatch_cooldown_gates_refire():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="entered_lava", handle=stub_handler, cooldown_s=0.5))

    await reg.dispatch("entered_lava", {})
    await reg.dispatch("entered_lava", {})  # within cooldown — gated
    assert len(reg.recent) == 1
    assert len(agent.slog_calls) == 1

    await asyncio.sleep(0.55)
    await reg.dispatch("entered_lava", {})
    assert len(reg.recent) == 2


async def test_dispatch_preempts_invokes_agent_preempt():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="entered_lava", handle=stub_handler, preempts=True))

    await reg.dispatch("entered_lava", {})
    assert agent.preempt_calls == 1


async def test_dispatch_no_preempt_does_not_call_preempt():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="tool_broke", handle=stub_handler, preempts=False))

    await reg.dispatch("tool_broke", {"item": "iron_pickaxe"})
    assert agent.preempt_calls == 0


async def test_dispatch_handler_exception_does_not_propagate():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)

    async def bad_handler(_a, _d):
        raise RuntimeError("boom")

    reg.register(ReflexHandler(event_type="damage_taken", handle=bad_handler))
    await reg.dispatch("damage_taken", {})
    assert len(reg.recent) == 1


async def test_recent_buffer_capped():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="entered_lava", handle=stub_handler))

    for _ in range(15):
        await reg.dispatch("entered_lava", {})
    assert len(reg.recent) == reg.recent.maxlen


async def test_last_fire_ts_advances():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="started_drowning", handle=stub_handler))
    assert reg.last_fire_ts == 0.0
    before = time.monotonic()
    await reg.dispatch("started_drowning", {})
    assert reg.last_fire_ts >= before


async def test_register_default_handlers_covers_all_event_types():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    register_default_handlers(reg)
    assert reg.known_types() == set(REFLEX_EVENT_TYPES)


async def test_register_default_handlers_preempt_flags():
    """The preempt policy is a deliberate per-event choice — pin it."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    register_default_handlers(reg)
    by_type = {t: reg._handlers[t] for t in REFLEX_EVENT_TYPES}
    assert by_type["damage_taken"].preempts is False  # handler decides
    assert by_type["entered_lava"].preempts is True
    assert by_type["started_drowning"].preempts is True
    assert by_type["tool_broke"].preempts is True
    assert by_type["exited_lava"].preempts is False  # record-only
    assert by_type["stopped_drowning"].preempts is False


# --- damage_taken handler --------------------------------------------------


async def test_damage_taken_no_attacker_is_record_only():
    """Fall damage / fire / suffocation: no attacker_kind → no preempt, no
    bridge call. The fact of damage is still in the recent buffer (recorded
    by dispatch before invoking the handler)."""
    agent = FakeAgent()
    await damage_taken_handler(agent, {"source": "fall", "amount": 4.0, "hp_before": 18.0})
    assert agent.preempt_calls == 0
    assert agent.bridge.goto_calls == []
    assert agent.bridge.attack_calls == []


async def test_damage_taken_high_hp_attacks_back():
    """Above LOW_HP threshold, retaliate against the attacker by id."""
    agent = FakeAgent(_FakeBridge({"position": {"x": 10.0, "y": 64.0, "z": 5.0}}))
    await damage_taken_handler(agent, {
        "source": "mob_attack",
        "attacker_kind": "zombie",
        "attacker_id": 42,
        "attacker_pos": {"x": 12.0, "y": 64.0, "z": 5.0},
        "amount": 2.0,
        "hp_before": 18.0,  # → 16 hp_after, well above 6
    })
    assert agent.preempt_calls == 1
    assert agent.bridge.attack_calls == ["42"]
    assert agent.bridge.goto_calls == []


async def test_damage_taken_low_hp_flees_opposite_attacker():
    """Below LOW_HP: flee 10 blocks in the opposite direction."""
    agent = FakeAgent(_FakeBridge({"position": {"x": 10.0, "y": 64.0, "z": 5.0}}))
    await damage_taken_handler(agent, {
        "source": "mob_attack",
        "attacker_kind": "zombie",
        "attacker_id": 42,
        "attacker_pos": {"x": 8.0, "y": 64.0, "z": 5.0},  # 2 west of player
        "amount": 5.0,
        "hp_before": 8.0,  # → 3 hp_after, below 6
    })
    assert agent.preempt_calls == 1
    assert agent.bridge.attack_calls == []
    assert len(agent.bridge.goto_calls) == 1
    fx, fy, fz = agent.bridge.goto_calls[0]
    # Player at x=10, attacker at x=8, so flee direction is +x.
    # 10 blocks east = x ~= 20, y unchanged, z unchanged.
    assert fy == 64.0
    assert fz == pytest.approx(5.0)
    assert fx == pytest.approx(20.0)


async def test_damage_taken_low_hp_no_attacker_pos_no_flee():
    """If we can't compute a flee direction, preempt happened but no goto."""
    agent = FakeAgent()
    await damage_taken_handler(agent, {
        "attacker_kind": "skeleton",
        "attacker_id": 1,
        # no attacker_pos
        "amount": 5.0,
        "hp_before": 8.0,
    })
    assert agent.preempt_calls == 1
    assert agent.bridge.goto_calls == []


async def test_damage_taken_high_hp_no_attacker_id_no_attack():
    """Edge: attacker_kind present but id missing — preempt but no attack call."""
    agent = FakeAgent()
    await damage_taken_handler(agent, {
        "attacker_kind": "spider",
        "amount": 2.0,
        "hp_before": 18.0,
    })
    assert agent.preempt_calls == 1
    assert agent.bridge.attack_calls == []


# --- lava + drowning escape handlers ---------------------------------------


async def test_started_drowning_walks_to_nearest_shore():
    """Pick the nearest non-hazard block whose +1 and +2 are air."""
    blocks = [
        # Player at (0, 62, 0). Pool of water around the player; shore east.
        _block("water", 0, 62, 0, 0.5),
        _block("water", 0, 61, 0, 1.5),
        _block("dirt", 0, 60, 0, 2.5),  # bottom of pool — water above, not shore
        _block("dirt", 2, 61, 0, 2.5),  # shore: y=61, y+1/+2 are air
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    # Goto target is y+1 (top of the block — where the player stands).
    assert bridge.goto_calls == [(2.0, 62.0, 0.0)]


async def test_entered_lava_handler_uses_shore_finder():
    blocks = [
        _block("lava", 0, 50, 0, 0.5),
        _block("dirt", 2, 49, 0, 2.5),  # shore
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 50.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await entered_lava_handler(agent, {})
    assert bridge.goto_calls == [(2.0, 50.0, 0.0)]


async def test_shore_finder_no_candidate_leaves_bot_alone():
    """Sealed prison: only water in range, no land tile to walk to. We accept
    the edge case and don't move the bot — Claude is on its own."""
    blocks = [
        _block("water", 0, 62, 0, 0.5),
        _block("water", 1, 62, 0, 1.0),
        _block("water", -1, 62, 0, 1.0),
        _block("water", 0, 62, 1, 1.0),
        _block("water", 0, 62, -1, 1.0),
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.goto_calls == []


async def test_shore_finder_rejects_walled_in_pillar():
    """A stone pillar whose top has another stone on it is not standable —
    the +1 air check filters it. Verifies the truly-sealed prison case where
    the only nearby non-hazard blocks are walls extending up indefinitely."""
    blocks = [
        _block("water", 0, 62, 0, 0.5),
        # Wall pillar on each side, two blocks tall — top of wall blocks the
        # +1 air for the bottom block, and the top block has nothing above so
        # it qualifies as shore from a strict "+1/+2 air" reading. That's
        # actually correct: in real MC such a 2-tall wall has air above and
        # *can* be stood on, so the shore finder picks it. We verify that —
        # if the user actually wants a fully sealed pool, they need a ceiling.
        _block("stone", 1, 62, 0, 1.0),
        _block("stone", 1, 63, 0, 1.0),  # blocks bottom-pillar's +1 air
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    # Top of the 2-tall wall qualifies (its +1/+2 are air).
    assert bridge.goto_calls == [(1.0, 64.0, 0.0)]


async def test_shore_finder_skips_blocks_with_block_above():
    """Tile with non-air at y+1 is unstandable — the buried dirt under a
    stone column never qualifies. The column's top extends past Y_DELTA so
    it's also filtered, leaving the farther flat dirt as the chosen shore."""
    blocks = [
        _block("dirt", 2, 61, 0, 2.5),
        # Tall stone column above the dirt — top sits past SHORE_MAX_Y_DELTA.
        *[_block("stone", 2, y, 0, 2.0 + 0.1 * (y - 62)) for y in range(62, 68)],
        _block("dirt", 4, 61, 0, 4.5),
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.goto_calls == [(4.0, 62.0, 0.0)]


async def test_shore_finder_excludes_hazard_block_as_standing_tile():
    """Even with two air blocks above, water/lava is not a shore."""
    blocks = [
        _block("water", 1, 61, 0, 1.5),  # air above but is hazard
        _block("dirt", 3, 61, 0, 3.5),
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.goto_calls == [(3.0, 62.0, 0.0)]


async def test_shore_finder_skips_far_y_candidates():
    """Don't path 30 blocks down to a far-below shore."""
    blocks = [
        _block("dirt", 1, 30, 0, 32.0),  # 32 blocks below player Y
        _block("dirt", 5, 60, 0, 5.4),   # within Y delta — pick this
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.goto_calls == [(5.0, 61.0, 0.0)]


async def test_shore_finder_respects_max_distance():
    """A candidate beyond SHORE_MAX_DISTANCE is rejected even if otherwise valid.
    The +1/+2 air check is only reliable for candidates strictly inside the
    queried cube."""
    blocks = [
        _block("dirt", 0, 61, 0, 13.0),  # too far — could have a non-air +1/+2 outside the scan
    ]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.goto_calls == []


async def test_escape_handler_swallows_status_failure():
    """Bridge errors mustn't kill the WS consumer — Claude reconciles next iter."""
    class _BoomBridge(_FakeBridge):
        async def get_status(self):
            raise RuntimeError("network down")
    agent = FakeAgent(_BoomBridge())
    # Should not raise.
    await entered_lava_handler(agent, {})
    await started_drowning_handler(agent, {})


async def test_escape_handler_swallows_nearby_blocks_failure():
    class _BoomBridge(_FakeBridge):
        async def get_nearby_blocks(self, radius=8, block_types=None):
            raise RuntimeError("network down")
    agent = FakeAgent(_BoomBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}))
    await started_drowning_handler(agent, {})


# --- format_recent / format_game_state -------------------------------------


def test_format_recent_empty_returns_empty_string():
    assert format_recent([]) == ""


def test_format_recent_renders_recent_first_with_data_hints():
    now = 1000.0
    recent = [
        {"type": "damage_taken", "data": {"amount": 3.0, "attacker_kind": "zombie"}, "ts": now - 5},
        {"type": "entered_lava", "data": {}, "ts": now - 2},
    ]
    out = format_recent(recent, now=now)
    assert out.startswith("=== Recent reflex events ===")
    lines = out.splitlines()[1:]
    assert "entered_lava" in lines[0]
    assert "2s ago" in lines[0]
    assert "damage_taken" in lines[1]
    assert "3.0 dmg" in lines[1]
    assert "from zombie" in lines[1]


def test_format_recent_caps_to_three():
    now = 1000.0
    recent = [
        {"type": f"event_{i}", "data": {}, "ts": now - i} for i in range(10)
    ]
    out = format_recent(recent, now=now)
    assert len(out.splitlines()) == 1 + 3


def test_format_game_state_appends_reflex_section():
    status = {
        "position": {"x": 1.0, "y": 64.0, "z": 2.0},
        "health": 18.0,
        "hunger": 16,
        "biome": "plains",
        "time": 1000,
        "inventory": [],
    }
    queue_status = {"running": None, "pending": [], "recent": []}
    reflexes = [
        {"type": "tool_broke", "data": {"item": "iron_pickaxe"}, "ts": time.time() - 1},
    ]
    out = format_game_state(status, queue_status, recent_reflexes=reflexes)
    assert "Recent reflex events" in out
    assert "tool_broke" in out
    assert "iron_pickaxe" in out


def test_format_game_state_omits_reflex_section_when_empty():
    status = {
        "position": {"x": 0, "y": 64, "z": 0},
        "health": 20.0,
        "hunger": 20,
        "biome": "plains",
        "time": 0,
        "inventory": [],
    }
    out = format_game_state(status, {"running": None, "pending": [], "recent": []})
    assert "Recent reflex events" not in out


# --- action_queue pre-interrupt hook ---------------------------------------


async def _noop_executor(code: str) -> str:
    await asyncio.sleep(0)
    return "ok"


async def test_pre_interrupt_runs_before_clear_and_cancel():
    q = ActionQueue(timeout=5.0)
    q.set_executor(_noop_executor)
    q.start()

    calls: list[str] = []

    async def pre():
        calls.append("pre")

    q.set_pre_interrupt(pre)
    await q.interrupt()
    assert calls == ["pre"]
    await q.stop()


async def test_pre_interrupt_failure_does_not_block_interrupt():
    q = ActionQueue(timeout=5.0)
    q.set_executor(_noop_executor)
    q.start()

    async def boom():
        raise RuntimeError("pre-hook explodes")

    q.set_pre_interrupt(boom)
    await q.interrupt()
    await q.stop()


async def test_interrupt_works_with_no_pre_hook():
    q = ActionQueue(timeout=5.0)
    q.set_executor(_noop_executor)
    q.start()
    await q.interrupt()
    await q.stop()
