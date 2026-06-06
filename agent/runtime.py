"""The headless runtime — the body the MCP server (and, transitionally, the
Claude-loop Agent) drives.

This module will grow to own the bridge handle, primitive namespace, the
single-flight executor, the reflex registry, and the event buffer (see
docs/superpowers/specs/2026-06-06-mcp-runtime-extraction.md). For now it
defines the `Controller` seam: the minimal surface a reflex handler needs from
its host. `reflexes.py` depends on this protocol (type-only) instead of on the
`Agent` class, so the body can be lifted out from under the brain.
"""

from __future__ import annotations

from typing import Any, Protocol

from agent.bridge import BridgeClient


class Controller(Protocol):
    """What a reflex handler / the reflex registry needs from its host.

    Both the transitional `Agent` (via thin wrappers) and the future `Runtime`
    satisfy this structurally. `resume()` replaces the brain-specific
    `_stage_resume`: under `Agent` it wakes the Claude loop; under `Runtime` it
    appends a notable event to the buffer for the next `get_state`.
    """

    bridge: BridgeClient

    async def preempt(self) -> None:
        """Acquire/clear the action slot: purge the queue + stop the bridge.

        Must NOT cancel the handler task that calls it (handlers run on their
        own task), so a conditional-interrupt handler survives its own call.
        """
        ...

    def resume(self, event_type: str) -> None:
        """Signal that a reflex handler finished and the host should react."""
        ...

    def slog(self, event: str, **data: Any) -> None:
        """Structured session-log entry."""
        ...

    async def emit_event(self, event: str, data: Any = None) -> None:
        """Emit on the monitor/event bus (e.g. ``reflex:fired``)."""
        ...
