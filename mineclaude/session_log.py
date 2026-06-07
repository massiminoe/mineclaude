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

The log is the primary artifact for post-hoc diagnosis — it must capture
enough structure to reconstruct what Claude saw at every decision point.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RETAIN = 50
DEFAULT_BASE_DIR = Path("state/sessions")
IMAGES_DIRNAME = "images"


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
        self.stem = f"{timestamp}-{short_id}"
        self.path = self.base_dir / f"{self.stem}.jsonl"
        self.images_dir = self.base_dir / IMAGES_DIRNAME / self.stem
        self._lock = threading.Lock()
        self._retain = retain
        self._prune_old()
        self.emit("session_open", session_id=session_id)

    def save_image(self, tool_use_id: str, b64_data: str, fmt: str = "jpeg") -> str | None:
        """Persist a base64 tool-result image alongside the JSONL log.

        Returns a relative path string (under base_dir) for indexing in events.
        Failures are swallowed — image archival is best-effort and must not
        break the agent loop.
        """
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            ext = "jpg" if fmt.lower() in ("jpeg", "jpg") else fmt.lower()
            # Sanitize tool_use_id (Anthropic ids are alnum + underscore, but be paranoid).
            safe_id = "".join(c for c in tool_use_id if c.isalnum() or c in "_-")[:80]
            if not safe_id:
                safe_id = f"img_{int(time.time() * 1000)}"
            path = self.images_dir / f"{safe_id}.{ext}"
            path.write_bytes(base64.b64decode(b64_data))
            return f"{IMAGES_DIRNAME}/{self.stem}/{path.name}"
        except Exception as e:
            logger.warning(f"session_log: image save failed: {e}")
            return None

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
                # Drop the matching images dir if present.
                img_dir = self.base_dir / IMAGES_DIRNAME / old.stem
                if img_dir.is_dir():
                    shutil.rmtree(img_dir, ignore_errors=True)
        except Exception:
            pass


def _safe_default(obj: Any) -> Any:
    try:
        return repr(obj)
    except Exception:
        return "<unserializable>"
