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
    WEAPON_PRIORITY,
    ReflexHandler,
    ReflexRegistry,
    damage_taken_handler,
    entered_lava_handler,
    format_recent,
    pick_best_weapon,
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
        self.attack_blocking = False  # if True, attack() blocks forever (test cancellation)
        self.attack_stop_calls = 0
        self.equip_calls: list[str] = []
        self.stop_calls = 0
        self.surface_calls = 0
        self.nearby_blocks_radii: list[int] = []
        self.call_order: list[str] = []

    async def get_status(self) -> BridgeResponse:
        return BridgeResponse("success", "ok", dict(self._status))

    async def get_nearby_blocks(self, radius: int = 8, block_types=None) -> BridgeResponse:
        self.nearby_blocks_radii.append(radius)
        # Mirror the real endpoint: sorted by distance ascending.
        sorted_blocks = sorted(self._blocks, key=lambda b: b.get("distance", 0.0))
        return BridgeResponse("success", "ok", {"blocks": sorted_blocks})

    async def goto(self, x, y, z):
        self.goto_calls.append((x, y, z))
        self.call_order.append("goto")
        return BridgeResponse("success", "ok", {})

    async def attack(self, entity_id):
        self.attack_calls.append(entity_id)
        self.call_order.append("attack")
        if self.attack_blocking:
            await asyncio.Event().wait()  # never resolves; awaits cancellation
        return BridgeResponse("success", "ok", {})

    async def attack_stop(self):
        self.attack_stop_calls += 1
        return BridgeResponse("success", "ok", {})

    async def equip(self, item, slot="hand"):
        self.equip_calls.append(item)
        self.call_order.append(f"equip:{item}")
        return BridgeResponse("success", f"Equipped {item}", {"equipped": True})

    async def stop(self):
        self.stop_calls += 1
        return BridgeResponse("success", "ok", {})

    async def surface(self, timeout: float = 2.0):
        self.surface_calls += 1
        self.call_order.append("surface")
        return BridgeResponse("success", "ok", {"surfaced": True, "ticks": 0})


def _block(name: str, x: int, y: int, z: int, distance: float = 1.0) -> dict:
    return {"name": name, "x": x, "y": y, "z": z, "distance": distance}


class FakeAgent:
    """Minimal Agent stand-in. The registry only touches `_slog`,
    `_emit`, `_preempt`, `_stage_resume`, and (via handlers) `bridge`."""

    def __init__(self, bridge: _FakeBridge | None = None):
        self.slog_calls: list[tuple[str, dict]] = []
        self.emit_calls: list[tuple[str, dict]] = []
        self.preempt_calls = 0
        self.resume_calls: list[str] = []
        self.queue = _FakeQueue()
        self.bridge = bridge or _FakeBridge()

    def _slog(self, event: str, **data) -> None:
        self.slog_calls.append((event, data))

    async def _emit(self, event: str, data) -> None:
        self.emit_calls.append((event, data))

    async def _preempt(self) -> None:
        self.preempt_calls += 1

    def _stage_resume(self, event_type: str) -> None:
        self.resume_calls.append(event_type)


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
    await reg.flush()
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
    await reg.flush()  # the exception happens inside the spawned task
    assert len(reg.recent) == 1
    # Subsequent dispatch must still work — the registry's _active_handler_task
    # being in a failed state shouldn't poison the next dispatch's cancel-prior
    # step.
    reg.register(ReflexHandler(event_type="entered_lava", handle=stub_handler))
    await reg.dispatch("entered_lava", {})
    await reg.flush()
    assert len(reg.recent) == 2


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


