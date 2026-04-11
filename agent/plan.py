"""Persistent plan document — read/write helpers for ./state/plan.md.

The plan file is freeform markdown with no schema. It is re-read from disk
each turn and injected into Claude's context via a synthetic tool_use /
tool_result pair (see agent.py). Claude modifies it via the `writePlan` tool.
"""

from __future__ import annotations

import os
from pathlib import Path

PLAN_PATH = Path("state/plan.md")


def read_plan() -> str:
    """Read the plan file. Returns empty string if missing or unreadable."""
    try:
        return PLAN_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def write_plan(content: str) -> int:
    """Atomically write the plan file. Creates parent dir if needed.

    Returns the number of lines in the new content (0 for empty).
    """
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAN_PATH.with_name(PLAN_PATH.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, PLAN_PATH)
    return len(content.splitlines())
