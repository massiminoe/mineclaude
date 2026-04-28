"""Routing tests for the agent's native-vs-legacy bridge selection.

The native Fabric mod (port 8081) progressively takes over endpoints from
the Minescript-backed Python bridge (port 8080). `agent.bridge.NATIVE_ENDPOINTS`
controls the routing — these tests verify the selection logic in isolation
so cutover bugs surface here, not in a live MC session.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import agent.bridge as bridge_mod
from agent.bridge import RealBridgeClient


@pytest.fixture
def client() -> RealBridgeClient:
    return RealBridgeClient(
        base_url="http://legacy.example:8080",
        native_url="http://native.example:8081",
    )


def test_unrouted_endpoint_uses_legacy(client: RealBridgeClient):
    with patch.object(bridge_mod, "NATIVE_ENDPOINTS", frozenset()):
        assert client._client_for("/status") is client._http
        assert client._client_for("/place") is client._http


def test_routed_endpoint_uses_native(client: RealBridgeClient):
    with patch.object(bridge_mod, "NATIVE_ENDPOINTS", frozenset({"/status"})):
        assert client._client_for("/status") is client._native_http
        # Other endpoints stay on legacy until explicitly added
        assert client._client_for("/place") is client._http


def test_phase2b_routing_includes_equip_and_discard():
    """Phase 2b lands the inventory-move helper (clickSlot SWAP / PICKUP)
    so /equip armor + non-hotbar /discard work natively without the
    legacy /item replace shuffle. All Phase 2 + 2b writes now route."""
    assert "/chat" in bridge_mod.NATIVE_ENDPOINTS
    assert "/equip" in bridge_mod.NATIVE_ENDPOINTS
    assert "/discard" in bridge_mod.NATIVE_ENDPOINTS


def test_phase3_routing_includes_world_mutations():
    """Phase 3 ports break/place/attack to native interactionManager calls."""
    assert "/break" in bridge_mod.NATIVE_ENDPOINTS
    assert "/place" in bridge_mod.NATIVE_ENDPOINTS
    assert "/attack" in bridge_mod.NATIVE_ENDPOINTS


def test_phase4_routing_includes_craft_and_furnace():
    """Phase 4 ports craft + the /furnace/* trio to native clickSlot.
    With these on native, no endpoint leaves a stale ScreenHandler open
    across bridges, so the EquipRoute cross-bridge sync barrier was
    retired. The /furnace/* trio replaced the old /smelt endpoint:
    load (insert input + fuel, no wait), inspect (read state), extract
    (pull all three slots back)."""
    assert "/craft" in bridge_mod.NATIVE_ENDPOINTS
    assert "/furnace/load" in bridge_mod.NATIVE_ENDPOINTS
    assert "/furnace/inspect" in bridge_mod.NATIVE_ENDPOINTS
    assert "/furnace/extract" in bridge_mod.NATIVE_ENDPOINTS
    assert "/smelt" not in bridge_mod.NATIVE_ENDPOINTS


def test_phase5_routing_includes_movement():
    """Phase 5 ports the Baritone-driven movement endpoints. Native impls
    send the same `#…` chat strings as legacy via the in-process tick
    thread; /goto polls player position directly; /collect runs the walk
    loop in Kotlin against world.entities."""
    for endpoint in ("/goto", "/mine", "/follow", "/stop", "/explore", "/collect"):
        assert endpoint in bridge_mod.NATIVE_ENDPOINTS, f"{endpoint} missing"


def test_phase6_ws_url_uses_native_when_provided():
    """Phase 6 binary-cuts events WS over to the native mod's dedicated
    listener (ws://…:8082/events). Per-endpoint routing doesn't apply
    here — `/events` is a single subscription, so we just flip ws_url."""
    client = RealBridgeClient(
        base_url="http://legacy.example:8080",
        native_url="http://native.example:8081",
        native_ws_url="ws://native.example:8082/events",
    )
    assert client.ws_url == "ws://native.example:8082/events"


def test_phase6_ws_url_falls_back_to_legacy_when_native_ws_disabled():
    """Setting BRIDGE_WS_URL_NATIVE='' (None on the constructor) must
    fall back to deriving the WS URL from base_url, so the agent keeps
    working when the native mod is being rebuilt."""
    client = RealBridgeClient(
        base_url="http://legacy.example:8080",
        native_url="http://native.example:8081",
        native_ws_url=None,
    )
    assert client.ws_url == "ws://legacy.example:8080/events"


def test_no_native_url_falls_back_to_legacy():
    """Disabling the native bridge (e.g. while the mod is rebuilt) must keep
    the agent working end-to-end against the legacy bridge alone."""
    legacy_only = RealBridgeClient(base_url="http://legacy.example:8080", native_url=None)
    with patch.object(bridge_mod, "NATIVE_ENDPOINTS", frozenset({"/status"})):
        # Even though /status is in NATIVE_ENDPOINTS, native_http is None
        # so we must fall back to legacy rather than crash.
        assert legacy_only._client_for("/status") is legacy_only._http


@pytest.mark.asyncio
async def test_close_releases_both_clients(client: RealBridgeClient):
    await client.close()
    assert client._http.is_closed
    assert client._native_http is not None and client._native_http.is_closed
