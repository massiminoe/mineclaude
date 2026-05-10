"""Async action queue for sequential execution of sandbox actions."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine


class ActionStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Action:
    id: str
    code: str
    status: ActionStatus = ActionStatus.PENDING
    enqueued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None
    timeout: float = 300.0
    subactions: list[dict[str, Any]] = field(default_factory=list)


# Type for the executor callable: (code: str) -> result string
Executor = Callable[[str], Coroutine[Any, Any, str]]

# Type for event callbacks: (event, action, *extra)
EventCallback = Callable[..., Coroutine[Any, Any, None]]


class ActionQueue:
    def __init__(self, timeout: float = 300.0):
        self._queue: asyncio.Queue[Action] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._executor: Executor | None = None
        self._running_action: Action | None = None
        self._recent: deque[Action] = deque(maxlen=10)
        self._timeout = timeout
        self._callbacks: dict[str, list[EventCallback]] = {}
        self._drain_event: asyncio.Event = asyncio.Event()
        self._drain_event.set()  # starts drained (empty)
        self._pre_interrupt: Callable[[], Coroutine[Any, Any, None]] | None = None

    def set_executor(self, executor: Executor) -> None:
        self._executor = executor

    def set_pre_interrupt(self, fn: Callable[[], Coroutine[Any, Any, None]] | None) -> None:
        """Register a coroutine to run *before* interrupt() cancels the worker.

        Used to halt out-of-band machinery (e.g. Baritone) that wouldn't
        otherwise cooperate with asyncio task cancellation. Failures are
        swallowed so a flaky pre-hook can't block interrupt itself.
        """
        self._pre_interrupt = fn

    def on(self, event: str, callback: EventCallback) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    async def _emit(self, event: str, action: Action, *extra: Any) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                await cb(event, action, *extra)
            except Exception:
                pass

    async def record_subaction(
        self,
        sub_id: str,
        name: str,
        args: dict[str, Any] | None,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """Record a sub-action event on the currently running action."""
        action = self._running_action
        if action is None:
            return
        if status == "started":
            sub: dict[str, Any] = {
                "id": sub_id,
                "name": name,
                "args": args,
                "status": "started",
                "started_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            action.subactions.append(sub)
            await self._emit("subaction:started", action, sub)
        else:
            # Update existing sub-action
            for s in action.subactions:
                if s["id"] == sub_id:
                    s["status"] = status
                    s["finished_at"] = time.time()
                    s["result"] = _truncate(result) if result is not None else None
                    s["error"] = error
                    await self._emit("subaction:completed", action, s)
                    break

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(self, code: str, timeout: float | None = None) -> Action:
        action = Action(
            id=uuid.uuid4().hex[:8],
            code=code,
            timeout=timeout or self._timeout,
        )
        self._drain_event.clear()
        await self._queue.put(action)
        await self._emit("action:enqueued", action)
        return action

    async def cancel(self, action_id: str) -> bool:
        # Can only cancel pending actions (not running)
        new_queue: asyncio.Queue[Action] = asyncio.Queue()
        found = False
        while not self._queue.empty():
            try:
                action = self._queue.get_nowait()
                if action.id == action_id:
                    action.status = ActionStatus.CANCELLED
                    action.finished_at = time.time()
                    self._recent.append(action)
                    found = True
                else:
                    new_queue.put_nowait(action)
            except asyncio.QueueEmpty:
                break
        self._queue = new_queue
        if self._queue.empty() and self._running_action is None:
            self._drain_event.set()
        return found

    async def clear(self) -> int:
        """Cancel all pending actions. Returns count cancelled."""
        count = 0
        while not self._queue.empty():
            try:
                action = self._queue.get_nowait()
                action.status = ActionStatus.CANCELLED
                action.finished_at = time.time()
                self._recent.append(action)
                count += 1
            except asyncio.QueueEmpty:
                break
        if self._running_action is None:
            self._drain_event.set()
        return count

    async def interrupt(self) -> None:
        """Cancel running action + clear all pending."""
        if self._pre_interrupt is not None:
            try:
                await self._pre_interrupt()
            except Exception:
                pass
        await self.clear()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = asyncio.create_task(self._worker())
            self._drain_event.set()

    async def drain(self) -> None:
        """Wait for all pending and running actions to complete."""
        await self._drain_event.wait()

    def is_running(self) -> bool:
        """Return True while the queue is executing an action."""
        return self._running_action is not None

    def status(self) -> dict[str, Any]:
        pending = []
        # Snapshot pending queue without consuming
        temp = []
        while not self._queue.empty():
            try:
                a = self._queue.get_nowait()
                temp.append(a)
                pending.append(_action_summary(a))
            except asyncio.QueueEmpty:
                break
        for a in temp:
            self._queue.put_nowait(a)

        return {
            "running": _action_summary(self._running_action) if self._running_action else None,
            "pending": pending,
            "recent": [_action_summary(a) for a in self._recent],
        }

    async def _worker(self) -> None:
        while True:
            action = await self._queue.get()
            self._running_action = action
            action.status = ActionStatus.RUNNING
            action.started_at = time.time()
            await self._emit("action:started", action)

            try:
                if self._executor is None:
                    raise RuntimeError("No executor set on ActionQueue")
                result = await asyncio.wait_for(
                    self._executor(action.code),
                    timeout=action.timeout,
                )
                action.status = ActionStatus.COMPLETED
                action.result = result
            except asyncio.TimeoutError:
                action.status = ActionStatus.FAILED
                action.error = f"Action timed out after {action.timeout}s"
            except asyncio.CancelledError:
                action.status = ActionStatus.CANCELLED
                raise
            except Exception as e:
                action.status = ActionStatus.FAILED
                action.error = str(e)
            finally:
                action.finished_at = time.time()
                self._running_action = None
                self._recent.append(action)

            await self._emit("action:completed", action)

            if self._queue.empty():
                self._drain_event.set()
                await self._emit("action:drained", action)


def _truncate(value: Any, max_len: int = 200) -> str | None:
    """Truncate a result value to a reasonable display length."""
    if value is None:
        return None
    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def _action_summary(action: Action) -> dict[str, Any]:
    return {
        "id": action.id,
        "status": action.status.value,
        "code": action.code[:100],
        "enqueued_at": action.enqueued_at,
        "started_at": action.started_at,
        "finished_at": action.finished_at,
        "result": action.result,
        "error": action.error,
        "subactions": action.subactions,
    }
