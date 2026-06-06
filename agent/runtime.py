"""The headless runtime — the body the MCP server (and, transitionally, the
Claude-loop Agent) drives.

This module will grow to own the bridge handle, primitive namespace, the
single-flight executor, the reflex registry, and the event buffer (see
docs/superpowers/specs/2026-06-06-mcp-runtime-extraction.md). For now it
defines the `Controller` seam: the minimal surface a reflex handler needs from
its host. `reflexes.py` depends on this protocol (type-only) instead of on the
`Agent` class, so the body can be lifted out from under the brain.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Protocol

from agent.action_queue import ActionQueue
from agent.bridge import BridgeClient
from agent.primitives import make_primitives
from agent.reflexes import ReflexRegistry, register_default_handlers
from agent.sandbox import SandboxError, execute

logger = logging.getLogger(__name__)


class Controller(Protocol):
    """What a reflex handler / the reflex registry needs from its host.

    Both the transitional `Agent` (via thin wrappers) and the future `Runtime`
    satisfy this structurally. `resume()` replaces the brain-specific
    `_stage_resume`: under `Agent` it wakes the Claude loop; under `Runtime` it
    appends a notable event to the buffer for the next `get_state`.
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
    reflex registry + event bus. Implements Controller for the reflexes.

    Transitional (P2): the Claude-loop Agent owns a Runtime and delegates to
    it. The Agent injects `slog` (so sub-action logging reaches its per-turn
    session logger) and `on_resume` (so a completed reflex wakes the Claude
    loop), and registers a preempt-hook that cancels its in-flight Claude turn.
    P3 moves event subscription + the event buffer in here; P5 deletes the Agent
    and these injection seams, and `resume()` becomes a buffer append.
    """

    def __init__(
        self,
        bridge: BridgeClient,
        *,
        slog: Callable[..., None] | None = None,
        on_resume: Callable[[str], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self._slog_cb = slog
        self._on_resume = on_resume
        self._callbacks: dict[str, list[Callable[[str, Any], Coroutine[Any, Any, None]]]] = {}
        self._preempt_hooks: list[PreemptHook] = []

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

    # --- event bus (monitor + brain subscribe via .on) --------------------

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
        queue/bridge are halted. The Agent uses this to cancel its Claude turn."""
        self._preempt_hooks.append(hook)

    async def preempt(self) -> None:
        """Acquire/clear the action slot. Runs the preempt-hooks (e.g. the
        brain's cancel-Claude-turn) first, then interrupts the queue — which
        also halts Baritone + any /attack loop via the pre-interrupt hook.

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
        if self._on_resume is not None:
            self._on_resume(event_type)
        # P3: also append a notable event to the buffer so get_state surfaces it.

    def slog(self, event: str, **data: Any) -> None:
        if self._slog_cb is not None:
            self._slog_cb(event, **data)

    async def emit_event(self, event: str, data: Any = None) -> None:
        await self._emit(event, data)

    # --- executor + sub-action tracing ------------------------------------

    async def _execute_action(self, code: str) -> str:
        """Execute action code in the sandbox. Injected as the queue executor."""
        try:
            return await execute(code, self.primitives)
        except SandboxError as e:
            return f"Sandbox error: {e}"

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
