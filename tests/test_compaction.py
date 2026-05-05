"""Tests for agent/compaction.py — history compaction with summary + memory reflection."""

from __future__ import annotations

import pytest

from agent import compaction as compaction_mod
from agent import memory as memory_module
from agent.compaction import (
    _find_cut_index,
    compact,
    extract_summary,
    needs_compaction,
    render_transcript,
)


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    test_path = tmp_path / "state" / "memory.md"
    monkeypatch.setattr(memory_module, "MEMORY_PATH", test_path)
    return test_path


# ---- needs_compaction ------------------------------------------------------


def test_needs_compaction_below_threshold():
    assert not needs_compaction([{"role": "user", "content": "x"}] * 10, threshold=80)


def test_needs_compaction_above_threshold():
    assert needs_compaction([{"role": "user", "content": "x"}] * 81, threshold=80)


# ---- _find_cut_index --------------------------------------------------------


def _user_str(text="hello"):
    return {"role": "user", "content": text}


def _assistant_text(text="ok"):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _tool_use(name="newAction", id_="t1"):
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": id_, "name": name, "input": {"code": "pass"}}],
    }


def _tool_result(id_="t1", content="done"):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": id_, "content": content}],
    }


def test_find_cut_picks_first_string_user_in_window():
    msgs = (
        [_user_str("old")] * 5
        + [_assistant_text("a"), _tool_use(), _tool_result()]
        + [_user_str("recent"), _assistant_text("b")]
    )
    cut = _find_cut_index(msgs, keep_recent=4)
    assert cut is not None
    assert msgs[cut]["role"] == "user"
    assert isinstance(msgs[cut]["content"], str)
    assert msgs[cut]["content"] == "recent"


def test_find_cut_returns_none_when_no_boundary_in_window():
    """One very long turn dominates: window has only tool_use/tool_result, no chat boundary."""
    msgs = (
        [_user_str("old")]
        + [_tool_use(f"t{i}") for i in range(10)]
        + [_tool_result(f"t{i}") for i in range(10)]
    )
    cut = _find_cut_index(msgs, keep_recent=5)
    assert cut is None


def test_find_cut_zero_when_first_message_is_boundary_in_window():
    """If keep_recent covers everything, cut should be 0 (nothing to compact)."""
    msgs = [_user_str("a"), _assistant_text("b"), _user_str("c")]
    cut = _find_cut_index(msgs, keep_recent=10)
    assert cut == 0


def test_find_cut_accepts_mid_turn_assistant_after_tool_results():
    """Mid-turn boundary: an assistant message right after a user-tool_results
    list is a valid cut point. The evicted slice keeps its assistant↔tool_results
    pair intact, kept starts cleanly with a new assistant turn."""
    msgs = (
        [_user_str("start")]
        + [_tool_use("newAction", id_="t1"), _tool_result(id_="t1")]
        + [_tool_use("newAction", id_="t2"), _tool_result(id_="t2")]
        + [_tool_use("newAction", id_="t3"), _tool_result(id_="t3")]
    )
    # Window covers only the last few messages — no string-user boundary in
    # window, but the assistants-after-tool_results boundaries qualify.
    cut = _find_cut_index(msgs, keep_recent=4)
    assert cut is not None
    assert msgs[cut]["role"] == "assistant"
    assert msgs[cut - 1]["role"] == "user"
    assert isinstance(msgs[cut - 1]["content"], list)


def test_find_cut_does_not_pick_assistant_after_non_tool_result_user():
    """Mid-turn boundary requires the preceding user message to be a
    tool_results list — a string-user (chat) preceding the assistant is
    already handled by the chat-turn rule, and the assistant-after-string-user
    case alone shouldn't qualify (we want kept to start at the chat, not
    one past it)."""
    msgs = [_user_str("hi"), _assistant_text("hello"), _user_str("again"), _assistant_text("yo")]
    cut = _find_cut_index(msgs, keep_recent=3)
    # Should pick the string-user "again" at index 2, not the assistant at 1 or 3.
    assert cut == 2
    assert msgs[cut]["content"] == "again"


# ---- render_transcript ------------------------------------------------------


def test_render_transcript_string_user():
    out = render_transcript([{"role": "user", "content": "hi there"}])
    assert "[user]:" in out
    assert "hi there" in out


def test_render_transcript_assistant_text_and_tool_use():
    out = render_transcript([
        _assistant_text("thinking"),
        _tool_use("newAction"),
    ])
    assert "[assistant text]: thinking" in out
    assert "[tool_use: newAction]" in out


def test_render_transcript_tool_result_strips_image():
    msg = {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x" * 10000}},
                {"type": "text", "text": "Screenshot captured (854x480)"},
            ],
        }],
    }
    out = render_transcript([msg])
    assert "[image]" in out
    assert "Screenshot captured" in out
    assert "x" * 100 not in out  # base64 stripped