async def test_register_default_handlers_all_resume_on_complete():
    """Every default handler reprompts Claude after recovery so the agent
    can react to whatever the reflex did. If a future handler shouldn't
    resume (e.g. a hypothetical stop event), flip resumes_on_complete to
    False at registration time."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    register_default_handlers(reg)
    for et in REFLEX_EVENT_TYPES:
        assert reg._handlers[et].resumes_on_complete is True, et


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


async def test_damage_taken_high_hp_equips_best_weapon_before_attack():
    """When retaliating, scan inventory and equip the best weapon, then
    attack. The equip must happen before the attack call so the bridge
    swing uses the right item."""
    bridge = _FakeBridge({
        "position": {"x": 10.0, "y": 64.0, "z": 5.0},
        "inventory": [
            {"name": "wooden_sword", "count": 1},
            {"name": "iron_sword", "count": 1},  # best available
            {"name": "stone_axe", "count": 1},
        ],
    })
    agent = FakeAgent(bridge)
    await damage_taken_handler(agent, {
        "attacker_kind": "zombie",
        "attacker_id": 42,
        "attacker_pos": {"x": 12.0, "y": 64.0, "z": 5.0},
        "amount": 2.0,
        "hp_before": 18.0,
    })
    assert bridge.equip_calls == ["iron_sword"]
    assert bridge.attack_calls == ["42"]
    # Equip happens before attack — verify call order.
    assert bridge.call_order.index("equip:iron_sword") < bridge.call_order.index("attack")


async def test_damage_taken_high_hp_no_weapon_retaliates_bare_handed():
    """No weapon in inventory → skip equip, still retaliate."""
    bridge = _FakeBridge({
        "position": {"x": 10.0, "y": 64.0, "z": 5.0},
        "inventory": [{"name": "dirt", "count": 64}],
    })
    agent = FakeAgent(bridge)
    await damage_taken_handler(agent, {
        "attacker_kind": "zombie",
        "attacker_id": 42,
        "attacker_pos": {"x": 12.0, "y": 64.0, "z": 5.0},
        "amount": 2.0,
        "hp_before": 18.0,
    })
    assert bridge.equip_calls == []
    assert bridge.attack_calls == ["42"]


async def test_damage_taken_high_hp_equip_failure_still_retaliates():
    """Equip raising must not block retaliation — bare-handed beats nothing."""
    class _BoomEquipBridge(_FakeBridge):
        async def equip(self, item, slot="hand"):
            raise RuntimeError("equip failed")

    bridge = _BoomEquipBridge({
        "position": {"x": 10.0, "y": 64.0, "z": 5.0},
        "inventory": [{"name": "iron_sword", "count": 1}],
    })
    agent = FakeAgent(bridge)
    await damage_taken_handler(agent, {
        "attacker_kind": "zombie",
        "attacker_id": 42,
        "attacker_pos": {"x": 12.0, "y": 64.0, "z": 5.0},
        "amount": 2.0,
        "hp_before": 18.0,
    })
    assert bridge.attack_calls == ["42"]


def test_pick_best_weapon_picks_highest_tier_sword():
    inv = [
        {"name": "wooden_sword", "count": 1},
        {"name": "diamond_sword", "count": 1},
        {"name": "iron_sword", "count": 1},
    ]
    assert pick_best_weapon(inv) == "diamond_sword"


def test_pick_best_weapon_falls_back_to_axe_when_no_sword():
    inv = [{"name": "stone_axe", "count": 1}, {"name": "iron_axe", "count": 1}]
    assert pick_best_weapon(inv) == "iron_axe"


def test_pick_best_weapon_prefers_any_sword_over_any_axe():
    inv = [{"name": "netherite_axe", "count": 1}, {"name": "wooden_sword", "count": 1}]
    # Sword tier sorts above axe tier — sustained DPS via 1.6 atk/sec wins
    # over per-swing damage in a looping fight.
    assert pick_best_weapon(inv) == "wooden_sword"


def test_pick_best_weapon_strips_minecraft_prefix():
    inv = [{"name": "minecraft:iron_sword", "count": 1}]
    assert pick_best_weapon(inv) == "iron_sword"


def test_pick_best_weapon_ignores_zero_count_entries():
    inv = [{"name": "diamond_sword", "count": 0}, {"name": "iron_sword", "count": 1}]
    assert pick_best_weapon(inv) == "iron_sword"


def test_pick_best_weapon_returns_none_for_no_weapons():
    assert pick_best_weapon([{"name": "dirt", "count": 64}]) is None
    assert pick_best_weapon([]) is None


def test_weapon_priority_is_complete_sword_then_axe_descending():
    """Pin the order so future edits don't accidentally reshuffle tiers."""
    assert WEAPON_PRIORITY[:6] == (
        "netherite_sword", "diamond_sword", "iron_sword",
        "stone_sword", "golden_sword", "wooden_sword",
    )
    assert WEAPON_PRIORITY[6:] == (
        "netherite_axe", "diamond_axe", "iron_axe",
        "stone_axe", "golden_axe", "wooden_axe",
    )


# --- latest-reflex preempts prior handler ----------------------------------


