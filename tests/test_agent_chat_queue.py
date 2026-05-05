"""Tests for the chat-queue + preempt + snapshot/restore behavior in Agent.

The reflex layer can preempt an in-flight Claude turn. This file exercises
the invariants that make that safe:

  * User input lands in `_pending_user_inputs`, never directly in
    `self.messages` from the events handler — so it can't insert between
    a tool_use and its matching tool_result.
  * Pending inputs survive `_preempt`, get flushed on the NEXT chat trigger.
  * Cancelling an in-flight turn truncates `self.messages` back to a
    pre-turn snapshot, preserving the user message but discarding partial
    plan/memory/gameState/assistant blocks.
  * Multiple pending inputs flushed together collapse into a single
    user-string message to keep user/assistant alternation.
  * Death uses the same `_preempt` path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.agent import Agent
from agent.bridge import MockBridgeClient


class _StubClaude:
    """Programmable stub for ClaudeClient.

    Default behavior is end_turn with a single text block. Override
    `responder` to control per-call responses, or `block_event` to make
    `send` block until set (for testing in-flight cancellation).
    """

    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self.responder = None  # callable (system, messages) -> response, or None
        self.block_event: asyncio.Event | None = None

    async def send(self, system, messages, max_tokens: int = 4096):
        self.calls.append((system, list(messages)))
        if self.block_event is not None:
            await self.block_event.wait()
        if self.responder is not None:
            return self.responder(system, messages)
        return _make_text_response("ok")

    async def close(self):
        pass


def _make_text_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[block])


def _make_agent(claude: _StubClaude | None = None) -> Agent:
    bridge = MockBridgeClient()
    return Agent(bridge=bridge, claude=claude or _StubClaude(), bot_name="Claude")


# --- _enqueue_chat ---------------------------------------------------------


def test_enqueue_chat_buffers_and_sets_trigger():
    agent = _make_agent()
    assert not agent._chat_trigger.is_set()
    agent._enqueue_chat({"username": "Steve", "message": "hi"})
    assert agent._chat_trigger.is_set()
    assert len(agent._pending_user_inputs) == 1
    pend = agent._pending_user_inputs[0]
    assert pend["role"] == "user"
    assert pend["content"] == "Steve: hi"
    assert pend["_username"] == "Steve"


def test_enqueue_chat_ignores_self_echo():
    agent = _make_agent()
    agent._enqueue_chat({"username": "Claude", "message": "hi"})  # bot_name match
    agent._enqueue_chat({"username": "claude", "message": "hi"})  # case-insensitive
    assert agent._pending_user_inputs == []
    assert not agent._chat_trigger.is_set()


def test_enqueue_chat_ignores_empty_username():
    agent = _make_agent()
    agent._enqueue_chat({"username": "", "message": "hi"})
    assert agent._pending_user_inputs == []


# --- _stage_resume ---------------------------------------------------------


def test_stage_resume_appends_pending_and_sets_trigger():
    """Reflex resume looks the same to the chat worker as a player chat:
    a row in pending + the trigger flipped."""
    agent = _make_agent()
    assert not agent._chat_trigger.is_set()
    agent._stage_resume("tool_broke")
    assert agent._chat_trigger.is_set()
    assert len(agent._pending_user_inputs) == 1
    pend = agent._pending_user_inputs[0]
    assert pend["role"] == "user"
    assert "tool_broke" in pend["content"]
    assert pend["_username"] == "reflex"


def test_stage_resume_coalesces_with_pending_chat_on_flush():
    """A reflex landing alongside a pending player chat produces a single
    user turn that contains both — preserves user/assistant alternation
    and lets Claude see them in one batch."""
    agent = _make_agent()
    agent._enqueue_chat({"username": "Steve", "message": "go mine"})
    agent._stage_resume("tool_broke")
    agent._flush_pending_inputs()
    assert len(agent.messages) == 1
    content = agent.messages[0]["content"]
    assert "Steve: go mine" in content
    assert "tool_broke" in content


# --- _flush_pending_inputs -------------------------------------------------


def test_flush_returns_none_when_empty():
    agent = _make_agent()
    assert agent._flush_pending_inputs() is None
    assert agent.messages == []


def test_flush_single_input_appends_one_message():
    agent = _make_agent()
    agent._enqueue_chat({"username": "Steve", "message": "hi"})
    last = agent._flush_pending_inputs()
    assert last == {"username": "Steve", "message": "Steve: hi"}
    assert agent._pending_user_inputs == []
    assert len(agent.messages) == 1
    assert agent.messages[0] == {"role": "user", "content": "Steve: hi"}


def test_flush_multiple_inputs_combines_into_single_message():
    """Multiple chats received without intervening responses must collapse
    into one user message — consecutive same-role messages would otherwise
    break user/assistant alternation."""
    agent = _make_agent()
    agent._enqueue_chat({"username": "Steve", "message": "first"})
    agent._enqueue_chat({"username": "Steve", "message": "second"})
    agent._enqueue_chat({"username": "Alice", "message": "third"})
    last = agent._flush_pending_inputs()
    assert last["username"] == "Alice"  # most recent
    assert len(agent.messages) == 1
    assert agent.messages[0]["role"] == "user"
    assert agent.messages[0]["content"] == "Steve: first\nSteve: second\nAlice: third"
    assert agent._pending_user_inputs == []


def test_flush_coalesces_with_trailing_user_string():
    """If history already ends with a user-string message (defensive case),
    the new flush appends to it rather than adding a second consecutive
    user message."""
    agent = _make_agent()
    agent.messages.append({"role": "user", "content": "Steve: existing"})
    agent._enqueue_chat({"username": "Steve", "message": "follow-up"})
    agent._flush_pending_inputs()
    assert len(agent.messages) == 1
    assert agent.messages[0]["content"] == "Steve: existing\nSteve: follow-up"


def test_flush_does_not_coalesce_with_trailing_user_list():
    """A user message with list content (tool_results) is a distinct turn —
    don't merge into it."""
    agent = _make_agent()
    agent.messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
    ]})
    agent._enqueue_chat({"username": "Steve", "message": "hi"})
    agent._flush_pending_inputs()
    assert len(agent.messages) == 2
    assert agent.messages[1] == {"role": "user", "content": "Steve: hi"}