def test_render_transcript_trims_long_text():
    long_code = "a" * 5000
    msg = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t", "name": "newAction", "input": {"code": long_code}}],
    }
    out = render_transcript([msg])
    assert "[truncated" in out
    assert len(out) < 2000


# ---- extract_summary --------------------------------------------------------


def test_extract_summary_with_tags():
    text = "preamble <conversation_summary>\nthe stuff\n</conversation_summary> trailing"
    assert extract_summary(text) == "the stuff"


def test_extract_summary_without_tags_returns_raw():
    assert extract_summary("just plain text") == "just plain text"


# ---- compact() — end-to-end with mock Claude -------------------------------


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content):
        self.content = content


class _MockClaude:
    """Records every send_raw call and returns scripted responses."""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def send_raw(self, *, system, messages, tools=None, max_tokens=2048, model=None):
        self.calls.append({
            "system": system,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "model": model,
        })
        return self.response


def _build_long_history(n_old=30, n_recent=5):
    """A history shaped like real agent traffic: chat turns interleaved with tool work."""
    msgs = []
    for i in range(n_old):
        msgs.append(_user_str(f"old chat {i}"))
        msgs.append(_assistant_text(f"old reply {i}"))
        msgs.append(_tool_use(id_=f"old_t{i}"))
        msgs.append(_tool_result(id_=f"old_t{i}", content=f"old result {i}"))
    for i in range(n_recent):
        msgs.append(_user_str(f"recent chat {i}"))
        msgs.append(_assistant_text(f"recent reply {i}"))
    return msgs


async def test_compact_replaces_older_portion_with_summary(isolated_memory):
    msgs = _build_long_history(n_old=30, n_recent=5)
    response = _Response([
        _Block("text", text="<conversation_summary>\nPlayer asked Claude to chop trees; got 12 oak logs.\n</conversation_summary>"),
    ])
    claude = _MockClaude(response)

    new = await compact(claude, msgs, keep_recent=10)

    assert new is not None
    # First two should be synthetic summary + ack
    assert new[0]["role"] == "user"
    assert "<conversation_summary>" in new[0]["content"]
    assert "12 oak logs" in new[0]["content"]
    assert new[1]["role"] == "assistant"
    assert "compacted" in new[1]["content"].lower()
    # Kept slice should preserve recent messages
    assert any(
        isinstance(m.get("content"), str) and "recent chat" in m["content"]
        for m in new[2:]
    )
    # Result should be much shorter than input
    assert len(new) < len(msgs)


async def test_compact_mid_turn_synthetic_prefix_omits_assistant_ack(isolated_memory):
    """Mid-turn cut: kept[0] is an assistant message, so the synthetic prefix
    must end with the user-summary alone — adding an assistant ack would create
    an assistant→assistant adjacency the API rejects."""
    # No chat-turn boundary in the keep window — only assistant↔tool_results pairs.
    msgs = [_user_str("start")]
    for i in range(8):
        msgs.append(_tool_use(id_=f"t{i}"))
        msgs.append(_tool_result(id_=f"t{i}"))

    response = _Response([
        _Block("text", text="<conversation_summary>\nMid-turn work in progress.\n</conversation_summary>"),
    ])
    claude = _MockClaude(response)

    new = await compact(claude, msgs, keep_recent=6)

    assert new is not None
    # Synthetic prefix is just the summary user message — no assistant ack.
    assert new[0]["role"] == "user"
    assert "<conversation_summary>" in new[0]["content"]
    assert new[1]["role"] == "assistant"
    # The assistant at index 1 must be from the kept slice (a real tool_use),
    # not the "Continuing from compacted context" ack.
    assert isinstance(new[1]["content"], list)
    assert new[1]["content"][0]["type"] == "tool_use"


async def test_compact_executes_writeMemory_tool_call(isolated_memory):
    msgs = _build_long_history(n_old=30, n_recent=5)
    response = _Response([
        _Block("text", text="<conversation_summary>\nFound iron at -50.\n</conversation_summary>"),
        _Block(
            "tool_use",
            name="writeMemory",
            id="m1",
            input={"content": "## Locations\n- iron_vein | overworld | 100, -50, 200 | found during compaction\n"},
        ),
    ])
    claude = _MockClaude(response)

    await compact(claude, msgs, keep_recent=10)

    saved = isolated_memory.read_text(encoding="utf-8")
    assert "iron_vein" in saved
    assert "100, -50, 200" in saved


async def test_compact_returns_none_when_no_safe_cut(isolated_memory):
    """One huge turn: kept window has no chat boundary, so we skip this round."""
    msgs = (
        [_user_str("very old")]
        + [_tool_use(id_=f"t{i}") for i in range(20)]
        + [_tool_result(id_=f"t{i}") for i in range(20)]
    )
    claude = _MockClaude(_Response([]))
    new = await compact(claude, msgs, keep_recent=5)
    assert new is None
    assert claude.calls == []  # Claude not called when nothing to compact


