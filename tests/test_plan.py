"""Tests for the persistent plan document feature."""

from __future__ import annotations

import pytest

from agent import plan as plan_module
from agent.plan import read_plan, write_plan
from agent.prompt import build_system_prompt


@pytest.fixture
def isolated_plan(tmp_path, monkeypatch):
    """Redirect PLAN_PATH to a tmp location for each test."""
    test_path = tmp_path / "state" / "plan.md"
    monkeypatch.setattr(plan_module, "PLAN_PATH", test_path)
    return test_path


def test_read_plan_missing_returns_empty(isolated_plan):
    assert not isolated_plan.exists()
    assert read_plan() == ""


def test_read_plan_returns_contents(isolated_plan):
    isolated_plan.parent.mkdir(parents=True)
    isolated_plan.write_text("- step 1\n- step 2\n", encoding="utf-8")
    assert read_plan() == "- step 1\n- step 2\n"


def test_write_plan_creates_file_and_parent_dir(isolated_plan):
    assert not isolated_plan.parent.exists()
    lines = write_plan("hello\nworld\n")
    assert isolated_plan.exists()
    assert isolated_plan.read_text(encoding="utf-8") == "hello\nworld\n"
    assert lines == 2


def test_write_plan_empty_returns_zero_lines(isolated_plan):
    write_plan("something")
    lines = write_plan("")
    assert isolated_plan.read_text(encoding="utf-8") == ""
    assert lines == 0


def test_write_plan_atomic_no_tmp_file_left(isolated_plan):
    write_plan("foo\nbar\n")
    tmp_files = list(isolated_plan.parent.glob("*.tmp"))
    assert tmp_files == [], f"tmp files left over: {tmp_files}"


def test_write_plan_overwrites_existing(isolated_plan):
    write_plan("version 1")
    write_plan("version 2")
    assert isolated_plan.read_text(encoding="utf-8") == "version 2"


def test_system_prompt_contains_planning_section():
    prompt = build_system_prompt("TestBot")
    assert "## Planning" in prompt
    assert "writePlan" in prompt
    assert "<plan_document>" in prompt
    assert "./state/plan.md" in prompt


def test_write_plan_tool_registered():
    from agent.claude import TOOLS

    names = [t["name"] for t in TOOLS]
    assert "writePlan" in names
    tool = next(t for t in TOOLS if t["name"] == "writePlan")
    props = tool["input_schema"]["properties"]
    assert "content" in props
    assert props["content"]["type"] == "string"
    assert "content" in tool["input_schema"]["required"]


@pytest.fixture
def agent(isolated_plan):
    """Minimal Agent instance for dispatch/injection tests."""
    from agent.agent import Agent
    from agent.bridge import MockBridgeClient

    return Agent(bridge=MockBridgeClient(), claude=None, bot_name="TestBot")


async def test_dispatch_write_plan_writes_file(agent, isolated_plan):
    result = await agent._dispatch_tool("writePlan", {"content": "- step 1\n- step 2"})
    assert "saved" in result.lower()
    assert "2" in result
    assert isolated_plan.read_text(encoding="utf-8") == "- step 1\n- step 2"


async def test_dispatch_write_plan_empty_clears(agent, isolated_plan):
    await agent._dispatch_tool("writePlan", {"content": "something"})
    result = await agent._dispatch_tool("writePlan", {"content": ""})
    assert "clear" in result.lower()
    assert isolated_plan.read_text(encoding="utf-8") == ""


def test_inject_plan_empty_uses_placeholder(agent, isolated_plan):
    assert not isolated_plan.exists()
    agent._inject_plan()

    assert len(agent.messages) == 2

    assistant_msg = agent.messages[0]
    assert assistant_msg["role"] == "assistant"
    tool_use = assistant_msg["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["id"] == "plan_auto"
    assert tool_use["name"] == "plan"

    user_msg = agent.messages[1]
    assert user_msg["role"] == "user"
    tool_result = user_msg["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "plan_auto"
    content = tool_result["content"]
    assert "<plan_document>" in content
    assert "</plan_document>" in content
    assert "(no active plan)" in content


def test_inject_plan_with_content_wraps_in_tags(agent, isolated_plan):
    isolated_plan.parent.mkdir(parents=True)
    isolated_plan.write_text("- mine 3 oak logs\n- craft planks\n", encoding="utf-8")

    agent._inject_plan()

    content = agent.messages[1]["content"][0]["content"]
    assert "<plan_document>" in content
    assert "</plan_document>" in content
    assert "- mine 3 oak logs" in content
    assert "- craft planks" in content
    assert "(no active plan)" not in content


async def test_handle_chat_injects_plan_before_gamestate(agent, isolated_plan, monkeypatch):
    """End-to-end: chat arrival injects plan (chat-level) then fresh gameState per iteration."""
    isolated_plan.parent.mkdir(parents=True)
    isolated_plan.write_text("- current step\n", encoding="utf-8")

    # Stub Claude to return an empty (end_turn) response so the loop exits immediately.
    class _FakeBlock:
        def __init__(self):
            self.type = "text"
            self.text = "ok"

    class _FakeResponse:
        stop_reason = "end_turn"
        content = [_FakeBlock()]

    async def fake_send(system, messages, max_tokens=4096):
        return _FakeResponse()

    class _FakeClaude:
        send = staticmethod(fake_send)

    agent.claude = _FakeClaude()

    # Stub chat sending so we don't hit the mock bridge's chat method unnecessarily.
    sent = []
    async def fake_send_chat(text):
        sent.append(text)
    monkeypatch.setattr(agent, "_send_chat", fake_send_chat)

    await agent._handle_chat_traced({"username": "player1", "message": "hi"})

    # plan_auto is injected once per chat; gameState injected at each iteration (iter 0 here).
    ids = [
        (m.get("content", [{}])[0].get("id") if isinstance(m.get("content"), list) else None)
        for m in agent.messages
    ]
    assert "plan_auto" in ids
    assert "gamestate_auto_0" in ids
    # Plan is chat-level so it comes first; gameState is per-iteration and follows.
    assert ids.index("plan_auto") < ids.index("gamestate_auto_0")

    # Verify the plan content was read from disk and wrapped.
    plan_idx = ids.index("plan_auto")
    plan_result_msg = agent.messages[plan_idx + 1]
    plan_content = plan_result_msg["content"][0]["content"]
    assert "- current step" in plan_content
    assert "<plan_document>" in plan_content
