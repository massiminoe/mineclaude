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
    """Phase 3 ports break/place/attack to native interactionManager calls.
    /collect stays legacy until the Baritone-driven walk loop is ported."""
    assert "/break" in bridge_mod.NATIVE_ENDPOINTS
    assert "/place" in bridge_mod.NATIVE_ENDPOINTS
    assert "/attack" in bridge_mod.NATIVE_ENDPOINTS
    assert "/collect" not in bridge_mod.NATIVE_ENDPOINTS


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
