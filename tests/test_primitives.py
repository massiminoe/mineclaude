"""Tests for primitives against mock bridge."""

import pytest

from agent.bridge import BridgeResponse, MockBridgeClient
from agent.primitives import _check, make_primitives


def test_check_returns_message_on_success():
    assert _check(BridgeResponse("success", "did the thing")) == "did the thing"


def test_check_raises_on_error():
    with pytest.raises(RuntimeError, match="nope"):
        _check(BridgeResponse("error", "nope"))


def test_check_prefixes_partial():
    """Partial successes must be surfaced so Claude sees '[partial]' in tool results."""
    assert _check(BridgeResponse("partial", "crafted 5 of 10")) == "[partial] crafted 5 of 10"


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
    bridge._nearby_blocks.append({"name": "crafting_table", "x": 1, "y": 64, "z": 1, "distance": 1.4})
    with pytest.raises(RuntimeError, match="missing"):
        await prims["craft"]("iron_pickaxe", 1)


@pytest.mark.asyncio
async def test_craft_partial_ingredients(bridge, prims):
    bridge._nearby_blocks.append({"name": "crafting_table", "x": 1, "y": 64, "z": 1, "distance": 1.4})
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
    bridge._nearby_blocks.append({"name": "crafting_table", "x": 1, "y": 64, "z": 1, "distance": 1.4})
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
async def test_craft_3x3_fails_without_table(bridge, prims):
    bridge._add_to_inventory("cobblestone", 8)
    bridge._add_to_inventory("stick", 2)
    with pytest.raises(RuntimeError, match="crafting table"):
        await prims["craft"]("stone_pickaxe", 1)
    # Ingredients untouched
    assert any(i["name"] == "cobblestone" and i["count"] == 8 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_2x2_works_without_table(bridge, prims):
    bridge._add_to_inventory("oak_log", 1)
    result = await prims["craft"]("oak_planks", 4)
    assert "Crafted" in result


@pytest.mark.asyncio
async def test_break_block(bridge, prims):
    result = await prims["breakBlockAt"](1, 64, 0)
    assert "Broke" in result


@pytest.mark.asyncio
async def test_break_and_collect(bridge, prims):
    """Break a block, then collect the dropped item — mimics real gameplay flow."""
    # Break creates a dropped item entity (not auto-added to inventory)
    inv_before = await prims["getInventory"]()
    grass_before = sum(i["count"] for i in inv_before if i["name"] == "grass_block")

    await prims["breakBlockAt"](1, 64, 0)  # grass_block at (1, 64, 0)

    # Item should NOT be in inventory yet — it's a dropped entity
    inv_mid = await prims["getInventory"]()
    grass_mid = sum(i["count"] for i in inv_mid if i["name"] == "grass_block")
    assert grass_mid == grass_before

    # collectItems vacuums up nearby drops (no coordinates needed)
    result = await prims["collectItems"]()
    assert "Collected" in result

    inv_after = await prims["getInventory"]()
    grass_after = sum(i["count"] for i in inv_after if i["name"] == "grass_block")
    assert grass_after == grass_before + 1


@pytest.mark.asyncio
async def test_collect_no_items_is_success(bridge, prims):
    """collectItems is idempotent — returns success when nothing to pick up."""
    # No item entities anywhere; should not raise
    result = await prims["collectItems"]()
    assert "No items" in result


@pytest.mark.asyncio
async def test_furnace_load_extract_round_trip(bridge, prims):
    """Pure load/extract pair: caller specifies exact input + fuel; bridge
    pulls those amounts from inventory; mock simulates smelting; extract
    returns the output and any leftovers."""
    bridge._add_to_inventory("raw_iron", 5)
    bridge._add_to_inventory("birch_planks", 4)
    bridge._nearby_blocks.append({"name": "furnace", "x": 2, "y": 64, "z": 0, "distance": 2.0})

    # Load 3 raw_iron + 2 planks (covers 3 items @ 1.5 per plank).
    await prims["furnaceLoad"]("raw_iron", 3, "birch_planks", 2)
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "raw_iron") == 2
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "birch_planks") == 2

    # Inspect should report the in-flight contents (mock simulates instant cook).
    state = await prims["furnaceInspect"]()
    assert state["output"]["item"] == "iron_ingot"

    # Extract returns output + remainders; inventory regains the iron + leftovers.
    result = await prims["furnaceExtract"]()
    assert result["output"]["item"] == "iron_ingot"
    assert result["output"]["count"] == 3
    assert any(i["name"] == "iron_ingot" and i["count"] == 3 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_furnace_load_no_furnace(bridge, prims):
    bridge._add_to_inventory("raw_iron", 5)
    bridge._add_to_inventory("coal", 1)
    with pytest.raises(RuntimeError, match="furnace"):
        await prims["furnaceLoad"]("raw_iron", 5, "coal", 1)


@pytest.mark.asyncio
async def test_furnace_load_short_input(bridge, prims):
    """Load atomically: short input rejects without consuming fuel."""
    bridge._add_to_inventory("raw_iron", 1)
    bridge._add_to_inventory("coal", 5)
    bridge._nearby_blocks.append({"name": "furnace", "x": 2, "y": 64, "z": 0, "distance": 2.0})
    with pytest.raises(RuntimeError, match="raw_iron"):
        await prims["furnaceLoad"]("raw_iron", 5, "coal", 1)
    # Fuel must NOT have been consumed.
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "coal") == 5


@pytest.mark.asyncio
async def test_furnace_load_short_fuel_refunds_input(bridge, prims):
    """Load atomically: short fuel must refund any input we already pulled."""
    bridge._add_to_inventory("raw_iron", 5)
    bridge._add_to_inventory("coal", 0)
    bridge._nearby_blocks.append({"name": "furnace", "x": 2, "y": 64, "z": 0, "distance": 2.0})
    with pytest.raises(RuntimeError, match="coal"):
        await prims["furnaceLoad"]("raw_iron", 5, "coal", 1)
    # Input must NOT have been consumed since fuel check failed.
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "raw_iron") == 5


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
