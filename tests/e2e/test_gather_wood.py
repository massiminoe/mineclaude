"""E2E: ask the agent to gather wood and verify it actually ends up in inventory.

This is the canonical smoke test — if this passes, the full stack is
working end-to-end. It covers: Claude call, tool dispatch, Baritone mine,
block cache invalidation, inventory cache refresh, and the session log.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import pytest_asyncio


pytestmark = pytest.mark.e2e


async def _inventory_count(bridge_url: str, item_substring: str) -> int:
    """Count how many of a given item are in the player's inventory."""
    async with httpx.AsyncClient(base_url=bridge_url, timeout=10.0) as client:
        resp = await client.get("/status")
    inv = resp.json().get("data", {}).get("inventory", []) or []
    return sum(
        entry.get("count", 0)
        for entry in inv
        if item_substring in (entry.get("name") or entry.get("item") or "")
    )


async def test_agent_gathers_wood(scenario):
    """Ask the bot to collect a log; assert it appears in inventory."""
    before = await _inventory_count("http://localhost:8080", "log")
    result = await scenario.say("tester", "please get one log", timeout_s=300.0)

    assert result.text_sent, "agent did not reply"
    assert result.iterations >= 1, "no Claude iterations recorded"

    # Give Baritone a moment to finish and the cache to refresh.
    await asyncio.sleep(5.0)
    after = await _inventory_count("http://localhost:8080", "log")
    assert after > before, (
        f"expected log count to increase; before={before} after={after}. "
        f"Session log: {result.session_log_path}"
    )

    # Sanity-check the session log format.
    events = scenario.read_session_log()
    event_types = {e.get("event") for e in events}
    assert "chat_in" in event_types
    assert "claude_request" in event_types
    assert "claude_response" in event_types