# --- _preempt --------------------------------------------------------------


async def test_preempt_clears_trigger_and_calls_queue_interrupt():
    agent = _make_agent()
    agent.queue.start()
    try:
        agent._chat_trigger.set()
        await agent._preempt()
        assert not agent._chat_trigger.is_set()
        # queue.interrupt should have replaced the worker task; it's a sign
        # of life that no exception was raised and the queue is still usable.
        await agent.queue.enqueue("ok")
    finally:
        await agent.queue.stop()


async def test_preempt_preserves_pending_user_inputs():
    """The whole point: user words survive a reflex preempt."""
    agent = _make_agent()
    agent.queue.start()
    try:
        agent._enqueue_chat({"username": "Steve", "message": "stay"})
        await agent._preempt()
        assert len(agent._pending_user_inputs) == 1
        assert agent._pending_user_inputs[0]["content"] == "Steve: stay"
    finally:
        await agent.queue.stop()


async def test_preempt_cancels_active_chat_task():
    agent = _make_agent()
    agent.queue.start()
    try:
        async def long_running():
            await asyncio.sleep(10)
        task = asyncio.create_task(long_running())
        agent._active_chat_task = task
        await agent._preempt()
        # Give the cancellation a moment to propagate.
        await asyncio.sleep(0.05)
        assert task.cancelled() or task.done()
    finally:
        await agent.queue.stop()


# --- end-to-end: chat worker + claude turn + cancel ------------------------


async def test_chat_worker_processes_pending_through_claude():
    claude = _StubClaude()
    agent = _make_agent(claude)
    agent.queue.start()
    worker = asyncio.create_task(agent._chat_worker())
    try:
        agent._enqueue_chat({"username": "Steve", "message": "hi"})
        # Wait for claude.send to be called.
        for _ in range(50):
            if claude.calls:
                break
            await asyncio.sleep(0.02)
        assert len(claude.calls) == 1
        # Worker spliced the user message into history before sending.
        sent_messages = claude.calls[0][1]
        assert any(
            m.get("role") == "user" and isinstance(m.get("content"), str) and "Steve: hi" in m["content"]
            for m in sent_messages
        )
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await agent.queue.stop()


async def test_preempt_during_claude_turn_truncates_history_but_keeps_user_msg():
    """The defining test: a reflex preempt mid-Claude-turn must leave
    `self.messages` with the user message intact and no orphaned tool_use."""
    claude = _StubClaude()
    claude.block_event = asyncio.Event()  # block claude.send until released
    agent = _make_agent(claude)
    agent.queue.start()
    worker = asyncio.create_task(agent._chat_worker())
    try:
        agent._enqueue_chat({"username": "Steve", "message": "stay"})
        # Wait until claude.send is in flight.
        for _ in range(50):
            if claude.calls:
                break
            await asyncio.sleep(0.02)
        assert claude.calls, "claude.send was never invoked"
        # At this point messages contains: user msg + plan + memory + gameState
        # synthetic pairs. The user msg is the very first.
        assert len(agent.messages) > 1
        assert agent.messages[0]["role"] == "user"
        assert agent.messages[0]["content"] == "Steve: stay"

        # Fire a preempt — simulates a reflex.
        await agent._preempt()
        # Cancellation propagates; release the block so the cancelled task
        # can finish unwinding.
        claude.block_event.set()
        # Give the worker a moment to clean up.
        await asyncio.sleep(0.05)

        # User message survives; everything added during the cancelled turn
        # (plan, memory, gameState injections) is gone.
        assert len(agent.messages) == 1
        assert agent.messages[0] == {"role": "user", "content": "Steve: stay"}
        # Pending is empty because the message was already flushed pre-snapshot.
        assert agent._pending_user_inputs == []
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await agent.queue.stop()


