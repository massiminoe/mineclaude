"""Reflex layer — fast-path responses to mod-emitted events.

The Fabric mod emits events over the existing /events WS for situations
that demand sub-second reaction (damage taken, lava, drowning, tool
breaking). The main Claude loop runs at multi-second cadence, which is
the wrong tool for these. This module owns the registry that dispatches
those events to handlers, the recent-fire buffer that surfaces what
happened in the next gameState injection, and the preemption hook into
the action queue + in-flight chat task.

A handler with `preempts=True` causes the dispatcher to call
`agent._preempt()` BEFORE running the handler — that halts Baritone,
cancels the running newAction, and cancels any in-flight Claude
iteration. Handlers that decide preemption conditionally (e.g.
damage_taken, which only acts on hostile-mob hits) register with
`preempts=False` and call `agent._preempt()` themselves.

Latest-wins: handlers run as their own asyncio.Tasks. When a new reflex
fires, the registry cancels any in-flight prior handler before running
the new one. The rule is "the reaction to the latest event preempts
the reaction to the prior event" — flee should interrupt retaliation
in progress, lava-escape should interrupt flee, and so on. Without
this, a long-running handler (notably the looping /attack call) would
be insulated from later, more urgent reflexes.

Resume: a handler with `resumes_on_complete=True` causes the registry
to stage a synthetic user message and set the chat trigger after the
handler returns successfully (not on cancellation). This restarts the
Claude loop so it can react to whatever the reflex just did. Handlers
should only return once the recovery is actually complete — e.g. the
drowning handler awaits its escape goto rather than fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

# Block names (Registry path, no `minecraft:` prefix) that are never valid
# "shore" tiles — standing on them would just put us back in a hazard. The
# `/nearby/blocks` endpoint also includes flowing variants for water/lava.
HAZARD_BLOCKS = frozenset({
    "water", "flowing_water",
    "lava", "flowing_lava",
    "fire", "soul_fire",
    "magma_block",
    "cactus",
    "sweet_berry_bush",
    "powder_snow",
})

if TYPE_CHECKING:
    from agent.agent import Agent

logger = logging.getLogger(__name__)

# Maximum reflex fires retained for gameState injection. Three are shown to
# Claude; the buffer is larger so a burst doesn't immediately push older
# entries out before the next iteration reads them.
RECENT_MAXLEN = 10

# Health threshold (HP, 20 = full) below which a damage_taken from a hostile
# mob switches from fight to flee. 6 = 3 hearts: at this point a single
# follow-up hit can kill, so disengage.
LOW_HP_THRESHOLD = 6.0

# How far to walk away from an attacker when fleeing.
FLEE_DISTANCE = 10.0

# Shore-finder parameters. `/nearby/blocks` is queried at SEARCH_RADIUS;
# we only consider candidates within MAX_DISTANCE so that a candidate's
# (y+1, y+2) air check is reliable — those positions are guaranteed to be
# inside the queried cube and therefore would be in the response if they
# weren't air. SEARCH_RADIUS - MAX_DISTANCE = 2 = the air-column height.
SHORE_SEARCH_RADIUS = 14
SHORE_MAX_DISTANCE = 12
# Don't path far up or down a cliff to reach a "shore" — that's a Claude
# decision, not a reflex one. 4 covers a normal pool/lake bank.
SHORE_MAX_Y_DELTA = 4


# Weapon priority for damage_taken retaliation. Sword tier first (highest DPS
# under the looping /attack — sword has 1.6 atk/sec vs axe's 1.0, so sword
# wins for sustained combat even though axes hit harder per-swing). Within a
# class, descend by material tier. Anything not on this list is considered
# bare-handed for the purposes of the equip step.
WEAPON_PRIORITY: tuple[str, ...] = (
    "netherite_sword", "diamond_sword", "iron_sword",
    "stone_sword", "golden_sword", "wooden_sword",
    "netherite_axe", "diamond_axe", "iron_axe",
    "stone_axe", "golden_axe", "wooden_axe",
)


HandlerFn = Callable[["Agent", dict], Coroutine[Any, Any, None]]


def pick_best_weapon(inventory: list[dict]) -> str | None:
    """Return the best-tier weapon name in inventory, or None.

    Inventory entries follow the bridge wire format: dicts with `name`
    and `count`. Strips the `minecraft:` prefix to be consistent with how
    the rest of the agent handles registry paths.
    """
    have: set[str] = set()
    for entry in inventory:
        if entry.get("count", 0) <= 0:
            continue
        name = (entry.get("name") or "").removeprefix("minecraft:")
        if name:
            have.add(name)
    for w in WEAPON_PRIORITY:
        if w in have:
            return w
    return None


@dataclass
class ReflexHandler:
    event_type: str
    handle: HandlerFn
    preempts: bool = False
    cooldown_s: float = 0.0
    # When True, the registry stages a synthetic user message and sets the
    # chat trigger after the handler completes successfully — Claude wakes
    # up and reacts to whatever the reflex did. Skipped when the handler
    # raises or is cancelled by a newer reflex (the newer reflex will fire
    # its own resume).
    resumes_on_complete: bool = False
    _last_fire: float = field(default=0.0, repr=False)


async def stub_handler(agent: "Agent", data: dict) -> None:
    """No-op handler — used when the only desired effect is preempt + record."""
    return None


class ReflexRegistry:
    """Dispatches mod-emitted reflex events to handlers.

    The registry owns four pieces of cross-cutting state:
      * the handler table (event_type → ReflexHandler)
      * the `recent` buffer of the last N fires for gameState rendering
      * `last_fire_ts`, the wall-clock of the most recent fire (kept as a
        cheap signal future monitor features may consume).
      * `_active_handler_task`, the currently-running handler. A new
        dispatch cancels this before spawning the new handler — the
        latest reflex preempts the prior reflex's reaction.

    `dispatch` is non-blocking with respect to the handler body: it
    spawns the handler as a task and returns. Tests use `flush()` to
    await the in-flight handler. Production callers (the events WS
    consumer) MUST NOT await the handler from inside dispatch — that
    would re-block the consumer on a long-running reflex (notably the
    looping /attack call from damage_taken) and stop later events from
    arriving.
    """

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self.recent: deque[dict] = deque(maxlen=RECENT_MAXLEN)
        self._handlers: dict[str, ReflexHandler] = {}
        self.last_fire_ts: float = 0.0
        self._active_handler_task: asyncio.Task | None = None

    def register(self, handler: ReflexHandler) -> None:
        self._handlers[handler.event_type] = handler

    def known_types(self) -> set[str]:
        return set(self._handlers)

    async def dispatch(self, event_type: str, data: dict) -> None:
        handler = self._handlers.get(event_type)
        if handler is None:
            # Fail-soft: an event type we haven't registered for is just
            # ignored, not an error. Keeps the WS consumer alive.
            return

        now = time.monotonic()
        if handler.cooldown_s > 0 and (now - handler._last_fire) < handler.cooldown_s:
            return
        handler._last_fire = now
        self.last_fire_ts = now

        entry = {"type": event_type, "data": data, "ts": time.time()}
        self.recent.append(entry)

        try:
            self.agent._slog("reflex_fired", type=event_type, data=data)
        except Exception:
            logger.exception("reflex slog failed")

        try:
            await self.agent._emit("reflex:fired", entry)
        except Exception:
            logger.exception("reflex emit failed")

        # Latest-wins: cancel any prior handler before this one runs. A
        # long-running handler (looping /attack, /goto pathing) must yield
        # to a newer reflex regardless of whether the new handler issues
        # its own _preempt(). Awaiting the cancel ensures the prior task
        # has actually unwound before we start the new handler — otherwise
        # the new handler's bridge calls could race the old one's.
        prior = self._active_handler_task
        if prior is not None and not prior.done():
            prior.cancel()
            try:
                await prior
            except (asyncio.CancelledError, Exception):
                pass

        if handler.preempts:
            try:
                await self.agent._preempt()
            except Exception:
                logger.exception("reflex preempt failed")

        # Spawn the handler. We do NOT await it — the events consumer
        # must stay responsive to subsequent events (notably so a more
        # urgent reflex can cancel us via the prior-task cancel above).
        self._active_handler_task = asyncio.create_task(
            self._run_handler(handler, data),
            name=f"reflex_handler:{event_type}",
        )

    async def _run_handler(self, handler: ReflexHandler, data: dict) -> None:
        try:
            await handler.handle(self.agent, data)
        except asyncio.CancelledError:
            # Cancelled by a newer reflex (or shutdown). Propagate so the
            # task's done state reflects the cancellation — `prior.cancel()
            # + await prior` upstream relies on this. The newer reflex will
            # fire its own resume, so we deliberately skip resume here.
            raise
        except Exception:
            logger.exception(f"reflex handler {handler.event_type} raised")
            return
        # Handler completed successfully. Stage a resume so Claude wakes
        # up and reacts. _stage_resume is sync — no await between the
        # handler returning and the trigger flipping, so a cancellation
        # arriving in this window is impossible (Python only checks at
        # await points).
        if handler.resumes_on_complete:
            try:
                self.agent._stage_resume(handler.event_type)
            except Exception:
                logger.exception("reflex resume staging failed")

    async def flush(self) -> None:
        """Test helper: await any in-flight handler. No-op in production."""
        task = self._active_handler_task
        if task is not None and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# Event type names — the wire shape from the mod. Kept here so callers can
# import a single source of truth and the agent's _handle_event dispatch
# can route by membership test.
REFLEX_EVENT_TYPES = (
    "damage_taken",
    "entered_lava",
    "started_drowning",
    "tool_broke",
)


# --- Default handlers ------------------------------------------------------


async def damage_taken_handler(agent: "Agent", data: dict) -> None:
    """React to incoming damage.

    No attacker (fall, fire, suffocation): record only — caller has nothing
    productive to do, and most of these are over by the time we see them.

    Hostile mob attacker:
      * post-hit HP ≤ LOW_HP_THRESHOLD → flee 10 blocks opposite the attacker
      * otherwise → equip the best weapon we have, then attack the
        attacker by entity id (the bridge loops swings until kill)

    In both action branches we preempt first (cancel any in-flight nav /
    Claude turn / prior /attack loop), then issue the bridge call ourselves.
    """
    attacker_kind = data.get("attacker_kind")
    if not attacker_kind:
        return  # not from a mob — record-only

    attacker_id = data.get("attacker_id")
    attacker_pos = data.get("attacker_pos") or {}
    hp_before = data.get("hp_before", 20.0)
    amount = data.get("amount", 0.0)
    hp_after = hp_before - amount

    await agent._preempt()

    if hp_after <= LOW_HP_THRESHOLD:
        ax = attacker_pos.get("x")
        az = attacker_pos.get("z")
        if ax is None or az is None:
            return  # no direction to flee
        try:
            status = (await agent.bridge.get_status()).data
        except Exception:
            return
        pos = status.get("position") or {}
        px = pos.get("x")
        py = pos.get("y")
        pz = pos.get("z")
        if px is None or py is None or pz is None:
            return
        dx = px - ax
        dz = pz - az
        mag = math.sqrt(dx * dx + dz * dz)
        if mag < 0.1:
            return  # standing on the attacker — direction undefined, skip
        fx = px + (dx / mag) * FLEE_DISTANCE
        fz = pz + (dz / mag) * FLEE_DISTANCE
        try:
            await agent.bridge.goto(fx, py, fz)
        except Exception:
            logger.exception("flee goto failed")
        return

    if attacker_id is None:
        return

    # Best-effort weapon equip. Skip if status fetch or equip fails — we'd
    # rather retaliate bare-handed than do nothing, and bridge.equip is
    # idempotent (held weapon → cheap re-verify, no swap).
    try:
        status = (await agent.bridge.get_status()).data
        weapon = pick_best_weapon(status.get("inventory") or [])
        if weapon is not None:
            try:
                await agent.bridge.equip(weapon)
            except Exception:
                logger.exception("retaliation equip failed")
    except Exception:
        logger.exception("retaliation status fetch failed")

    try:
        resp = await agent.bridge.attack(str(attacker_id))
    except Exception:
        logger.exception("retaliation attack failed")
        return
    if resp.status == "error":
        # Bridge non-2xx responses don't raise — surface them so silent
        # failures (entity despawned, navigation failed, matcher miss)
        # don't masquerade as successful retaliation.
        logger.warning("retaliation attack rejected: %s", resp.message)
        try:
            agent._slog("retaliation_failed", attacker_id=attacker_id, attacker_kind=attacker_kind, message=resp.message)
        except Exception:
            pass


async def _escape_to_shore(agent: "Agent") -> None:
    """Find the nearest standable land tile and ask Baritone to walk there.

    A "shore" is any non-hazard solid block whose two blocks above are air
    (room for the player to stand) and whose Y is within SHORE_MAX_Y_DELTA
    of the player's. We rely on `/nearby/blocks` filtering air out of its
    response: a position absent from the response within the queried cube
    is, by definition, air.

    Best-effort. If nothing qualifies — sealed pool, tiny prison, far
    out at sea — we leave the bot where it is. Claude sees the reflex
    entry in `recent` on the next iteration and can take over.
    """
    try:
        status = (await agent.bridge.get_status()).data
    except Exception:
        return
    pos = status.get("position") or {}
    py = pos.get("y")
    if py is None:
        return

    try:
        resp = await agent.bridge.get_nearby_blocks(radius=SHORE_SEARCH_RADIUS)
    except Exception:
        return
    blocks = resp.data.get("blocks") or []
    if not blocks:
        return

    occupied = {(b["x"], b["y"], b["z"]) for b in blocks}

    # `/nearby/blocks` returns sorted by distance — first qualifying
    # candidate is the closest shore.
    for b in blocks:
        if b.get("distance", float("inf")) > SHORE_MAX_DISTANCE:
            break
        if b.get("name", "") in HAZARD_BLOCKS:
            continue
        x, y, z = b["x"], b["y"], b["z"]
        if (x, y + 1, z) in occupied or (x, y + 2, z) in occupied:
            continue
        if abs(y - py) > SHORE_MAX_Y_DELTA:
            continue
        try:
            await agent.bridge.goto(float(x), float(y + 1), float(z))
        except Exception:
            logger.exception("escape goto failed")
        return


async def entered_lava_handler(agent: "Agent", data: dict) -> None:
    """Find shore and walk there. Preempt has already fired (preempts=True)."""
    await _escape_to_shore(agent)


async def started_drowning_handler(agent: "Agent", data: dict) -> None:
    """Surface, then find shore and walk there. Preempt has already fired.

    The /surface call is a workaround for a Baritone limitation: from a
    fully-submerged start position, `#goto` instantly fails (PathNode map
    size: 1) and the bot floats in place until our stall detection bails.
    Holding forward+jump+sprint via vanilla input keys gets the player to
    the surface, after which Baritone can path normally to the shore tile
    selected below.
    """
    try:
        await agent.bridge.surface()
    except Exception:
        logger.exception("surface failed during drowning escape")
    await _escape_to_shore(agent)


def register_default_handlers(registry: ReflexRegistry) -> None:
    """Register every v1 reflex handler with its preempt + cooldown policy.

    Notes per event:
      * damage_taken: preempts=False here because the handler decides
        conditionally (no preempt for fall damage etc.). Resume only fires
        when the handler took an action — record-only paths return before
        the resume staging.
      * entered_lava / started_drowning: always preempt + escape. The
        handler awaits goto arrival (bridge.goto polls) before returning,
        so resume fires once the bot is actually on shore.
      * tool_broke: preempt only — handler is a stub. Resume fires
        immediately so Claude can decide whether to re-equip and continue.
    """
    registry.register(ReflexHandler(
        event_type="damage_taken",
        handle=damage_taken_handler,
        preempts=False,
        # 10s coalesces a damage burst into a single reflex fire. Sustained
        # combat (multiple phantoms, mob swarm at night) can deliver hits
        # faster than a Claude turn completes — at the old 0.5s cooldown,
        # every hit cancelled the in-flight chat and the agent never got a
        # turn off. The first hit's handler (retaliate via looping /attack,
        # or flee) is already running; suppressing the next 10s of fires
        # lets it finish.
        cooldown_s=30.0,
        resumes_on_complete=True,
    ))
    registry.register(ReflexHandler(
        event_type="entered_lava",
        handle=entered_lava_handler,
        preempts=True,
        cooldown_s=5.0,
        resumes_on_complete=True,
    ))
    registry.register(ReflexHandler(
        event_type="started_drowning",
        handle=started_drowning_handler,
        preempts=True,
        cooldown_s=10.0,
        resumes_on_complete=True,
    ))
    registry.register(ReflexHandler(
        event_type="tool_broke",
        handle=stub_handler,
        preempts=True,
        cooldown_s=1.0,
        resumes_on_complete=True,
    ))


def format_recent(recent: list[dict], *, limit: int = 3, now: float | None = None) -> str:
    """Render the recent reflex buffer into the gameState section.

    Returns an empty string when there's nothing to show, so the caller
    can `if s: lines.append(s)` without an extra branch.
    """
    if not recent:
        return ""
    if now is None:
        now = time.time()
    # Most recent first, capped.
    items = list(reversed(recent))[:limit]
    rows = []
    for entry in items:
        ago = max(0, int(now - entry.get("ts", now)))
        rows.append(f"- {ago}s ago: {entry['type']}{_format_data_hint(entry)}")
    return "=== Recent reflex events ===\n" + "\n".join(rows)


def _format_data_hint(entry: dict) -> str:
    """Compact hint for the data payload. Kept minimal — Claude reads the
    session log for full detail; the gameState line is just situational
    awareness."""
    et = entry.get("type")
    data = entry.get("data") or {}
    if et == "damage_taken":
        amount = data.get("amount")
        attacker = data.get("attacker_kind")
        source = data.get("source")
        bits = []
        if amount is not None:
            bits.append(f"{amount} dmg")
        if attacker:
            bits.append(f"from {attacker}")
        elif source:
            bits.append(f"from {source}")
        return f" ({', '.join(bits)})" if bits else ""
    if et == "tool_broke":
        item = data.get("item")
        return f" ({item})" if item else ""
    return ""
