"""Tests for the Runtime MCP-facing surface (P3).

Covers the methods an MCP server maps tools onto — execute / get_state /
screenshot / get_handler / set_handler / wait_for_event — plus the canonical
event router and the say() primitive, driven directly against a fake bridge
with no brain in the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from mineclaude.bridge import BridgeResponse
from mineclaude.models import Event, GameState
from mineclaude.primitives import make_primitives
from mineclaude.runtime import Runtime
from mineclaude.sandbox import SandboxError


def _default_status() -> dict:
    return {
        "position": {"x": 1.0, "y": 64.0, "z": 2.0},
        "health": 18.0,
        "hunger": 17,
        "biome": "plains",
        "dimension": "overworld",
        "time": 1000,
        "inventory": [{"slot": 0, "name": "oak_log", "count": 12}],
        "equipped": {"hand": "iron_pickaxe"},
        "held_slot": 3,
    }


class _FakeBridge:
    """Just the bridge surface Runtime touches in these tests."""

    def __init__(self, status: dict | None = None, mod_events: list[dict] | None = None):
        self._status = status if status is not None else _default_status()
        self._mod_events = mod_events or []
        self.chat_messages: list[str] = []
        self.stop_calls = 0
        self.attack_stop_calls = 0
        self.screenshot_resp = BridgeResponse(
            "success", "ok",
            {"image": "BASE64DATA", "format": "jpeg", "width": 854, "height": 480},
        )

    async def get_status(self, include_events: bool = False) -> BridgeResponse:
        data = dict(self._status)
        if include_events:
            # Mirror the mod's EventLog: include_events drains it server-side,
            # so subsequent calls don't re-ship the same events.
            data["events"] = [dict(e) for e in self._mod_events]
            self._mod_events = []
        return BridgeResponse("success", "ok", data)

    async def chat(self, message: str) -> BridgeResponse:
        self.chat_messages.append(message)
        return BridgeResponse("success", "ok", {})

    async def stop(self) -> BridgeResponse:
        self.stop_calls += 1
        return BridgeResponse("success", "ok", {})

    async def attack_stop(self) -> BridgeResponse:
        self.attack_stop_calls += 1
        return BridgeResponse("success", "ok", {})

    async def screenshot(self, yaw=None, pitch=None, look_at=None) -> BridgeResponse:
        return self.screenshot_resp


def _runtime(bridge: _FakeBridge | None = None) -> Runtime:
    return Runtime(bridge or _FakeBridge())


# --- execute ---------------------------------------------------------------


async def test_execute_completed_returns_result():
    rt = _runtime()
    rt.start()
    res = await rt.execute("return 2 + 2")
    assert res.status == "completed"
    assert res.result == "4"
    assert res.action_id
    assert res.error is None


async def test_execute_code_error_is_failed():
    rt = _runtime()
    rt.start()
    res = await rt.execute("return undefined_name")
    assert res.status == "failed"
    assert "Action error" in (res.error or "")


async def test_execute_import_rejected_is_failed():
    rt = _runtime()
    rt.start()
    res = await rt.execute("import os\nreturn 1")
    assert res.status == "failed"
    assert "Imports are not allowed" in (res.error or "")


async def test_execute_concurrent_call_is_busy():
    rt = _runtime()
    rt.start()
    slow = asyncio.create_task(rt.execute("await sleep(0.3)\nreturn 'done'"))
    await asyncio.sleep(0.02)  # let the first execute claim the slot
    busy = await rt.execute("return 1")
    assert busy.status == "busy"
    assert busy.action_id == ""
    first = await slow
    assert first.status == "completed"
    assert first.result == "done"


async def test_execute_interrupt_cancels_in_flight():
    bridge = _FakeBridge()
    rt = _runtime(bridge)
    rt.start()
    task = asyncio.create_task(rt.execute("await sleep(5)\nreturn 'never'"))
    await asyncio.sleep(0.05)  # let the action reach RUNNING
    await rt.interrupt()
    res = await task
    assert res.status == "cancelled"
    # interrupt halts bridge-side machinery via the pre-interrupt hook.
    assert bridge.stop_calls >= 1
    assert bridge.attack_stop_calls >= 1


async def test_execute_timeout_maps_to_timeout_status():
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(0.5)\nreturn 'x'", timeout=0.05)
    assert res.status == "timeout"
    assert "timed out" in (res.error or "")


async def test_execute_slot_frees_after_completion():
    rt = _runtime()
    rt.start()
    await rt.execute("return 1")
    # Slot released — a second execute is accepted, not busy.
    res = await rt.execute("return 2")
    assert res.status == "completed"
    assert res.result == "2"


async def test_execute_outliving_inline_wait_returns_running_handle():
    # A long action with a short inline-wait budget hands back a running
    # handle (instead of erroring) while it keeps going in the background.
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(5)\nreturn 'done'", timeout=30, wait=0.1)
    assert res.status == "running"
    assert res.action_id
    # The slot stays held while it runs: a concurrent execute is rejected busy.
    busy = await rt.execute("return 'x'", wait=0.1)
    assert busy.status == "busy"
    # When the background action finishes, the slot frees and the result is
    # observable on the live snapshot.
    await rt.queue.drain()
    state = await rt.get_state()
    assert state.action["state"] == "completed"
    assert state.action["result"] == "done"
    assert (await rt.execute("return 'next'", wait=0.1)).status == "completed"


async def test_execute_running_handle_can_be_interrupted():
    # interrupt() releases a slot held by a backgrounded running action.
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(5)\nreturn 'never'", timeout=30, wait=0.1)
    assert res.status == "running"
    await rt.interrupt()
    # Slot freed by preempt() even though the cancel path emits no completion.
    assert (await rt.execute("return 'ok'", wait=0.1)).status == "completed"


async def test_execute_inline_wait_env_override(monkeypatch):
    monkeypatch.setenv("MINECLAUDE_EXECUTE_WAIT_S", "0.1")
    rt = _runtime()
    rt.start()
    assert rt._inline_wait_s == 0.1
    # Default wait now picks up the env value → long action returns running.
    res = await rt.execute("await sleep(5)\nreturn 'x'", timeout=30)
    assert res.status == "running"


# --- get_state -------------------------------------------------------------


async def test_get_state_shape():
    rt = _runtime()
    state = await rt.get_state()
    assert isinstance(state, GameState)
    assert state.player["pos"] == [1.0, 64.0, 2.0]
    assert state.player["health"] == 18.0
    assert state.player["dimension"] == "overworld"
    assert state.inventory == [{"slot": 0, "name": "oak_log", "count": 12}]
    # equipped is normalized to the fixed 5-slot shape.
    assert state.equipped == {
        "hand": "iron_pickaxe", "head": None, "chest": None, "legs": None, "feet": None,
    }
    # held_slot (selected hotbar index) is carried into the player block.
    assert state.player["held_slot"] == 3
    assert state.action["state"] == "idle"
    assert state.events == []
    assert state.events_truncated is False


async def test_get_state_flush_drains_buffer_and_mod_events():
    mod_events = [{"type": "block_broken", "block": "stone",
                   "pos": {"x": 1, "y": 63, "z": 2}, "ts_ms": 1_700_000_000_000}]
    rt = _runtime(_FakeBridge(mod_events=mod_events))
    rt._record_event("chat", {"username": "Steve", "message": "hi"})

    state = await rt.get_state(flush=True)
    types = [e["type"] for e in state.events]
    assert "chat" in types
    assert "block_broken" in types
    # block_broken's mod fields land under data; ts normalized to seconds.
    blk = next(e for e in state.events if e["type"] == "block_broken")
    assert blk["data"]["block"] == "stone"
    assert blk["ts"] == pytest.approx(1_700_000_000.0)

    # Buffer + mod log were drained — a second flush is empty.
    again = await rt.get_state(flush=True)
    assert again.events == []


async def test_get_state_no_flush_peeks_without_draining():
    rt = _runtime()
    rt._record_event("chat", {"message": "hi"})
    state = await rt.get_state(flush=False)
    assert [e["type"] for e in state.events] == ["chat"]
    # Not drained — still there for the next read.
    assert len(rt._events) == 1


async def test_get_state_caps_returned_events_and_flags_truncation():
    from mineclaude.runtime import MAX_RETURNED_EVENTS

    rt = _runtime()
    n = MAX_RETURNED_EVENTS + 30
    for i in range(n):
        rt._record_event("block_broken", {"i": i})
    state = await rt.get_state(flush=False)
    # Returned list is capped to the newest MAX_RETURNED_EVENTS, truncation flagged.
    assert len(state.events) == MAX_RETURNED_EVENTS
    assert state.events_truncated is True
    # Kept the most recent ones (highest i), dropped the oldest.
    kept = [e["data"]["i"] for e in state.events]
    assert kept == list(range(n - MAX_RETURNED_EVENTS, n))


async def test_get_state_running_action_view():
    rt = _runtime()
    rt.start()
    task = asyncio.create_task(rt.execute("await sleep(0.3)\nreturn 'x'"))
    await asyncio.sleep(0.05)
    state = await rt.get_state(flush=False)
    assert state.action["state"] == "running"
    assert state.action["id"]
    await task


# --- screenshot ------------------------------------------------------------


async def test_screenshot_returns_model():
    rt = _runtime()
    shot = await rt.screenshot(look_at=(10.0, 64.0, 5.0))
    assert shot.image_base64 == "BASE64DATA"
    assert shot.format == "jpeg"
    assert shot.width == 854 and shot.height == 480


async def test_screenshot_raises_on_bridge_error():
    bridge = _FakeBridge()
    bridge.screenshot_resp = BridgeResponse("error", "camera unavailable")
    rt = _runtime(bridge)
    with pytest.raises(RuntimeError, match="camera unavailable"):
        await rt.screenshot()


# --- event router ----------------------------------------------------------


async def test_handle_event_chat_is_recorded_not_dispatched():
    rt = _runtime()
    await rt._handle_event({"type": "chat", "data": {"username": "Steve", "message": "yo"}})
    assert [e.type for e in rt._events] == ["chat"]
    # Chat is not a hazard — nothing landed in the reflex buffer.
    assert len(rt.reflexes.recent) == 0


async def test_handle_event_death_preempts_and_records():
    rt = _runtime()
    fired = []
    rt.add_preempt_hook(lambda: _record(fired))
    await rt._handle_event({"type": "death", "data": {}})
    assert fired == ["preempt"]
    assert [e.type for e in rt._events] == ["death"]


async def _record(sink):
    sink.append("preempt")


async def test_handle_event_hazard_dispatched_not_buffered():
    rt = _runtime()  # register_default_handlers ran in __init__
    await rt._handle_event({"type": "hostile_nearby", "data": {"kind": "creeper", "distance": 6}})
    await rt.reflexes.flush()
    # Hazard surfaces in reflexes_recent, NOT the flushable event buffer.
    assert [e["type"] for e in rt.reflexes.recent] == ["hostile_nearby"]
    assert [e.type for e in rt._events] == []


async def test_resume_appends_reflex_done_event():
    rt = _runtime()
    rt.resume("entered_lava")
    assert len(rt._events) == 1
    ev = rt._events[0]
    assert ev.type == "reflex_done"
    assert ev.data == {"event_type": "entered_lava"}


# --- wait_for_event --------------------------------------------------------


async def test_wait_for_event_returns_matching_event():
    rt = _runtime()
    waiter = asyncio.create_task(rt.wait_for_event(["chat"], timeout=1.0))
    await asyncio.sleep(0)  # let the waiter park
    rt._record_event("chat", {"message": "hello"})
    ev = await waiter
    assert isinstance(ev, Event)
    assert ev.type == "chat"
    assert ev.data == {"message": "hello"}


async def test_wait_for_event_times_out_to_none():
    rt = _runtime()
    ev = await rt.wait_for_event(["never"], timeout=0.05)
    assert ev is None


async def test_wait_for_event_type_filter_skips_non_matches():
    rt = _runtime()
    waiter = asyncio.create_task(rt.wait_for_event(["chat"], timeout=1.0))
    await asyncio.sleep(0)
    rt._record_event("death", {})        # non-match — waiter keeps waiting
    rt._record_event("chat", {"m": 1})   # match
    ev = await waiter
    assert ev.type == "chat"


async def test_wait_for_event_any_type_when_none():
    rt = _runtime()
    waiter = asyncio.create_task(rt.wait_for_event(None, timeout=1.0))
    await asyncio.sleep(0)
    rt._record_event("respawn", {})
    ev = await waiter
    assert ev.type == "respawn"


# --- action_done events (companion to the status="running" handle) ---------


async def test_backgrounded_action_emits_action_done_on_completion():
    # A long action handed back as a running handle fires action_done when it
    # finishes, so a wait_for_event(["action_done"]) consumer unblocks.
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(0.2)\nreturn 'deep'", timeout=30, wait=0.05)
    assert res.status == "running"
    waiter = asyncio.create_task(rt.wait_for_event(["action_done"], timeout=2.0))
    ev = await waiter
    assert ev.type == "action_done"
    assert ev.data["action_id"] == res.action_id
    assert ev.data["status"] == "completed"
    assert ev.data["result"] == "deep"


async def test_blocking_action_emits_no_action_done():
    # A normal inline execute already returned its result — no buffer noise.
    rt = _runtime()
    rt.start()
    await rt.execute("return 'fast'")
    state = await rt.get_state()
    assert [e for e in state.events if e["type"] == "action_done"] == []


async def test_interrupted_backgrounded_action_emits_action_done():
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(5)\nreturn 'never'", timeout=30, wait=0.05)
    assert res.status == "running"
    await rt.interrupt()
    state = await rt.get_state()
    done = [e for e in state.events if e["type"] == "action_done"]
    assert len(done) == 1
    assert done[0]["data"]["status"] == "cancelled"
    assert done[0]["data"]["action_id"] == res.action_id


# --- wait_for_action (level-triggered completion) --------------------------


async def test_wait_for_action_blocks_until_backgrounded_action_done():
    # The clean idiom: execute(wait=0) -> running handle, then wait_for_action
    # blocks once and returns the terminal result with no timeout to guess.
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(0.2)\nreturn 'deep'", timeout=30, wait=0.05)
    assert res.status == "running"
    done = await rt.wait_for_action(res.action_id, timeout=5.0)
    assert done.status == "completed"
    assert done.action_id == res.action_id
    assert done.result == "deep"


async def test_wait_for_action_returns_immediately_if_already_done():
    # Level-triggered: an action that already terminated (and may have aged into
    # `recent`) is reported instantly — no missable future-only race.
    rt = _runtime()
    rt.start()
    res = await rt.execute("return 'fast'")
    assert res.status == "completed"
    done = await rt.wait_for_action(res.action_id, timeout=5.0)
    assert done.status == "completed"
    assert done.result == "fast"


async def test_wait_for_action_unknown_id_is_failed():
    rt = _runtime()
    rt.start()
    res = await rt.wait_for_action("deadbeef", timeout=1.0)
    assert res.status == "failed"
    assert "unknown action_id" in (res.error or "")


async def test_wait_for_action_running_handle_on_timeout():
    # Still going when the wait elapses -> status="running" (call again /
    # interrupt), not a lie that it finished.
    rt = _runtime()
    rt.start()
    res = await rt.execute("await sleep(5)\nreturn 'never'", timeout=30, wait=0.05)
    assert res.status == "running"
    out = await rt.wait_for_action(res.action_id, timeout=0.2)
    assert out.status == "running"
    assert out.action_id == res.action_id
    await rt.interrupt()


# --- handlers --------------------------------------------------------------


async def test_get_handler_builtin_defaults():
    rt = _runtime()
    chat = rt.get_handler("chat")
    assert chat.source == "default" and chat.preempts is False and chat.code is None
    death = rt.get_handler("death")
    assert death.preempts is True  # death is record + preempt


async def test_get_handler_reads_registry_default():
    rt = _runtime()
    info = rt.get_handler("damage_taken")
    assert info.source == "default"
    assert info.code is None
    assert info.cooldown_s == 30.0  # the registered default's policy


async def test_set_handler_installs_authored_body():
    rt = _runtime()
    info = rt.set_handler("chat", "log('reacting')", preempts=False, cooldown_s=1.5)
    assert info.source == "authored"
    assert info.code == "log('reacting')"
    assert info.cooldown_s == 1.5
    # get_handler now reports it as authored.
    assert rt.get_handler("chat").source == "authored"
    assert "chat" in rt.reflexes.known_types()


async def test_set_handler_rejects_imports():
    rt = _runtime()
    with pytest.raises(SandboxError):
        rt.set_handler("chat", "import os")


async def test_set_handler_body_runs_on_dispatch_with_data_and_say():
    bridge = _FakeBridge()
    rt = _runtime(bridge)
    rt.set_handler("chat", "await say(data['message'])")
    await rt._handle_event({"type": "chat", "data": {"message": "echo this"}})
    await rt.reflexes.flush()
    assert bridge.chat_messages == ["echo this"]


# --- say primitive ---------------------------------------------------------


async def test_say_primitive_sends_chat():
    bridge = _FakeBridge()
    prims = make_primitives(bridge)
    await prims["say"]("hello there")
    assert bridge.chat_messages == ["hello there"]


async def test_say_primitive_splits_long_message():
    bridge = _FakeBridge()
    prims = make_primitives(bridge)
    word = "blockblock"  # 10 chars
    msg = " ".join([word] * 40)  # ~440 chars, forces a split at 240
    await prims["say"](msg)
    assert len(bridge.chat_messages) >= 2
    assert all(len(m) <= 240 for m in bridge.chat_messages)
    # No content lost — rejoined chunks reproduce the words.
    assert " ".join(bridge.chat_messages).split() == msg.split()


async def test_say_primitive_available_in_execute():
    bridge = _FakeBridge()
    rt = _runtime(bridge)
    rt.start()
    res = await rt.execute("await say('from execute')\nreturn 'ok'")
    assert res.status == "completed"
    assert bridge.chat_messages == ["from execute"]
