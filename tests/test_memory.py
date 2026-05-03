"""Tests for the persistent memory document feature."""

from __future__ import annotations

import pytest

from agent import memory as memory_module
from agent.memory import read_memory, write_memory
from agent.prompt import build_system_prompt


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    """Redirect MEMORY_PATH to a tmp location for each test."""
    test_path = tmp_path / "state" / "memory.md"
    monkeypatch.setattr(memory_module, "MEMORY_PATH", test_path)
    return test_path


def test_read_memory_missing_returns_empty(isolated_memory):
    assert not isolated_memory.exists()
    assert read_memory() == ""


def test_read_memory_returns_contents(isolated_memory):
    isolated_memory.parent.mkdir(parents=True)
    isolated_memory.write_text("## Locations\n- home | overworld | 0,64,0 |\n", encoding="utf-8")
    assert read_memory() == "## Locations\n- home | overworld | 0,64,0 |\n"


def test_write_memory_creates_file_and_parent_dir(isolated_memory):
    assert not isolated_memory.parent.exists()
    lines = write_memory("hello\nworld\n")
    assert isolated_memory.exists()
    assert isolated_memory.read_text(encoding="utf-8") == "hello\nworld\n"
    assert lines == 2


def test_write_memory_empty_returns_zero_lines(isolated_memory):
    write_memory("something")
    lines = write_memory("")
    assert isolated_memory.read_text(encoding="utf-8") == ""
    assert lines == 0


def test_write_memory_atomic_no_tmp_file_left(isolated_memory):
    write_memory("foo\nbar\n")
    tmp_files = list(isolated_memory.parent.glob("*.tmp"))
    assert tmp_files == [], f"tmp files left over: {tmp_files}"


def test_write_memory_overwrites_existing(isolated_memory):
    write_memory("version 1")
    write_memory("version 2")
    assert isolated_memory.read_text(encoding="utf-8") == "version 2"


def test_system_prompt_contains_memory_section():
    prompt = build_system_prompt("TestBot")
    assert "## Memory" in prompt
    assert "writeMemory" in prompt
    assert "<memory>" in prompt
    assert "./state/memory.md" in prompt


def test_write_memory_tool_registered():
    from agent.claude import TOOLS

    names = [t["name"] for t in TOOLS]
    assert "writeMemory" in names
    tool = next(t for t in TOOLS if t["name"] == "writeMemory")
    props = tool["input_schema"]["properties"]
    assert "content" in props
    assert props["content"]["type"] == "string"
    assert "content" in tool["input_schema"]["required"]


@pytest.fixture
def agent(isolated_memory):
    """Minimal Agent instance for dispatch/injection tests."""
    from agent.agent import Agent
    from agent.bridge import MockBridgeClient

    return Agent(bridge=MockBridgeClient(), claude=None, bot_name="TestBot")


async def test_dispatch_write_memory_writes_file(agent, isolated_memory):
    result = await agent._dispatch_tool(
        "writeMemory", {"content": "## Locations\n- home | overworld | 0,64,0 |"}
    )
    assert "saved" in result.lower()
    assert isolated_memory.read_text(encoding="utf-8") == "## Locations\n- home | overworld | 0,64,0 |"


async def test_dispatch_write_memory_empty_clears(agent, isolated_memory):
    await agent._dispatch_tool("writeMemory", {"content": "something"})
    result = await agent._dispatch_tool("writeMemory", {"content": ""})
    assert "clear" in result.lower()
    assert isolated_memory.read_text(encoding="utf-8") == ""


def test_inject_memory_empty_uses_placeholder(agent, isolated_memory):
    assert not isolated_memory.exists()
    agent._inject_memory()

    assert len(agent.messages) == 2

    assistant_msg = agent.messages[0]
    assert assistant_msg["role"] == "assistant"
    tool_use = assistant_msg["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["id"] == "memory_auto"
    assert tool_use["name"] == "memory"

    user_msg = agent.messages[1]
    assert user_msg["role"] == "user"
    tool_result = user_msg["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "memory_auto"
    content = tool_result["content"]
    assert "<memory>" in content
    assert "</memory>" in content
    assert "(no memories saved)" in content


def test_inject_memory_with_content_wraps_in_tags(agent, isolated_memory):
    isolated_memory.parent.mkdir(parents=True)
    isolated_memory.write_text(
        "## Locations\n- home_base | overworld | 120,70,-40 | bed here\n",
        encoding="utf-8",
    )

    agent._inject_memory()

    content = agent.messages[1]["content"][0]["content"]
    assert "<memory>" in content
    assert "</memory>" in content
    assert "home_base" in content
    assert "(no memories saved)" not in content


async def test_handle_chat_injects_memory_alongside_plan(agent, isolated_memory, monkeypatch):
    """End-to-end: chat arrival injects memory (chat-level) alongside plan and fresh gameState."""
    isolated_memory.parent.mkdir(parents=True)
    isolated_memory.write_text("## Locations\n- spawn | overworld | 0,64,0 |\n", encoding="utf-8")

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

    sent = []
    async def fake_send_chat(text):
        sent.append(text)
    monkeypatch.setattr(agent, "_send_chat", fake_send_chat)

    await agent._handle_chat_traced({"username": "player1", "message": "hi"})

    ids = [
        (m.get("content", [{}])[0].get("id") if isinstance(m.get("content"), list) else None)
        for m in agent.messages
    ]
    assert "plan_auto" in ids
    assert "memory_auto" in ids
    assert "gamestate_auto_0" in ids
    # plan and memory are both chat-level, injected before per-iteration gameState.
    assert ids.index("plan_auto") < ids.index("gamestate_auto_0")
    assert ids.index("memory_auto") < ids.index("gamestate_auto_0")

    mem_idx = ids.index("memory_auto")
    mem_result_msg = agent.messages[mem_idx + 1]
    mem_content = mem_result_msg["content"][0]["content"]
    assert "spawn" in mem_content
    assert "<memory>" in mem_content