async def test_chat_arriving_during_turn_waits_for_next_trigger_after_preempt():
    """If chat 2 arrives while chat 1 is being processed, and a reflex
    preempts chat 1, chat 2's user message must still end up in history
    when the NEXT chat trigger fires (since the trigger from chat 2 was
    cleared by preempt)."""
    claude = _StubClaude()
    claude.block_event = asyncio.Event()
    agent = _make_agent(claude)
    agent.queue.start()
    worker = asyncio.create_task(agent._chat_worker())
    try:
        agent._enqueue_chat({"username": "Steve", "message": "first"})
        for _ in range(50):
            if claude.calls:
                break
            await asyncio.sleep(0.02)
        # Chat 2 arrives mid-turn; goes to pending.
        agent._enqueue_chat({"username": "Steve", "message": "second"})
        assert len(agent._pending_user_inputs) == 1

        # Preempt chat 1.
        await agent._preempt()
        claude.block_event.set()
        await asyncio.sleep(0.05)

        # Trigger was cleared → chat 2 is still pending, no new claude call.
        calls_before_next = len(claude.calls)
        # Pending preserved.
        assert len(agent._pending_user_inputs) == 1
        assert agent._pending_user_inputs[0]["content"] == "Steve: second"
        # History has chat 1's user msg only (chat 2 still in pending).
        assert agent.messages == [{"role": "user", "content": "Steve: first"}]

        # Now a new chat arrives — flushes BOTH pending and itself, single
        # claude call sees both.
        claude.block_event = asyncio.Event()  # don't block this one
        claude.block_event.set()
        agent._enqueue_chat({"username": "Steve", "message": "third"})
        for _ in range(50):
            if len(claude.calls) > calls_before_next:
                break
            await asyncio.sleep(0.02)
        sent = claude.calls[-1][1]
        # The user-string message in this iteration's history should contain
        # both the second and third chats combined.
        user_msgs = [m for m in sent if m.get("role") == "user" and isinstance(m.get("content"), str)]
        # Last user-string message is the freshly flushed combined one.
        last_user = user_msgs[-1]["content"]
        assert "Steve: second" in last_user
        assert "Steve: third" in last_user
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await agent.queue.stop()


    # --- ! commands --------------------------------------------------------


async def test_bang_command_does_not_buffer_or_trigger():
    """`!`-prefixed messages are commands, not chat — they never land in
    pending or set the chat trigger."""
    agent = _make_agent()
    agent._enqueue_chat({"username": "Steve", "message": "!stop"})
    # Let the spawned command task start.
    await asyncio.sleep(0)
    assert agent._pending_user_inputs == []
    assert not agent._chat_trigger.is_set()


async def test_bang_stop_cancels_active_chat_task_and_clears_pending():
    agent = _make_agent()
    agent.queue.start()
    try:
        async def long_running():
            await asyncio.sleep(10)
        task = asyncio.create_task(long_running())
        agent._active_chat_task = task
        # Pre-queued chat that should be wiped by !stop.
        agent._pending_user_inputs.append({
            "role": "user", "content": "Steve: queued", "_username": "Steve",
        })

        agent._enqueue_chat({"username": "Steve", "message": "!stop"})
        # Give the command task time to run.
        await asyncio.sleep(0.05)

        assert task.cancelled() or task.done()
        assert agent._pending_user_inputs == []
        assert not agent._chat_trigger.is_set()
    finally:
        await agent.queue.stop()


async def test_bang_stop_does_not_appear_in_messages():
    """The whole point: a command never pollutes Claude's view."""
    claude = _StubClaude()
    agent = _make_agent(claude)
    agent.queue.start()
    worker = asyncio.create_task(agent._chat_worker())
    try:
        agent._enqueue_chat({"username": "Steve", "message": "!stop"})
        await asyncio.sleep(0.05)
        assert claude.calls == []
        assert agent.messages == []
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await agent.queue.stop()


async def test_normal_chat_after_bang_stop_runs_a_turn():
    """!stop is a one-shot interrupt — no lingering paused state."""
    claude = _StubClaude()
    agent = _make_agent(claude)
    agent.queue.start()
    worker = asyncio.create_task(agent._chat_worker())
    try:
        agent._enqueue_chat({"username": "Steve", "message": "!stop"})
        await asyncio.sleep(0.05)
        agent._enqueue_chat({"username": "Steve", "message": "hi"})
        for _ in range(50):
            if claude.calls:
                break
            await asyncio.sleep(0.02)
        assert len(claude.calls) == 1
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await agent.queue.stop()


async def test_unknown_bang_command_replies_and_skips_claude():
    claude = _StubClaude()
    agent = _make_agent(claude)
    agent._enqueue_chat({"username": "Steve", "message": "!frobnicate now"})
    await asyncio.sleep(0.05)
    assert agent.messages == []
    assert agent._pending_user_inputs == []
    assert claude.calls == []
    # MockBridgeClient records sent chat in _chat_log.
    assert any("unknown command: !frobnicate" in m for m in agent.bridge._chat_log)


async def test_handle_death_uses_preempt():
    """Death should cancel an in-flight chat too, not just halt the queue."""
    agent = _make_agent()
    agent.queue.start()
    try:
        agent._chat_trigger.set()
        await agent._handle_death()
        assert not agent._chat_trigger.is_set()
    finally:
        await agent.queue.stop()
