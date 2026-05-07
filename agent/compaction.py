"""Conversation history compaction.

When `self.messages` grows past a budget, an older slice gets replaced with a
short summary so the agent can keep going without dragging the full history
through every API call.

Design (informed by how Claude Code's auto-compact works):
- Single Claude call. The summarizer is given the `writeMemory` tool so any
  durable knowledge worth promoting (locations, persistent rules) is captured
  in the same turn — no separate reflection phase.
- The to-be-evicted slice is rendered as a plain-text transcript inside ONE
  user message. We do not hand Claude back its own structured tool_use /
  tool_result blocks: pairing them up cleanly across an arbitrary cut point
  is fiddly, and image base64 is enormous waste in a summary call.
- The cut between summarized/kept lands at a chat-turn boundary (a user
  message with plain-string content) — guarantees we never strand a
  tool_result without its tool_use.
- Resulting message shape replacing the evicted slice:
    [user: "<conversation_summary>...</conversation_summary>"]
    [assistant: "Continuing from compacted context."]
  Two messages because the kept slice starts with a user message — without
  the assistant ack we'd have user-then-user, which the API rejects.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from agent.memory import read_memory, write_memory

logger = logging.getLogger(__name__)


COMPACT_THRESHOLD = 80
KEEP_RECENT = 20
TRANSCRIPT_TEXT_TRIM = 1000


COMPACTION_SYSTEM_PROMPT = """You are summarizing the earlier portion of a conversation between a Minecraft agent (named Claude) and one or more players. The agent will continue the conversation from your summary alone — the raw transcript will be discarded after you respond.

Your job is twofold:

1. Write a faithful, dense summary of what happened. Cover: the player(s) involved, what was asked, what the agent did (actions taken, key results, failures), the current goal state, and any open threads. Wrap the summary in <conversation_summary>...</conversation_summary> tags. Be concrete (coords, item counts, block types) — the agent will plan from this.

2. While reading, watch for anything worth promoting to durable memory: locations the agent discovered (base, mines, portals, hazards), persistent rules learned, long-lived facts. Memory survives across compactions and across sessions; conversation summaries do not. If you find something worth keeping, call the writeMemory tool with the FULL new contents of memory.md (it does not edit in place). The current memory.md contents are shown below — preserve existing entries unless they're now wrong, and add new ones at the end of the appropriate section.

Do NOT include things that are already in gameState every turn (current position, health, inventory, time). Do NOT include in-progress plan steps — those live in plan.md, which the agent re-reads each turn.

The conversation may be in the middle of an active task — the agent could be partway through a long autonomous run when compaction fired. If so, capture in-flight intent: what the agent was doing, what it had completed, what it was about to do next, any partial results that haven't been collected yet. The post-summary continuation must be able to resume cleanly without re-deriving plan from scratch.

Be terse. A good summary is much shorter than the transcript."""


def needs_compaction(messages: list[dict[str, Any]], threshold: int | None = None) -> bool:
    if threshold is None:
        threshold = COMPACT_THRESHOLD
    return len(messages) > threshold


def _find_cut_index(messages: list[dict[str, Any]], keep_recent: int) -> int | None:
    """Find the index where the kept slice should start.

    Two valid boundary types:
    - Chat-turn boundary: a user message with plain-string content (one
      inbound chat). Always safe — no pending tool_use chain.
    - Mid-turn boundary: an assistant message whose immediately-preceding
      user message is a complete tool_results list. Cutting here keeps the
      assistant↔tool_results pair intact in the evicted slice and starts
      kept cleanly with a new assistant turn.

    We search from `len - keep_recent` forward looking for the first such
    boundary. Returns None if no boundary exists in the search window (the
    cut would otherwise strand a tool_use without its tool_result, or vice
    versa) — caller should skip compaction that round.
    """
    start = max(0, len(messages) - keep_recent)
    for i in range(start, len(messages)):
        msg = messages[i]
        if msg["role"] == "user" and isinstance(msg.get("content"), str):
            return i
        if (
            msg["role"] == "assistant"
            and i > 0
            and messages[i - 1]["role"] == "user"
            and isinstance(messages[i - 1].get("content"), list)
            and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in messages[i - 1]["content"]
            )
        ):
            return i
    return None


def render_transcript(messages: list[dict[str, Any]]) -> str:
    """Render a slice of message-history as a plain-text transcript.

    Strips image base64 (kept as `[image]` placeholder), trims long text
    blocks, preserves tool names + structural shape so the summarizer sees
    what kind of work was done.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            label = "user" if role == "user" else "assistant"
            lines.append(f"[{label}]: {_trim(content)}")
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            btype = block.get("type")
            if btype == "text":
                lines.append(f"[assistant text]: {_trim(block.get('text', ''))}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                if name == "newAction" and isinstance(inp, dict) and "code" in inp:
                    lines.append(f"[tool_use: newAction]: {_trim(inp['code'])}")
                else:
                    lines.append(f"[tool_use: {name}]: {_trim(json.dumps(inp))}")
            elif btype == "tool_result":
                inner = block.get("content")
                lines.append(f"[tool_result]: {_render_tool_result_inner(inner)}")
    return "\n".join(lines)


def _render_tool_result_inner(inner: Any) -> str:
    if isinstance(inner, str):
        return _trim(inner)
    if isinstance(inner, list):
        parts = []
        for sub in inner:
            stype = sub.get("type")
            if stype == "image":
                parts.append("[image]")
            elif stype == "text":
                parts.append(_trim(sub.get("text", "")))
            else:
                parts.append(f"[{stype}]")
        return " ".join(parts)
    return _trim(str(inner))


def _trim(text: str, limit: int = TRANSCRIPT_TEXT_TRIM) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ⏎ ")
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[truncated {len(text) - limit} chars]"


_SUMMARY_RE = re.compile(
    r"<conversation_summary>(.*?)</conversation_summary>",
    re.DOTALL,
)


def extract_summary(text: str) -> str:
    """Pull the summary out of <conversation_summary> tags.

    Falls back to the raw text if Claude forgot the tags — better to keep
    a slightly-malformed summary than discard it and lose all the work.
    """
    match = _SUMMARY_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


WRITE_MEMORY_TOOL: dict[str, Any] = {
    "name": "writeMemory",
    "description": (
        "Replace the contents of state/memory.md. Pass the FULL new file content. "
        "Use to promote durable knowledge (locations, hazards, persistent rules) "
        "that the agent should carry forward beyond this conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full new content of memory.md.",
            }
        },
        "required": ["content"],
    },
}


