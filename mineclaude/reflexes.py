"""Reflex layer — fast-path responses to mod-emitted events.

The Fabric mod emits events over the existing /events WS for situations
that demand sub-second reaction (damage taken, lava, drowning, tool
breaking). The main Claude loop runs at multi-second cadence, which is
the wrong tool for these. This module owns the registry that dispatches
those events to handlers, the recent-fire buffer that surfaces what
happened in the next gameState injection, and the preemption hook into
the action queue + in-flight chat task.

A handler with `preempts=True` causes the dispatcher to call
`controller.preempt()` BEFORE running the handler — that halts Baritone,
cancels the running newAction, and cancels any in-flight Claude
iteration. Handlers that decide preemption conditionally (e.g.
damage_taken, which only acts on hostile-mob hits) register with
`preempts=False` and call `controller.preempt()` themselves.

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
import contextvars
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
    from mineclaude.runtime import Controller

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

# Attacker entity kinds the damage reflex does NOT react to (no flee / no
# retaliate). The event is still recorded for the next gameState — we only
# suppress the automatic response.
#   * player  — leave PvP to Claude; auto-retaliating against another player
#               is rarely what we want.
#   * phantom — flies in swooping arcs out of melee reach, so a ground-based
#               /attack loop just thrashes without landing hits.
# Everything else with an attacker entity (hostile mobs, and provoked
# neutrals) still triggers the reflex.
NO_RETALIATE_KINDS = frozenset({"player", "phantom"})

# Global time budget for the auto-retaliation reaction (equip + attack). The
# mod's /attack loop caps itself at 30s, but a mob that flees out of melee
# while staying pathable keeps us chasing for that whole window. 15s bounds
# the reaction: on expiry timed_op issues /attack/stop and the handler returns
# normally, so resumes_on_complete wakes Claude to decide whether to re-engage.
RETALIATE_TIMEOUT_S = 15.0

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


HandlerFn = Callable[["Controller", dict], Coroutine[Any, Any, None]]


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


# --- Deadline-scoped timeouts ----------------------------------------------
#
# Some bridge ops loop server-side until a terminal condition (the /attack
# swing loop, Baritone /goto). The client call blocks for the whole loop, so
# bounding one needs two things: cancel the local await AND tell the mod to
# stop — closing the HTTP request alone leaves the loop running. `timed_op`
# pairs an op with its matching stop endpoint and enforces a budget.
#
# `deadline(t)` sets a *global* budget that every timed_op inside inherits:
# the effective per-op budget is the time left on the enclosing deadline,
# clamped further by any op-specific timeout. So a child op (e.g. a goto
# issued after an attack under the same deadline) can only tighten, never
# extend, the parent's budget. Scopes nest — an inner deadline clamps to the
# outer one.

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "reflex_deadline", default=None
)


def _remaining_budget(timeout: float | None) -> float | None:
    """Effective budget = min(op-specific timeout, time left on the enclosing
    deadline). None means unbounded (no timeout and no enclosing deadline)."""
    budget = timeout
    dl = _deadline.get()
    if dl is not None:
        remaining = dl - time.monotonic()
        budget = remaining if budget is None else min(budget, remaining)
    return budget


class deadline:
    """Establish a global time budget for the timed_op calls within.

    Children inherit the remaining budget; nested scopes clamp to the
    tightest. A None timeout is a passthrough so call sites stay uniform.

    Implemented as a class rather than @contextlib.contextmanager: a
    ContextVar set inside a generator-based CM isn't visible to the with
    body, whereas __enter__ runs directly in the caller's context.
    """

    def __init__(self, timeout: float | None):
        self._timeout = timeout
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "deadline":
        if self._timeout is None:
            return self
        new_deadline = time.monotonic() + self._timeout
        outer = _deadline.get()
        if outer is not None:
            new_deadline = min(new_deadline, outer)
        self._token = _deadline.set(new_deadline)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _deadline.reset(self._token)
            self._token = None


async def timed_op(
    op: Coroutine[Any, Any, Any],
    stop: Callable[[], Coroutine[Any, Any, Any]],
    *,
    timeout: float | None = None,
) -> Any:
    """Await a long, server-looping bridge op under the active deadline.

    `op` is the coroutine (e.g. ``bridge.attack(id)``); `stop` is its matching
    halt (e.g. ``bridge.attack_stop``). On budget expiry we cancel the local
    await AND call ``stop()`` to halt the server loop, then return None. Normal
    completion returns the op's result; real exceptions propagate. Cancellation
    by a newer reflex propagates untouched — the preempt path issues the stop
    in that case.
    """
    budget = _remaining_budget(timeout)
    if budget is not None and budget <= 0:
        # Parent budget already spent before we started — don't run the op.
        # Close the un-awaited coroutine so Python doesn't warn.
        op.close()
        return None
    if budget is None:
        return await op
    try:
        return await asyncio.wait_for(op, budget)
    except asyncio.TimeoutError:
        try:
            await stop()
        except Exception:
            logger.exception("timed_op cleanup stop failed")
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


async def stub_handler(controller: "Controller", data: dict) -> None:
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

    def __init__(self, controller: "Controller"):
        self.controller = controller
        self.recent: deque[dict] = deque(maxlen=RECENT_MAXLEN)
        self._handlers: dict[str, ReflexHandler] = {}
        self.last_fire_ts: float = 0.0
        self._active_handler_task: asyncio.Task | None = None

    def register(self, handler: ReflexHandler) -> None:
        self._handlers[handler.event_type] = handler

    def known_types(self) -> set[str]:
        return set(self._handlers)

    def get(self, event_type: str) -> ReflexHandler | None:
        """Look up the handler for an event type (None if none registered).
        Used by Runtime.get_handler / set_handler to read + replace policy."""
        return self._handlers.get(event_type)

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
            self.controller.slog("reflex_fired", type=event_type, data=data)
        except Exception:
            logger.exception("reflex slog failed")

        try:
            await self.controller.emit_event("reflex:fired", entry)
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
                await self.controller.preempt()
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
            await handler.handle(self.controller, data)
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
                self.controller.resume(handler.event_type)
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
    "hostile_nearby",
)


# --- Default handlers ------------------------------------------------------


async def damage_taken_handler(controller: "Controller", data: dict) -> None:
    """React to incoming damage.

    No attacker (fall, fire, suffocation): record only — caller has nothing
    productive to do, and most of these are over by the time we see them.

    Player or phantom attacker (NO_RETALIATE_KINDS): record only — we leave
    PvP to Claude and don't chase swooping phantoms with a ground melee loop.

    Other entity attacker (hostile mobs, provoked neutrals):
      * post-hit HP ≤ LOW_HP_THRESHOLD → flee 10 blocks opposite the attacker
      * otherwise → equip the best weapon we have, then attack the
        attacker by entity id (the bridge loops swings until kill)

    In both action branches we preempt first (cancel any in-flight nav /
    Claude turn / prior /attack loop), then issue the bridge call ourselves.
    """
    attacker_kind = data.get("attacker_kind")
    if not attacker_kind:
        return  # environmental (fall, fire, drowning) — record-only
    if attacker_kind in NO_RETALIATE_KINDS:
        return  # players + phantoms — record-only, no flee/retaliate

    attacker_id = data.get("attacker_id")
    attacker_pos = data.get("attacker_pos") or {}
    hp_before = data.get("hp_before", 20.0)
    amount = data.get("amount", 0.0)
    hp_after = hp_before - amount

    await controller.preempt()

    if hp_after <= LOW_HP_THRESHOLD:
        ax = attacker_pos.get("x")
        az = attacker_pos.get("z")
        if ax is None or az is None:
            return  # no direction to flee
        try:
            status = (await controller.bridge.get_status()).data
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
            await controller.bridge.goto(fx, py, fz)
        except Exception:
            logger.exception("flee goto failed")
        return

    if attacker_id is None:
        return

    # The whole retaliation runs under one global budget (RETALIATE_TIMEOUT_S):
    # equip + attack share it, and any future child op (e.g. a follow-up goto)
    # inherits whatever's left. Only the server-looping /attack is enforced via
    # timed_op — the quick equip just eats into the budget.
    with deadline(RETALIATE_TIMEOUT_S):
        # Best-effort weapon equip. Skip if status fetch or equip fails — we'd
        # rather retaliate bare-handed than do nothing, and bridge.equip is
        # idempotent (held weapon → cheap re-verify, no swap).
        try:
            status = (await controller.bridge.get_status()).data
            weapon = pick_best_weapon(status.get("inventory") or [])
            if weapon is not None:
                try:
                    await controller.bridge.equip(weapon)
                except Exception:
                    logger.exception("retaliation equip failed")
        except Exception:
            logger.exception("retaliation status fetch failed")

        try:
            resp = await timed_op(
                controller.bridge.attack(str(attacker_id)),
                controller.bridge.attack_stop,
            )
        except Exception:
            logger.exception("retaliation attack failed")
            return

    if resp is None:
        # Budget exhausted — timed_op already issued /attack/stop. Return
        # normally (not raising) so resumes_on_complete wakes Claude to decide
        # whether to re-engage the now-distant mob.
        logger.info("retaliation timed out after %.0fs — stopped", RETALIATE_TIMEOUT_S)
        try:
            controller.slog("retaliation_timeout", attacker_id=attacker_id, attacker_kind=attacker_kind)
        except Exception:
            pass
        return
    if resp.status == "error":
        # Bridge non-2xx responses don't raise — surface them so silent
        # failures (entity despawned, navigation failed, matcher miss)
        # don't masquerade as successful retaliation.
        logger.warning("retaliation attack rejected: %s", resp.message)
        try:
            controller.slog("retaliation_failed", attacker_id=attacker_id, attacker_kind=attacker_kind, message=resp.message)
        except Exception:
            pass


async def _escape_to_shore(controller: "Controller") -> None:
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
        status = (await controller.bridge.get_status()).data
    except Exception:
        return
    pos = status.get("position") or {}
    py = pos.get("y")
    if py is None:
        return

    try:
        resp = await controller.bridge.get_nearby_blocks(radius=SHORE_SEARCH_RADIUS)
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
            await controller.bridge.goto(float(x), float(y + 1), float(z))
        except Exception:
            logger.exception("escape goto failed")
        return


async def entered_lava_handler(controller: "Controller", data: dict) -> None:
    """Find shore and walk there. Preempt has already fired (preempts=True)."""
    await _escape_to_shore(controller)


async def started_drowning_handler(controller: "Controller", data: dict) -> None:
    """Surface, then find shore and walk there. Preempt has already fired.

    The /surface call is a workaround for a Baritone limitation: from a
    fully-submerged start position, `#goto` instantly fails (PathNode map
    size: 1) and the bot floats in place until our stall detection bails.
    Holding forward+jump+sprint via vanilla input keys gets the player to
    the surface, after which Baritone can path normally to the shore tile
    selected below.
    """
    try:
        await controller.bridge.surface()
    except Exception:
        logger.exception("surface failed during drowning escape")
    await _escape_to_shore(controller)


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
      * hostile_nearby: informational only. No preempt, no resume — the
        dispatcher records the fire into `recent` before running the
        (no-op) handler, so it surfaces in the next gameState the agent
        reads without ever waking or interrupting Claude on its own. A
        short cooldown coalesces a swarm entering range together so the
        burst doesn't flush the rest of the recent buffer.
    """
    registry.register(ReflexHandler(
        event_type="damage_taken",
        handle=damage_taken_handler,
        preempts=False,
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
    registry.register(ReflexHandler(
        event_type="hostile_nearby",
        handle=stub_handler,
        # Pure awareness signal: never preempt the current action and never
        # resume/wake Claude. The dispatcher appends every fire to `recent`
        # before invoking the handler, so a no-op handler is enough to make
        # "a creeper wandered into range" show up in the next gameState the
        # agent naturally reads. cooldown coalesces a night-time swarm so a
        # dozen mobs entering at once don't evict everything else from the
        # 10-slot recent buffer.
        preempts=False,
        cooldown_s=3.0,
        resumes_on_complete=False,
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
    if et == "hostile_nearby":
        kind = data.get("kind")
        dist = data.get("distance")
        bits = []
        if kind:
            bits.append(str(kind))
        if dist is not None:
            try:
                bits.append(f"{float(dist):.0f} blocks")
            except (TypeError, ValueError):
                pass
        return f" ({', '.join(bits)})" if bits else ""
    return ""
