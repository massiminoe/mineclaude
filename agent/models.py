"""Typed return shapes shared across the runtime + MCP surface.

These are plain data holders — no behaviour. The Runtime methods return them
and the MCP server renders them to tool results. Kept dependency-free so both
`runtime.py` and `mcp_server.py` can import them without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# An execute() outcome:
#   completed — the action ran and returned
#   failed    — the sandbox/code raised
#   cancelled — a reflex or interrupt() preempted the slot mid-run
#   busy      — the single-flight slot was already held (concurrent execute)
#   timeout   — the action exceeded its timeout
ExecuteStatus = Literal["completed", "failed", "cancelled", "busy", "timeout"]

HandlerSource = Literal["default", "authored"]


@dataclass
class ExecuteResult:
    """Result of Runtime.execute() — one action through the single-flight slot."""

    status: ExecuteStatus
    action_id: str
    duration_s: float
    result: str | None = None  # stringified return value + [Log] tail
    error: str | None = None


@dataclass
class GameState:
    """Structured snapshot returned by Runtime.get_state().

    `player`/`inventory`/`equipped`/`action`/`reflexes_recent` are a live
    snapshot (always fresh). `events` is the flushable buffer — drained when
    get_state(flush=True). `events_truncated` is True if the buffer overflowed
    since the last flush, so a silent drop can't masquerade as "quiet".
    """

    player: dict[str, Any]
    inventory: list[dict[str, Any]]
    equipped: dict[str, Any]
    action: dict[str, Any]
    reflexes_recent: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    events_truncated: bool = False


@dataclass
class Screenshot:
    """A captured frame. `image_base64` is what the bridge returns and what an
    MCP image block consumes — no decode/re-encode round trip."""

    image_base64: str
    format: str = "jpeg"  # jpeg | png
    width: int | None = None
    height: int | None = None
    yaw: float | None = None
    pitch: float | None = None


@dataclass
class HandlerInfo:
    """The current reaction handler for an event type. `code` is None for the
    native default handlers (lava/drowning/damage); a string for authored ones."""

    event_type: str
    source: HandlerSource
    preempts: bool
    cooldown_s: float
    code: str | None = None


@dataclass
class Event:
    """A realtime event off the bridge WS — the unit in the get_state buffer and
    the payload a handler body receives as `data` (here wrapped with type/ts)."""

    type: str
    data: dict[str, Any]
    ts: float
