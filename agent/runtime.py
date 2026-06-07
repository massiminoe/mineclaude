"""The headless runtime — the body the MCP server drives.

Owns the bridge handle, the primitive namespace, the single-flight executor +
action queue, the reflex registry, the flushable event buffer, and a small
event bus the monitor subscribes to. It also implements the `Controller` seam
the reflex layer reaches its host through (`reflexes.py` depends on that
protocol type-only, not on Runtime). `agent/mcp_server.py` exposes Runtime's
MCP-facing methods (execute / get_state / screenshot / handlers / wait_for_event)
as tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine, Iterable, Protocol

from agent.action_queue import Action, ActionQueue
from agent.bridge import BridgeClient
from agent.gamestate import build_game_state
from agent.models import Event, ExecuteResult, GameState, HandlerInfo, Screenshot
from agent.primitives import make_primitives
from agent.reflexes import (
    REFLEX_EVENT_TYPES,
    ReflexHandler,
    ReflexRegistry,
    register_default_handlers,
)
from agent.sandbox import _validate_ast, execute

logger = logging.getLogger(__name__)

# Capacity of the flushable event buffer get_state/wait_for_event read from.
# Large enough to hold a busy span between two get_state(flush=True) drains
# without dropping; an overflow flips events_truncated so a silent drop can't
# masquerade as a quiet world.
EVENT_BUFFER_MAXLEN = 200

# The hazard event types that react via the ReflexRegistry and surface in
# `reflexes_recent`. Every OTHER event type (chat, death, respawn, and the
# mod's world-mutation log) is recorded into the flushable event buffer.
HAZARD_EVENT_TYPES = frozenset(REFLEX_EVENT_TYPES)

# Built-in reaction policy for the non-hazard event types, reported by
# get_handler when no handler has been authored over the top. death preempts
# the slot (record + stop); chat / respawn are record-only.
_DEFAULT_EVENT_POLICY = {
    "death": True,
    "chat": False,
    "respawn": False,
}


def _normalize_mod_event(raw: dict) -> dict[str, Any]:
    """Reshape a mod EventLog entry (block_broken/placed, entity_attacked) into
    the {type, data, ts} buffer shape. The mod ships epoch milliseconds in
    `ts_ms`; everything else becomes the event's data."""
    ts_ms = raw.get("ts_ms")
    ts = (ts_ms / 1000.0) if isinstance(ts_ms, (int, float)) else 0.0
    data = {k: v for k, v in raw.items() if k not in ("type", "ts_ms")}
    return {"type": raw.get("type"), "data": data, "ts": ts}


class Controller(Protocol):
    """What a reflex handler / the reflex registry needs from its host.

    `Runtime` satisfies this structurally. `resume()` signals a reflex handler
    finished recovering; Runtime records it as a notable event the next
    `get_state` / `wait_for_event` surfaces.
    """

    bridge: BridgeClient

    async def preempt(self) -> None:
        """Acquire/clear the action slot: purge the queue + stop the bridge.

        Must NOT cancel the handler task that calls it (handlers run on their
        own task), so a conditional-interrupt handler survives its own call.
        """
        ...

    def resume(self, event_type: str) -> None:
        """Signal that a reflex handler finished and the host should react."""
        ...

    def slog(self, event: str, **data: Any) -> None:
        """Structured session-log entry."""
        ...

    async def emit_event(self, event: str, data: Any = None) -> None:
        """Emit on the monitor/event bus (e.g. ``reflex:fired``)."""
        ...


PreemptHook = Callable[[], Coroutine[Any, Any, None]]


