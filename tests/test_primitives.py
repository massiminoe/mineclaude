"""Tests for primitives against mock bridge."""

import pytest

from agent.bridge import MockBridgeClient
from agent.primitives import make_primitives


@pytest.fixture
def bridge():
    return MockBridgeClient()


@pytest.fixture
def prims(bridge):
    return make_primitives(bridge)


@pytest.mark.asyncio
async def test_go_to_position(bridge, prims):
    result = await prims["goToPosition"](100, 64, 200)
    assert "Moved to" in result
    assert bridge._position == {"x": 100, "y": 64, "z": 200}


@pytest.mark.asyncio
async def test_get_stats(bridge, prims):
    stats = await prims["getStats"]()
    assert stats["health"] == 20.0
    assert stats["hunger"] == 20
    assert "position" in stats


@pytest.mark.asyncio
async def test_get_inventory(bridge, prims):
    inv = await prims["getInventory"]()
    assert isinstance(inv, list)


@pytest.mark.asyncio
async def test_get_nearby_blocks(bridge, prims):
    blocks = await prims["getNearbyBlocks"](16)
    assert len(blocks) > 0
    assert all("name" in b for b in blocks)


@pytest.mark.asyncio
async def test_get_nearby_entities(bridge, prims):
    entities = await prims["getNearbyEntities"](32)
    assert len(entities) > 0


@pytest.mark.asyncio
async def test_find_blocks(bridge, prims):
    blocks = await prims["findBlocks"]("oak_log", 64, 10)
    assert all(b["name"] == "oak_log" for b in blocks)


@pytest.mark.asyncio
async def test_craft(bridge, prims):
    result = await prims["craft"]("oak_planks", 4)
    assert "Crafted" in result
    assert any(i["name"] == "oak_planks" for i in bridge._inventory)


@pytest.mark.asyncio
async def test_break_block(bridge, prims):
    result = await prims["breakBlockAt"](1, 64, 0)
    assert "Broke" in result


@pytest.mark.asyncio
async def test_log(prims):
    from agent.primitives import _log_buffer
    _log_buffer.clear()
    prims["log"]("test message")
    assert "test message" in _log_buffer


@pytest.mark.asyncio
async def test_sleep(prims):
    import time
    start = time.monotonic()
    await prims["sleep"](0.05)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04
