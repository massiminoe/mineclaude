"""Tests for the action queue."""

import asyncio

import pytest

from mineclaude import action_queue
from mineclaude.action_queue import ActionQueue, ActionStatus


async def simple_executor(code: str) -> str:
    """Test executor that just returns the code."""
    await asyncio.sleep(0.01)
    return f"executed: {code}"


async def slow_executor(code: str) -> str:
    await asyncio.sleep(10)
    return "done"


async def failing_executor(code: str) -> str:
    raise RuntimeError("boom")


@pytest.fixture
async def queue():
    q = ActionQueue(timeout=5.0)
    q.set_executor(simple_executor)
    q.start()
    yield q
    await q.stop()


@pytest.mark.asyncio
async def test_enqueue_and_drain(queue):
    action = await queue.enqueue("test code")
    assert action.status == ActionStatus.PENDING

    await queue.drain()
    status = queue.status()
    assert len(status["recent"]) == 1
    assert status["recent"][0]["status"] == "completed"
    assert "executed: test code" in status["recent"][0]["result"]


@pytest.mark.asyncio
async def test_multiple_actions_fifo(queue):
    results = []

    async def tracking_executor(code: str) -> str:
        results.append(code)
        await asyncio.sleep(0.01)
        return code

    queue.set_executor(tracking_executor)

    await queue.enqueue("first")
    await queue.enqueue("second")
    await queue.enqueue("third")
    await queue.drain()

    assert results == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_cancel_pending(queue):
    async def slow(code: str) -> str:
        await asyncio.sleep(1)
        return code

    queue.set_executor(slow)

    a1 = await queue.enqueue("first")
    a2 = await queue.enqueue("second")
    a3 = await queue.enqueue("third")

    # Wait for first to start running
    await asyncio.sleep(0.05)

    # Cancel second (pending)
    found = await queue.cancel(a2.id)
    assert found

    status = queue.status()
    assert len(status["pending"]) == 1  # only third remains


@pytest.mark.asyncio
async def test_clear(queue):
    async def slow(code: str) -> str:
        await asyncio.sleep(1)
        return code

    queue.set_executor(slow)

    await queue.enqueue("a")
    await queue.enqueue("b")
    await queue.enqueue("c")
    await asyncio.sleep(0.05)

    count = await queue.clear()
    assert count == 2  # b and c (a is running)


@pytest.mark.asyncio
async def test_interrupt(queue):
    async def slow(code: str) -> str:
        await asyncio.sleep(10)
        return code

    queue.set_executor(slow)

    await queue.enqueue("running")
    await queue.enqueue("pending")
    await asyncio.sleep(0.05)

    await queue.interrupt()

    status = queue.status()
    assert status["running"] is None
    assert len(status["pending"]) == 0
    # Both should be in recent as cancelled
    cancelled = [r for r in status["recent"] if r["status"] == "cancelled"]
    assert len(cancelled) == 2


@pytest.mark.asyncio
async def test_interrupt_abandons_uncancellable_worker(monkeypatch):
    """A running action whose cancellation won't unwind (e.g. an httpx request
    mid-flight) must not hang interrupt() — it gets abandoned and a fresh
    worker takes over. Regression for the +780s preempt-path deadlock."""
    monkeypatch.setattr(action_queue, "_WORKER_CANCEL_TIMEOUT_S", 0.2)
    release = asyncio.Event()

    async def stubborn(code: str) -> str:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Simulate an await that ignores cancellation until much later.
            await release.wait()
        return code

    q = ActionQueue(timeout=30.0)
    q.set_executor(stubborn)
    q.start()
    await q.enqueue("running")
    await asyncio.sleep(0.05)  # let the worker pick it up

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(q.interrupt(), timeout=2.0)  # must NOT hang on 60s
    assert loop.time() - t0 < 1.5  # bounded by _WORKER_CANCEL_TIMEOUT_S

    # The fresh worker is functional after abandoning the stuck one.
    q.set_executor(simple_executor)
    await q.enqueue("after")
    await asyncio.wait_for(q.drain(), timeout=2.0)
    assert any(r["status"] == "completed" for r in q.status()["recent"])

    release.set()  # let the abandoned task unwind cleanly
    await asyncio.sleep(0.05)
    await q.stop()


@pytest.mark.asyncio
async def test_interrupt_bounds_hanging_pre_interrupt(monkeypatch):
    """A pre_interrupt hook (bridge stop-calls) that never returns must not
    hang interrupt() — it's abandoned after the deadline."""
    monkeypatch.setattr(action_queue, "_PRE_INTERRUPT_TIMEOUT_S", 0.2)
    q = ActionQueue(timeout=30.0)
    q.set_executor(simple_executor)
    q.start()

    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.sleep(60)

    q.set_pre_interrupt(hang)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(q.interrupt(), timeout=2.0)
    assert started.is_set()
    assert loop.time() - t0 < 1.5  # bounded by _PRE_INTERRUPT_TIMEOUT_S

    await q.stop()


@pytest.mark.asyncio
async def test_failed_action():
    q = ActionQueue()
    q.set_executor(failing_executor)
    q.start()

    await q.enqueue("bad code")
    await q.drain()

    status = q.status()
    assert status["recent"][0]["status"] == "failed"
    assert "boom" in status["recent"][0]["error"]

    await q.stop()


@pytest.mark.asyncio
async def test_timeout():
    q = ActionQueue(timeout=0.1)
    q.set_executor(slow_executor)
    q.start()

    await q.enqueue("slow")
    await q.drain()

    status = q.status()
    assert status["recent"][0]["status"] == "failed"
    assert "timed out" in status["recent"][0]["error"].lower()

    await q.stop()


@pytest.mark.asyncio
async def test_event_callbacks(queue):
    events = []

    async def on_event(event_name, action):
        events.append(event_name)

    queue.on("action:enqueued", on_event)
    queue.on("action:started", on_event)
    queue.on("action:completed", on_event)
    queue.on("action:drained", on_event)

    await queue.enqueue("test")
    await queue.drain()

    assert "action:enqueued" in events
    assert "action:started" in events
    assert "action:completed" in events
    assert "action:drained" in events


@pytest.mark.asyncio
async def test_status_snapshot(queue):
    status = queue.status()
    assert status["running"] is None
    assert status["pending"] == []
    assert status["recent"] == []