async def test_dispatch_cancels_prior_handler_task_on_new_dispatch():
    """A new reflex must cancel the prior handler before its own runs.

    Without this, a long-running handler (looping /attack from
    damage_taken) would survive across newer reflexes — the bot would
    keep swinging while a flee handler tried to path away.
    """
    agent = FakeAgent()
    reg = ReflexRegistry(agent)

    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def long_running(_a, _d):
        started.set()
        try:
            await asyncio.Event().wait()  # park forever
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def quick(_a, _d):
        return None

    reg.register(ReflexHandler(event_type="damage_taken", handle=long_running))
    reg.register(ReflexHandler(event_type="entered_lava", handle=quick))

    await reg.dispatch("damage_taken", {})
    await started.wait()  # ensure the long handler is actually running

    await reg.dispatch("entered_lava", {})  # latest wins → cancels long_running
    assert cancelled.is_set()
    await reg.flush()


async def test_dispatch_awaits_prior_cancel_before_new_handler_starts():
    """The dispatch contract is: prior task is fully unwound before the new
    handler's body starts. This prevents bridge calls racing across
    reflexes."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    order: list[str] = []

    async def slow(_a, _d):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # let cleanup interleave
            order.append("slow_unwind")
            raise

    async def quick(_a, _d):
        order.append("quick_start")

    reg.register(ReflexHandler(event_type="damage_taken", handle=slow))
    reg.register(ReflexHandler(event_type="entered_lava", handle=quick))

    await reg.dispatch("damage_taken", {})
    # Yield so the slow handler actually starts — otherwise the cancel hits
    # a not-yet-started task and there's nothing to unwind.
    await asyncio.sleep(0)

    await reg.dispatch("entered_lava", {})
    await reg.flush()
    assert order == ["slow_unwind", "quick_start"]


async def test_dispatch_first_dispatch_no_prior_to_cancel():
    """Smoke: registry handles the no-prior case cleanly."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(event_type="damage_taken", handle=stub_handler))
    await reg.dispatch("damage_taken", {})
    await reg.flush()
    assert len(reg.recent) == 1


async def test_resume_fires_after_handler_completes():
    """A handler with resumes_on_complete=True triggers _stage_resume on
    successful completion."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(
        event_type="tool_broke", handle=stub_handler, resumes_on_complete=True,
    ))
    await reg.dispatch("tool_broke", {"item": "iron_pickaxe"})
    await reg.flush()
    assert agent.resume_calls == ["tool_broke"]


async def test_resume_skipped_when_handler_cancelled_by_newer_reflex():
    """If a newer reflex cancels the in-flight handler, the cancelled
    handler must NOT fire its resume — the newer reflex will fire its own
    resume after its handler completes. Without this, both reflexes would
    stage resumes and Claude would see a double prompt for one logical
    event sequence."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    started = asyncio.Event()

    async def parked(_a, _d):
        started.set()
        await asyncio.Event().wait()  # park forever, awaits cancellation

    reg.register(ReflexHandler(
        event_type="damage_taken", handle=parked, resumes_on_complete=True,
    ))
    reg.register(ReflexHandler(
        event_type="entered_lava", handle=stub_handler, resumes_on_complete=True,
    ))

    await reg.dispatch("damage_taken", {})
    await started.wait()
    await reg.dispatch("entered_lava", {})  # cancels parked, runs stub
    await reg.flush()
    # Only the new (winning) reflex resumed.
    assert agent.resume_calls == ["entered_lava"]


async def test_resume_skipped_when_handler_raises():
    """A raised handler shouldn't trigger resume — recovery is incomplete
    by definition."""
    agent = FakeAgent()
    reg = ReflexRegistry(agent)

    async def boom(_a, _d):
        raise RuntimeError("nope")

    reg.register(ReflexHandler(
        event_type="tool_broke", handle=boom, resumes_on_complete=True,
    ))
    await reg.dispatch("tool_broke", {})
    await reg.flush()
    assert agent.resume_calls == []


async def test_resume_skipped_when_flag_false():
    agent = FakeAgent()
    reg = ReflexRegistry(agent)
    reg.register(ReflexHandler(
        event_type="tool_broke", handle=stub_handler, resumes_on_complete=False,
    ))
    await reg.dispatch("tool_broke", {})
    await reg.flush()
    assert agent.resume_calls == []


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


async def test_started_drowning_surfaces_before_walking_to_shore():
    """Baritone can't path from a submerged start (PathNode map size: 1), so the
    handler must drive direct-input swim-up first, *then* hand off to goto."""
    blocks = [_block("dirt", 2, 61, 0, 2.5)]
    bridge = _FakeBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
    assert bridge.surface_calls == 1
    assert bridge.call_order == ["surface", "goto"]


