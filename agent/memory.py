"""Persistent memory document — read/write helpers for ./state/memory.md.

Memory is plain markdown that outlives any single goal — Claude structures it
however it likes; there's no enforced schema. It is re-read from disk each turn
and injected into Claude's context via a synthetic tool_use / tool_result pair
(see agent.py). Claude modifies it via the `writeMemory` tool.
"""

from __future__ import annotations

import os
from pathlib import Path

MEMORY_PATH = Path("state/memory.md")


def read_memory() -> str:
    """Read the memory file. Returns empty string if missing or unreadable."""
    try:
        return MEMORY_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def write_memory(content: str) -> int:
    """Atomically write the memory file. Creates parent dir if needed.

    Returns the number of lines in the new content (0 for empty).
    """
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_PATH.with_name(MEMORY_PATH.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, MEMORY_PATH)
    return len(content.splitlines())