class Runtime:
    """The headless body: bridge + primitives + single-flight executor +
    reflex registry + flushable event buffer + a small event bus. Implements
    Controller for the reflexes, and exposes the MCP-facing surface.

    `slog` is an optional structured-logging hook (e.g. a SessionLogger.emit)
    carrying the run timeline — execute_start/execute_done (with the code +
    outcome), inbound `event`s, handler_set, and per-step subactions. When None
    those traces are dropped.
    """

    def __init__(
        self,
        bridge: BridgeClient,
        *,
        slog: Callable[..., None] | None = None,
    ) -> None:
        self.bridge = bridge
        self._slog_cb = slog
        self._callbacks: dict[str, list[Callable[[str, Any], Coroutine[Any, Any, None]]]] = {}
        self._preempt_hooks: list[PreemptHook] = []

        # Flushable event buffer (chat / death / respawn / world-mutations),
        # the waiters wait_for_event parks on, and the truncation flag. Distinct
        # from reflexes.recent, which holds the rolling hazard-reflex fires.
        self._events: deque[Event] = deque(maxlen=EVENT_BUFFER_MAXLEN)
        self._events_truncated = False
        self._event_waiters: list[tuple[frozenset[str] | None, asyncio.Future]] = []
        # event_type -> source code for handlers installed via set_handler.
        # Lets get_handler report source="authored" + the body; absence means
        # the registry handler (if any) is a native default.
        self._authored_handlers: dict[str, str] = {}
        # Single-flight guard: execute() rejects with status="busy" while an
        # action is already in flight. interrupt() is always out-of-band.
        self._executing = False

        self.queue = ActionQueue()
        self.primitives = make_primitives(bridge, on_subaction=self._on_subaction)
        self.reflexes = ReflexRegistry(self)
        register_default_handlers(self.reflexes)

        self.queue.set_executor(self._execute_action)
        self.queue.on("action:started", self._on_action_started)
        self.queue.on("action:completed", self._on_action_completed)
        # bridge.stop halts Baritone before the worker task is cancelled —
        # otherwise an in-flight `#goto` keeps walking after preemption.
        self.queue.set_pre_interrupt(self._pre_interrupt_stop_bridge)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.queue.start()

    # --- event bus (the monitor subscribes via .on) -----------------------

    def on(self, event: str, callback: Callable[[str, Any], Coroutine[Any, Any, None]]) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    async def _emit(self, event: str, data: Any = None) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                await cb(event, data)
            except Exception:
                pass

    # --- Controller impl ---------------------------------------------------

    def add_preempt_hook(self, hook: PreemptHook) -> None:
        """Register a coroutine run at the front of preempt() — before the
        queue/bridge are halted. A general extension point for cancelling
        out-of-band work that must stop before the slot is cleared."""
        self._preempt_hooks.append(hook)

    async def preempt(self) -> None:
        """Acquire/clear the action slot. Runs the preempt-hooks first, then
        interrupts the queue — which also halts Baritone + any /attack loop via
        the pre-interrupt hook.

        Runs the hooks and the queue interrupt independently of each other's
        failures, and does NOT touch handler tasks, so a handler that calls
        interrupt() survives its own call."""
        for hook in self._preempt_hooks:
            try:
                await hook()
            except Exception:
                logger.exception("preempt hook failed")
        try:
            await self.queue.interrupt()
        except Exception:
            logger.exception("queue interrupt failed during preempt")

    async def interrupt(self) -> None:
        """Public name for preempt() — the out-of-band 'stop everything' verb."""
        await self.preempt()

    def resume(self, event_type: str) -> None:
        # Surface "a reflex finished recovering" as an event, so a get_state /
        # wait_for_event consumer can react to the recovery completing rather
        # than to the hazard fire alone.
        self._record_event("reflex_done", {"event_type": event_type})

    def slog(self, event: str, **data: Any) -> None:
        if self._slog_cb is not None:
            self._slog_cb(event, **data)

    async def emit_event(self, event: str, data: Any = None) -> None:
        await self._emit(event, data)

    # --- executor + sub-action tracing ------------------------------------

    async def _execute_action(self, code: str) -> str:
        """Execute action code in the sandbox. Injected as the queue executor.

        SandboxError (AST violation or wrapped runtime error) propagates so the
        queue marks the action FAILED — execute() maps that to status="failed".
        """
        return await execute(code, self.primitives)

    async def _on_subaction(
        self, sub_id: str, name: str, args: dict | None, status: str, **kwargs: Any
    ) -> None:
        """Callback from instrumented primitives — slog the step, then forward
        to the queue so the running action records its sub-action timeline."""
        self.slog(
            "subaction",
            id=sub_id,
            name=name,
            args=args,
            status=status,
            result=kwargs.get("result"),
            error=kwargs.get("error"),
        )
        await self.queue.record_subaction(sub_id, name, args, status, **kwargs)

    async def _on_action_started(self, event: str, action, *_extra) -> None:
        logger.info(f"Action {action.id} STARTED: {action.code[:200]}")

    async def _on_action_completed(self, event: str, action, *_extra) -> None:
        elapsed = (action.finished_at - action.started_at) if action.started_at and action.finished_at else 0
        if action.status.value == "completed":
            logger.info(f"Action {action.id} COMPLETED ({elapsed:.1f}s): {action.result or 'no output'}")
        else:
            logger.warning(f"Action {action.id} {action.status.value.upper()} ({elapsed:.1f}s): {action.error or 'no error'}")

    # --- pre-interrupt: halt bridge-side machinery ------------------------

    async def _pre_interrupt_stop_bridge(self) -> None:
        """Halt Baritone *and* any in-flight /attack loop before worker
        cancellation. Both are fire-and-forget bridge-side effects — preemption
        must reach into bridge state, not just local awaiters, or a preempted
        reflex keeps swinging/walking in the background."""
        try:
            await self.bridge.stop()
        except Exception:
            logger.exception("bridge.stop in preempt failed")
        try:
            await self.bridge.attack_stop()
        except Exception:
            logger.exception("bridge.attack_stop in preempt failed")

    # ======================================================================
    # MCP-facing surface
    #
    # The methods an MCP server (P4) maps tools onto. They return the typed
    # holders in agent/models.py. `say()` is a primitive inside execute(), not
    # here — talking is something action code does, not a top-level verb.
    # ======================================================================

    # --- execute ----------------------------------------------------------

    async def execute(self, code: str, timeout: float = 300.0) -> ExecuteResult:
        """Run `code` through the single-flight slot, blocking until it ends.

        Rejects with status="busy" if an action is already in flight — the slot
        is single-flight by contract; concurrency comes from an out-of-band
        watcher that calls interrupt(), not from overlapping executes.
        """
        if self._executing:
            self.slog("execute_rejected", reason="busy", code=code)
            return ExecuteResult(
                status="busy",
                action_id="",
                duration_s=0.0,
                error="An action is already running; interrupt() it or wait.",
            )
        self._executing = True
        try:
            action = await self.queue.enqueue(code, timeout=timeout)
            self.slog("execute_start", action_id=action.id, code=code, timeout=timeout)
            await self.queue.drain()
            result = self._action_to_result(action)
            self.slog(
                "execute_done",
                action_id=action.id,
                status=result.status,
                duration_s=round(result.duration_s, 3),
                result=result.result,
                error=result.error,
            )
            return result
        finally:
            self._executing = False

    @staticmethod
    def _action_to_result(action: Action) -> ExecuteResult:
        """Map a finished queue Action onto an ExecuteResult. Distinguishes a
        timeout (worker's wait_for tripped) from a generic code failure by the
        error string the queue stamps."""
        duration = (
            action.finished_at - action.started_at
            if action.started_at and action.finished_at
            else 0.0
        )
        st = action.status.value
        if st == "completed":
            return ExecuteResult("completed", action.id, duration, result=action.result)
        if st == "cancelled":
            return ExecuteResult("cancelled", action.id, duration, error=action.error)
        if st == "failed":
            status = "timeout" if (action.error and "timed out" in action.error) else "failed"
            return ExecuteResult(status, action.id, duration, error=action.error)
        # PENDING/RUNNING shouldn't survive drain() — surface it rather than lie.
        return ExecuteResult("failed", action.id, duration, error=f"unexpected status: {st}")

    # --- get_state --------------------------------------------------------

    async def get_state(self, flush: bool = True) -> GameState:
        """Structured world snapshot. With flush=True (default) the flushable
        event buffer AND the mod's world-mutation log are drained into
        `events`; with flush=False both are peeked non-destructively so a
        read-only poll doesn't consume what a later drain should see."""
        resp = await self.bridge.get_status(include_events=flush)
        status = dict(resp.data)
        mod_events = status.pop("events", []) if flush else []
        if flush:
            events = self._drain_events()
            truncated = self._events_truncated
            self._events_truncated = False
            events.extend(_normalize_mod_event(e) for e in mod_events)
            events.sort(key=lambda e: e.get("ts", 0.0))
        else:
            events = [{"type": e.type, "data": e.data, "ts": e.ts} for e in self._events]
            truncated = self._events_truncated
        return build_game_state(
            status,
            self.queue.status(),
            recent_reflexes=list(self.reflexes.recent),
            events=events,
            events_truncated=truncated,
        )

    # --- screenshot -------------------------------------------------------

    async def screenshot(
        self,
        *,
        yaw: float | None = None,
        pitch: float | None = None,
        look_at: tuple[float, float, float] | None = None,
    ) -> Screenshot:
        """Capture the first-person view. Aim with yaw/pitch OR look_at (a world
        coord to point the eye at), never both. Raises on a bridge failure."""
        resp = await self.bridge.screenshot(yaw=yaw, pitch=pitch, look_at=look_at)
        if resp.status != "success":
            raise RuntimeError(f"screenshot failed: {resp.message}")
        d = resp.data
        return Screenshot(
            image_base64=d["image"],
            format=d.get("format", "jpeg"),
            width=d.get("width"),
            height=d.get("height"),
            yaw=yaw,
            pitch=pitch,
        )

    # --- handlers ---------------------------------------------------------

    def get_handler(self, event_type: str) -> HandlerInfo:
        """Report the current reaction policy for an event type. Reads the
        registry for hazard + authored handlers, falling back to the built-in
        policy for the record-only/preempt defaults (chat/death/respawn)."""
        handler = self.reflexes.get(event_type)
        if handler is not None:
            code = self._authored_handlers.get(event_type)
            source = "authored" if code is not None else "default"
            return HandlerInfo(event_type, source, handler.preempts, handler.cooldown_s, code)
        return HandlerInfo(
            event_type,
            "default",
            _DEFAULT_EVENT_POLICY.get(event_type, False),
            0.0,
            None,
        )

    def set_handler(
        self,
        event_type: str,
        code: str,
        *,
        preempts: bool = False,
        cooldown_s: float = 0.0,
    ) -> HandlerInfo:
        """Install an authored reaction body for an event type (overriding any
        default). The body is AST-validated up front, then runs in the sandbox
        with the primitive namespace plus `data` (the event payload) and
        `interrupt` (acquire the slot mid-body). preempts=True acquires the slot
        before the body runs; otherwise call interrupt() yourself when the
        payload warrants it. Reads and say() never need the slot."""
        _validate_ast(code)

        async def authored(controller: "Controller", data: dict) -> None:
            namespace = dict(self.primitives)
            namespace["data"] = data
            namespace["interrupt"] = self.interrupt
            await execute(code, namespace)

        self.reflexes.register(ReflexHandler(
            event_type=event_type,
            handle=authored,
            preempts=preempts,
            cooldown_s=cooldown_s,
            resumes_on_complete=False,
        ))
        self._authored_handlers[event_type] = code
        self.slog("handler_set", event_type=event_type, preempts=preempts, cooldown_s=cooldown_s)
        return self.get_handler(event_type)

    # --- events: subscription, recording, waiting -------------------------

    async def run_events(self) -> None:
        """Subscribe to the bridge event stream and route forever (the WS read
        loop reconnects on its own). The co-hosted MCP launcher (P4) awaits
        this; the transitional Agent still owns its own subscription until the
        brain is deleted, so this stays dormant under the brain."""
        await self.bridge.events(self._handle_event)

    async def _handle_event(self, event: dict) -> None:
        """Canonical event router. Hazard types react via the ReflexRegistry
        (surfaced in reflexes_recent); every other type is recorded into the
        flushable buffer. death additionally preempts the slot; an authored
        handler for any type also dispatches."""
        event_type = event.get("type")
        if event_type is None:
            return
        data = event.get("data") or {}
        self.slog("event", type=event_type, data=data)
        if event_type not in HAZARD_EVENT_TYPES:
            self._record_event(event_type, data)
        if event_type == "death":
            await self.preempt()
        if event_type in self.reflexes.known_types():
            await self.reflexes.dispatch(event_type, data)

    def _record_event(self, event_type: str, data: dict) -> Event:
        """Append to the flushable buffer (flagging truncation on overflow) and
        resolve any wait_for_event waiter the new event matches."""
        if len(self._events) == self._events.maxlen:
            self._events_truncated = True
        ev = Event(type=event_type, data=dict(data or {}), ts=time.time())
        self._events.append(ev)
        self._resolve_waiters(ev)
        return ev

    def _resolve_waiters(self, ev: Event) -> None:
        if not self._event_waiters:
            return
        remaining: list[tuple[frozenset[str] | None, asyncio.Future]] = []
        for types, fut in self._event_waiters:
            if fut.done():
                continue
            if types is None or ev.type in types:
                fut.set_result(ev)
            else:
                remaining.append((types, fut))
        self._event_waiters = remaining

    def _drain_events(self) -> list[dict[str, Any]]:
        out = [{"type": e.type, "data": e.data, "ts": e.ts} for e in self._events]
        self._events.clear()
        return out

    async def wait_for_event(
        self,
        types: Iterable[str] | None = None,
        timeout: float = 30.0,
    ) -> Event | None:
        """Block until the next event whose type is in `types` (any type if
        None) is recorded, or `timeout` elapses. Returns the Event or None.
        Future-only: it waits for the next matching record, not for something
        already buffered (drain via get_state first, then watch for new)."""
        type_set = frozenset(types) if types else None
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._event_waiters.append((type_set, fut))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._event_waiters = [(t, f) for (t, f) in self._event_waiters if f is not fut]