async def test_compact_skips_when_summary_is_empty(isolated_memory):
    msgs = _build_long_history(n_old=30, n_recent=5)
    # Tool call only, no text → no usable summary
    response = _Response([
        _Block("tool_use", name="writeMemory", id="m1", input={"content": "just memory"}),
    ])
    claude = _MockClaude(response)
    new = await compact(claude, msgs, keep_recent=10)
    assert new is None
    # writeMemory still ran though
    assert isolated_memory.read_text(encoding="utf-8") == "just memory"


async def test_compact_passes_current_memory_to_summarizer(isolated_memory):
    isolated_memory.parent.mkdir(parents=True, exist_ok=True)
    isolated_memory.write_text("## Locations\n- spawn | overworld | 0,64,0 |\n", encoding="utf-8")

    msgs = _build_long_history(n_old=30, n_recent=5)
    response = _Response([_Block("text", text="<conversation_summary>x</conversation_summary>")])
    claude = _MockClaude(response)

    await compact(claude, msgs, keep_recent=10)

    user_msg = claude.calls[0]["messages"][0]["content"]
    assert "spawn | overworld" in user_msg
    # writeMemory tool should be available to the summarizer
    tool_names = [t["name"] for t in claude.calls[0]["tools"]]
    assert "writeMemory" in tool_names


async def test_compact_passes_model_override_to_send_raw(isolated_memory):
    msgs = _build_long_history(n_old=30, n_recent=5)
    response = _Response([_Block("text", text="<conversation_summary>x</conversation_summary>")])
    claude = _MockClaude(response)
    await compact(claude, msgs, keep_recent=10, model="claude-haiku-4-5-20251001")
    assert claude.calls[0]["model"] == "claude-haiku-4-5-20251001"


async def test_compact_default_model_is_none(isolated_memory):
    """When no override is passed, compact() leaves model selection to the client."""
    msgs = _build_long_history(n_old=30, n_recent=5)
    response = _Response([_Block("text", text="<conversation_summary>x</conversation_summary>")])
    claude = _MockClaude(response)
    await compact(claude, msgs, keep_recent=10)
    assert claude.calls[0]["model"] is None


async def test_compact_does_not_swallow_claude_errors(isolated_memory):
    msgs = _build_long_history(n_old=30, n_recent=5)

    class _BoomClaude:
        async def send_raw(self, **kw):
            raise RuntimeError("api down")

    with pytest.raises(RuntimeError, match="api down"):
        await compact(_BoomClaude(), msgs, keep_recent=10)


# ---- agent integration ------------------------------------------------------


@pytest.fixture
def agent(isolated_memory):
    from agent.agent import Agent
    from agent.bridge import MockBridgeClient

    return Agent(bridge=MockBridgeClient(), claude=None, bot_name="TestBot")


async def test_agent_compact_if_needed_no_claude_is_noop(agent):
    """Agent constructed without a Claude client must not blow up on compaction."""
    agent.messages = _build_long_history(n_old=30, n_recent=5)
    before = len(agent.messages)
    await agent._compact_if_needed()
    assert len(agent.messages) == before


async def test_agent_compact_if_needed_below_threshold_skips(agent):
    agent.claude = _MockClaude(_Response([_Block("text", text="should not fire")]))
    agent.messages = [_user_str("only one chat")]
    await agent._compact_if_needed()
    # Claude should NOT have been called
    assert agent.claude.calls == []


async def test_agent_compact_if_needed_runs_when_over_threshold(agent, monkeypatch):
    monkeypatch.setattr(compaction_mod, "COMPACT_THRESHOLD", 5)
    agent.claude = _MockClaude(_Response([
        _Block("text", text="<conversation_summary>compact ran</conversation_summary>"),
    ]))
    agent.messages = _build_long_history(n_old=10, n_recent=3)
    before = len(agent.messages)
    await agent._compact_if_needed()
    assert len(agent.messages) < before
    assert "<conversation_summary>" in agent.messages[0]["content"]


async def test_agent_compact_if_needed_swallows_claude_errors(agent, monkeypatch):
    """At the agent boundary, errors get logged and history is left intact —
    one over-budget turn is better than crashing the chat loop."""
    monkeypatch.setattr(compaction_mod, "COMPACT_THRESHOLD", 5)

    class _BoomClaude:
        async def send_raw(self, **kw):
            raise RuntimeError("api down")

    agent.claude = _BoomClaude()
    agent.messages = _build_long_history(n_old=10, n_recent=3)
    before = len(agent.messages)
    await agent._compact_if_needed()  # must not raise
    assert len(agent.messages) == before
