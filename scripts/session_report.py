#!/usr/bin/env python3
"""Emit a human-readable timeline from an agent session JSONL log.

Usage:
    python scripts/session_report.py state/sessions/<session>.jsonl
    python scripts/session_report.py --latest

The report summarizes each chat turn: iterations, tool calls with timing,
belief mismatches, exceptions, and the final reply. Invoke when
diagnosing a reliability regression — it saves you from tailing raw JSONL.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

SESSIONS_DIR = Path("state/sessions")


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _load_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _truncate(s: str, n: int = 140) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _render_tool_dispatch(e: dict) -> str:
    d = e.get("data", {})
    name = d.get("name")
    elapsed = d.get("elapsed_ms")
    result = d.get("result")
    if isinstance(result, dict):
        result_str = result.get("text") or json.dumps(result)
    else:
        result_str = str(result) if result is not None else ""
    first_line = result_str.splitlines()[0] if result_str else ""
    flag = ""
    if first_line.startswith("[partial]"):
        flag = " PARTIAL"
    elif first_line.startswith("[status=error") or first_line.startswith("Error"):
        flag = " ERROR"
    return f"tool.{name} ({elapsed}ms){flag}  →  {_truncate(first_line, 120)}"


def _render_claude_response(e: dict) -> str:
    d = e.get("data", {})
    stop = d.get("stop_reason")
    blocks = d.get("blocks") or []
    tool_names = [b.get("tool_name") for b in blocks if b.get("tool_name")]
    text_preview = ""
    for b in blocks:
        if b.get("type") == "text" and b.get("text"):
            text_preview = _truncate(b["text"].replace("\n", " "), 100)
            break
    tool_str = f" tools={tool_names}" if tool_names else ""
    text_str = f' text="{text_preview}"' if text_preview else ""
    return f"claude_response stop={stop}{tool_str}{text_str}"


def _render_subaction(e: dict) -> str:
    d = e.get("data", {})
    name = d.get("name")
    status = d.get("status")
    args = d.get("args") or {}
    arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
    if status == "failed":
        err = d.get("error") or ""
        return f"  └─ {name}({arg_str}) FAILED: {_truncate(err, 120)}"
    if status == "completed":
        result = d.get("result")
        tail = ""
        if isinstance(result, str) and result:
            tail = f"  →  {_truncate(result.splitlines()[0], 100)}"
        return f"  └─ {name}({arg_str}) ok{tail}"
    return f"  └─ {name}({arg_str}) {status}"


def _render_belief_mismatch(e: dict) -> str:
    d = e.get("data", {})
    ms = d.get("mismatches", [])
    parts: list[str] = []
    for m in ms:
        field = m.get("field")
        if field == "inventory":
            changes = m.get("changes", [])
            parts.append(
                "inv:[" + ", ".join(f"{c['item']} b={c['belief']} a={c['actual']}" for c in changes) + "]"
            )
        else:
            parts.append(f"{field} Δ={m.get('delta')}")
    return "belief_mismatch  " + "; ".join(parts)


def _render_exception(e: dict) -> str:
    d = e.get("data", {})
    return f"exception {d.get('stage')} {d.get('exc')}: {_truncate(d.get('message', ''), 160)}"


def _render(events: Iterable[dict]) -> str:
    lines: list[str] = []
    for e in events:
        ts = _fmt_ts(e.get("ts", 0))
        kind = e.get("event")
        d = e.get("data", {})
        if kind == "session_open":
            lines.append(f"{ts}  == session {d.get('session_id')} ==")
        elif kind == "chat_in":
            lines.append(f"{ts}  ── chat from {d.get('username')}: {_truncate(d.get('message', ''), 140)}")
        elif kind == "claude_request":
            iteration = d.get("iteration")
            lines.append(f"{ts}    iter {iteration} → claude.send (msgs={d.get('message_count')})")
        elif kind == "claude_response":
            lines.append(f"{ts}    iter {d.get('iteration')} ← {_render_claude_response(e)}")
        elif kind == "tool_dispatch":
            lines.append(f"{ts}      {_render_tool_dispatch(e)}")
        elif kind == "subaction":
            # Only render completed/failed states; started is just noise.
            if e.get("data", {}).get("status") in ("completed", "failed"):
                lines.append(f"{ts}    {_render_subaction(e)}")
        elif kind == "chat_out":
            lines.append(f"{ts}  → reply: {_truncate(d.get('text', ''), 200)}")
        elif kind == "belief_mismatch":
            lines.append(f"{ts}  !! {_render_belief_mismatch(e)}")
        elif kind == "exception":
            lines.append(f"{ts}  XX {_render_exception(e)}")
    return "\n".join(lines)


def _latest_session() -> Path:
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"No sessions found in {SESSIONS_DIR}", file=sys.stderr)
        sys.exit(2)
    return files[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="path to session JSONL")
    parser.add_argument("--latest", action="store_true", help="use newest session in state/sessions/")
    args = parser.parse_args()

    if args.latest or not args.path:
        path = _latest_session()
    else:
        path = Path(args.path)

    if not path.exists():
        print(f"Session file not found: {path}", file=sys.stderr)
        sys.exit(2)

    events = _load_events(path)
    print(f"# {path}  ({len(events)} events)\n")
    print(_render(events))


if __name__ == "__main__":
    main()