async def compact(
    claude,
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = KEEP_RECENT,
    model: str | None = None,
    on_summary: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]] | None:
    """Run one compaction pass and return the new message list.

    Returns None if no safe cut boundary exists (caller keeps current history
    and retries next turn). Raises on Claude API errors — callers should NOT
    swallow these; let them propagate so failures are loud.

    `model` overrides the Claude client's default model for this call (e.g.
    cheaper Haiku for compaction). When None, the client's main model is used.

    Side effects:
    - Writes state/memory.md if Claude calls writeMemory.
    - Logs the summary text + memory delta.
    """
    cut = _find_cut_index(messages, keep_recent)
    if cut is None or cut == 0:
        logger.info("compaction: no safe cut boundary, skipping")
        return None

    evicted = messages[:cut]
    kept = messages[cut:]

    transcript = render_transcript(evicted)
    current_memory = read_memory() or "(empty)"
    user_payload = (
        f"Current memory.md contents:\n```\n{current_memory}\n```\n\n"
        f"Conversation transcript to summarize:\n```\n{transcript}\n```\n\n"
        f"Now produce the summary in <conversation_summary> tags, and call writeMemory if anything is worth promoting to durable memory."
    )

    response = await claude.send_raw(
        system=COMPACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_payload}],
        tools=[WRITE_MEMORY_TOOL],
        max_tokens=2048,
        model=model,
    )

    summary_text_parts: list[str] = []
    memory_writes = 0
    for block in response.content:
        if block.type == "text":
            summary_text_parts.append(block.text)
        elif block.type == "tool_use" and block.name == "writeMemory":
            content = (block.input or {}).get("content", "")
            lines = write_memory(content)
            memory_writes += 1
            logger.info(f"compaction: writeMemory called ({lines} lines)")

    summary_raw = "\n".join(summary_text_parts).strip()
    if not summary_raw:
        logger.warning("compaction: Claude returned no text summary, skipping")
        return None

    summary = extract_summary(summary_raw)
    logger.info(
        f"compaction: evicted {len(evicted)} messages → {len(summary)}-char summary "
        f"(memory_writes={memory_writes}, kept={len(kept)})"
    )

    summary_msg = {
        "role": "user",
        "content": f"<conversation_summary>\n{summary}\n</conversation_summary>",
    }
    # If kept starts with a user message (chat-turn cut), we need an
    # assistant ack between them to keep roles alternating. If kept starts
    # with an assistant message (mid-turn cut), the summary user message
    # flows directly into it — no ack needed (and adding one would create
    # an assistant→assistant violation).
    if kept[0]["role"] == "user":
        synthetic = [
            summary_msg,
            {"role": "assistant", "content": "Continuing from compacted context."},
        ]
    else:
        synthetic = [summary_msg]

    if on_summary is not None:
        try:
            on_summary({
                "summary": summary,
                "evicted_messages": len(evicted),
                "kept_messages": len(kept),
                "transcript_chars": len(transcript),
                "memory_writes": memory_writes,
            })
        except Exception:
            logger.exception("compaction: on_summary callback raised")

    return synthetic + kept