async def test_started_drowning_still_walks_when_surface_fails():
    """Surface failure shouldn't abort the escape — Baritone might still
    succeed if the player happened to drift to the surface, and even a failed
    goto leaves a useful reflex entry for Claude to react to next iteration."""
    class _NoSurfaceBridge(_FakeBridge):
        async def surface(self, timeout: float = 2.0):
            raise RuntimeError("bridge down")
    blocks = [_block("dirt", 2, 61, 0, 2.5)]
    bridge = _NoSurfaceBridge({"position": {"x": 0.0, "y": 62.0, "z": 0.0}}, blocks=blocks)
    agent = FakeAgent(bridge)
    await started_drowning_handler(agent, {})
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


# --- format_game_state events section --------------------------------------


def _bare_status() -> dict:
    return {
        "position": {"x": 0, "y": 64, "z": 0},
        "health": 20.0,
        "hunger": 20,
        "biome": "plains",
        "time": 0,
        "inventory": [],
    }


def test_format_game_state_omits_events_section_when_empty():
    out = format_game_state(_bare_status(), {"running": None, "pending": [], "recent": []}, events=[])
    assert "Events since last gameState" not in out


def test_format_game_state_omits_events_section_when_none():
    out = format_game_state(_bare_status(), {"running": None, "pending": [], "recent": []}, events=None)
    assert "Events since last gameState" not in out


def test_format_game_state_renders_block_events_one_line_each():
    events = [
        {"ts_ms": 1_700_000_000_000, "type": "block_broken", "block": "stone",
         "pos": {"x": 100, "y": 64, "z": 50}},
        {"ts_ms": 1_700_000_000_420, "type": "block_broken", "block": "stone",
         "pos": {"x": 100, "y": 65, "z": 50}},
        {"ts_ms": 1_700_000_001_180, "type": "block_placed", "block": "torch",
         "pos": {"x": 101, "y": 65, "z": 50}},
    ]
    out = format_game_state(
        _bare_status(),
        {"running": None, "pending": [], "recent": []},
        events=events,
    )
    # Header reports event count and span.
    assert "=== Events since last gameState (3 events, 1.18s span) ===" in out
    # One line per event, in order, with wallclock deltas from oldest.
    assert "[+0.00s] block_broken stone @ (100, 64, 50)" in out
    assert "[+0.42s] block_broken stone @ (100, 65, 50)" in out
    assert "[+1.18s] block_placed torch @ (101, 65, 50)" in out


def test_format_game_state_renders_entity_attacked_with_rounded_pos():
    events = [
        {"ts_ms": 1_700_000_000_000, "type": "entity_attacked", "kind": "zombie",
         "entity_id": 4421, "pos": {"x": 103.234, "y": 64.0, "z": 49.876}},
    ]
    out = format_game_state(
        _bare_status(),
        {"running": None, "pending": [], "recent": []},
        events=events,
    )
    assert "[+0.00s] entity_attacked zombie #4421 @ (103.2, 64.0, 49.9)" in out


def test_format_game_state_does_not_collapse_repeats():
    # Same block, same coords, three times. Verbose-by-design: three lines.
    events = [
        {"ts_ms": 1_700_000_000_000, "type": "block_broken", "block": "oak_log",
         "pos": {"x": 5, "y": 64, "z": 5}},
        {"ts_ms": 1_700_000_000_500, "type": "block_broken", "block": "oak_log",
         "pos": {"x": 5, "y": 64, "z": 5}},
        {"ts_ms": 1_700_000_001_000, "type": "block_broken", "block": "oak_log",
         "pos": {"x": 5, "y": 64, "z": 5}},
    ]
    out = format_game_state(
        _bare_status(),
        {"running": None, "pending": [], "recent": []},
        events=events,
    )
    rendered = out.splitlines()
    matching = [ln for ln in rendered if "block_broken oak_log @ (5, 64, 5)" in ln]
    assert len(matching) == 3


def test_format_game_state_renders_unknown_event_type_as_fallback():
    events = [
        {"ts_ms": 1_700_000_000_000, "type": "future_event_kind",
         "some_field": "value", "another": 42},
    ]
    out = format_game_state(
        _bare_status(),
        {"running": None, "pending": [], "recent": []},
        events=events,
    )
    assert "[+0.00s] future_event_kind" in out
    assert "some_field" in out
    assert "value" in out


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
