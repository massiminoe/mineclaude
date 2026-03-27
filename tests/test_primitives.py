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
async def test_find_multiple_blocks(bridge, prims):
    result = await prims["findMultipleBlocks"](["oak_log", "dirt"], 64, 10)
    assert isinstance(result, dict)
    assert set(result.keys()) == {"oak_log", "dirt"}
    assert all(b["name"] == "oak_log" for b in result["oak_log"])
    assert all(b["name"] == "dirt" for b in result["dirt"])
    assert len(result["oak_log"]) == 3  # mock has 3 oak_logs
    assert len(result["dirt"]) == 1


@pytest.mark.asyncio
async def test_find_multiple_blocks_missing_type(bridge, prims):
    result = await prims["findMultipleBlocks"](["diamond_ore", "emerald_ore"], 64, 10)
    assert result == {"diamond_ore": [], "emerald_ore": []}


@pytest.mark.asyncio
async def test_craft(bridge, prims):
    bridge._add_to_inventory("oak_log", 1)
    result = await prims["craft"]("oak_planks", 4)
    assert "Crafted" in result
    # 1 log -> 4 planks
    assert any(i["name"] == "oak_planks" and i["count"] == 4 for i in bridge._inventory)
    # log consumed
    assert not any(i["name"] == "oak_log" for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_missing_ingredients(bridge, prims):
    with pytest.raises(RuntimeError, match="missing"):
        await prims["craft"]("iron_pickaxe", 1)


@pytest.mark.asyncio
async def test_craft_partial_ingredients(bridge, prims):
    bridge._add_to_inventory("iron_ingot", 2)  # need 3
    bridge._add_to_inventory("stick", 2)
    with pytest.raises(RuntimeError, match="missing"):
        await prims["craft"]("iron_pickaxe", 1)
    # Ingredients should NOT be consumed on failure
    assert any(i["name"] == "iron_ingot" and i["count"] == 2 for i in bridge._inventory)
    assert any(i["name"] == "stick" and i["count"] == 2 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_unknown_recipe(bridge, prims):
    with pytest.raises(RuntimeError, match="Unknown recipe"):
        await prims["craft"]("diamond_block", 1)


@pytest.mark.asyncio
async def test_craft_consumes_ingredients(bridge, prims):
    bridge._add_to_inventory("cobblestone", 8)
    bridge._add_to_inventory("stick", 2)
    result = await prims["craft"]("stone_pickaxe", 1)
    assert "Crafted" in result
    # 3 cobblestone + 2 sticks consumed
    cobble = next((i for i in bridge._inventory if i["name"] == "cobblestone"), None)
    assert cobble is not None and cobble["count"] == 5  # 8 - 3
    assert not any(i["name"] == "stick" for i in bridge._inventory)  # 2 - 2 = 0


@pytest.mark.asyncio
async def test_craft_multiple_batches(bridge, prims):
    bridge._add_to_inventory("oak_log", 2)
    result = await prims["craft"]("oak_planks", 5)
    # ceil(5/4) = 2 crafts, 2 logs consumed, 8 planks produced
    assert "Crafted" in result
    planks = next((i for i in bridge._inventory if i["name"] == "oak_planks"), None)
    assert planks is not None and planks["count"] == 8
    assert not any(i["name"] == "oak_log" for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_output_rounding(bridge, prims):
    bridge._add_to_inventory("oak_log", 1)
    result = await prims["craft"]("oak_planks", 1)
    # Requesting 1 plank still yields 4 (1 craft minimum)
    assert "Crafted" in result
    planks = next((i for i in bridge._inventory if i["name"] == "oak_planks"), None)
    assert planks is not None and planks["count"] == 4


@pytest.mark.asyncio
async def test_craft_spruce_planks_make_sticks(bridge, prims):
    bridge._add_to_inventory("spruce_planks", 4)
    result = await prims["craft"]("stick", 4)
    assert "Crafted" in result
    assert any(i["name"] == "stick" and i["count"] == 4 for i in bridge._inventory)
    # 2 spruce planks consumed (recipe uses 2 planks per craft)
    planks = next((i for i in bridge._inventory if i["name"] == "spruce_planks"), None)
    assert planks is not None and planks["count"] == 2


@pytest.mark.asyncio
async def test_craft_mixed_planks_for_crafting_table(bridge, prims):
    # crafting_table needs 4 planks — mix of types should work
    bridge._add_to_inventory("spruce_planks", 2)
    bridge._add_to_inventory("birch_planks", 2)
    result = await prims["craft"]("crafting_table", 1)
    assert "Crafted" in result
    assert any(i["name"] == "crafting_table" for i in bridge._inventory)
    assert not any(i["name"] in ("spruce_planks", "birch_planks") for i in bridge._inventory)


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
