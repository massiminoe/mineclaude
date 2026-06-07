#!/usr/bin/env python3
"""Emit a human-readable timeline from an agent session JSONL log.

Usage:
    python scripts/session_report.py state/sessions/<session>.jsonl
    python scripts/session_report.py --latest

The report summarizes each execute() action — its code, sub-step timeline, and
outcome — interleaved with inbound world events (chat/death/respawn/hazards) and
handler installs. Invoke when diagnosing a reliability regression — it saves you
from tailing raw JSONL.
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


def _code_preview(code: str) -> str:
    """First non-blank line of the submitted code, with a tail count if multi-line."""
    lines = [ln for ln in (code or "").splitlines() if ln.strip()]
    if not lines:
        return "<empty>"
    head = _truncate(lines[0].strip(), 120)
    extra = len(lines) - 1
    return f"{head}  (+{extra} more line{'s' if extra != 1 else ''})" if extra else head


def _render_execute_done(d: dict) -> str:
    status = d.get("status")
    dur = d.get("duration_s")
    detail = ""
    if status in ("failed", "timeout", "cancelled"):
        detail = f"  {_truncate(str(d.get('error') or ''), 140)}"
    else:
        result = d.get("result")
        if isinstance(result, str) and result.strip():
            detail = f"  →  {_truncate(result.splitlines()[0], 120)}"
    return f"execute #{d.get('action_id')} {status} ({dur}s){detail}"


def _render_event(d: dict) -> str:
    data = d.get("data") or {}
    preview = _truncate(json.dumps(data, default=str), 120) if data else ""
    return f"event {d.get('type')}  {preview}".rstrip()


def _render_handler_set(d: dict) -> str:
    return (
        f"handler_set {d.get('event_type')} "
        f"preempts={d.get('preempts')} cooldown={d.get('cooldown_s')}s"
    )


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


def _render(events: Iterable[dict]) -> str:
    lines: list[str] = []
    for e in events:
        ts = _fmt_ts(e.get("ts", 0))
        kind = e.get("event")
        d = e.get("data", {})
        if kind == "session_open":
            lines.append(f"{ts}  == session {d.get('session_id')} ==")
        elif kind == "execute_start":
            lines.append(f"{ts}  → execute #{d.get('action_id')}  {_code_preview(d.get('code', ''))}")
        elif kind == "subaction":
            # Only render completed/failed states; started is just noise.
            if d.get("status") in ("completed", "failed"):
                lines.append(f"{ts}    {_render_subaction(e)}")
        elif kind == "execute_done":
            lines.append(f"{ts}  ← {_render_execute_done(d)}")
        elif kind == "execute_rejected":
            lines.append(f"{ts}  XX execute rejected ({d.get('reason')})")
        elif kind == "event":
            lines.append(f"{ts}  ·· {_render_event(d)}")
        elif kind == "handler_set":
            lines.append(f"{ts}  :: {_render_handler_set(d)}")
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
