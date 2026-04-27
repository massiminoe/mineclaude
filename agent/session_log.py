"""Structured per-session JSONL log for agent replay and diagnostics.

One SessionLogger per session_id writes append-only events to
`state/sessions/<YYYYMMDD-HHMMSS>-<short_id>.jsonl`. Events are JSON objects
with `ts`, `session_id`, `event`, and `data` fields.

Event types emitted by the agent loop:
  - chat_in         user message, game state snapshot
  - claude_request  iteration, message count
  - claude_response stop_reason, text/tool_use summary
  - tool_dispatch   name, input, result, elapsed_ms
  - chat_out        message sent back to the player
  - exception       stage, exc type, traceback
  - belief_mismatch divergence between agent belief and live bridge state

The log is the primary artifact for post-hoc diagnosis — it must capture
enough structure to reconstruct what Claude saw at every decision point.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETAIN = 50
DEFAULT_BASE_DIR = Path("state/sessions")


class SessionLogger:
    """Thread-safe JSONL writer for one session."""

    def __init__(
        self,
        session_id: str,
        base_dir: str | Path = DEFAULT_BASE_DIR,
        retain: int = DEFAULT_RETAIN,
    ):
        self.session_id = session_id
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        short_id = session_id.split("-")[0] if "-" in session_id else session_id[:8]
        self.path = self.base_dir / f"{timestamp}-{short_id}.jsonl"
        self._lock = threading.Lock()
        self._retain = retain
        self._prune_old()
        self.emit("session_open", session_id=session_id)

    def emit(self, event: str, **data: Any) -> None:
        entry = {
            "ts": time.time(),
            "session_id": self.session_id,
            "event": event,
            "data": data,
        }
        try:
            line = json.dumps(entry, default=_safe_default)
        except Exception as e:
            logger.warning(f"session_log: serialize failed for {event}: {e}")
            return
        with self._lock:
            try:
                with self.path.open("a") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.warning(f"session_log: write failed: {e}")

    def _prune_old(self) -> None:
        try:
            files = sorted(
                self.base_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in files[self._retain:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            pass


def _safe_default(obj: Any) -> Any:
    try:
        return repr(obj)
    except Exception:
        return "<unserializable>"
