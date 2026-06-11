"""Tests for primitives against mock bridge."""

import pytest

from mineclaude.bridge import BridgeResponse, MockBridgeClient
from mineclaude.primitives import _check, make_primitives


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
    result = await prims["goToPosition"](100, 200, y=64)
    # Reports the achieved position + a real move, not just "ok".
    assert "Walked to" in result
    assert "100" in result and "200" in result
    assert bridge._position == {"x": 100, "y": 64, "z": 200}


@pytest.mark.asyncio
async def test_go_to_position_no_op_is_flagged(bridge, prims):
    # Target already where the bot stands (mock starts at 0,64,0): the response
    # must say it did NOT move, so a no-op can't read as a successful walk.
    result = await prims["goToPosition"](0, 0, y=64)
    assert "Did not move" in result
    assert bridge._position == {"x": 0, "y": 64, "z": 0}


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
async def test_equip_tracks_equipped_state(bridge, prims):
    """equip() updates the mock's equipped block so get_status reflects what's
    held/worn — the contract get_state().equipped reads (was all-null before)."""
    bridge._add_to_inventory("iron_pickaxe", 1)
    bridge._add_to_inventory("iron_helmet", 1)

    await prims["equip"]("iron_pickaxe")            # default slot = hand
    await prims["equip"]("iron_helmet", "head")

    status = (await bridge.get_status()).data
    assert status["equipped"]["hand"] == "iron_pickaxe"
    assert status["equipped"]["head"] == "iron_helmet"
    assert status["equipped"]["chest"] is None
    assert "held_slot" in status


@pytest.mark.asyncio
async def test_equip_mainhand_alias_maps_to_hand(bridge, prims):
    """'mainhand' is an alias for 'hand' — both land in equipped['hand']."""
    bridge._add_to_inventory("stone_pickaxe", 1)
    await prims["equip"]("stone_pickaxe", "mainhand")
    status = (await bridge.get_status()).data
    assert status["equipped"]["hand"] == "stone_pickaxe"


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
async def test_get_blocks_batch(bridge, prims):
    # One known solid cell (dirt at 0,63,0), one empty cell -> air.
    result = await prims["getBlocks"]([(0, 63, 0), (50, 70, 50)])
    assert [b["block"] for b in result] == ["dirt", "air"]
    # Order preserved, coords echoed, shape matches getBlock.
    assert result[0] == {"x": 0, "y": 63, "z": 0, "block": "dirt", "replaceable": False}
    assert result[1] == {"x": 50, "y": 70, "z": 50, "block": "air", "replaceable": True}


@pytest.mark.asyncio
async def test_get_blocks_empty(bridge, prims):
    assert await prims["getBlocks"]([]) == []


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
        await prims["craft"]("totally_made_up_item", 1)


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
async def test_craft_torch_from_charcoal(bridge, prims):
    # Torch recipe wants "coal" but charcoal is interchangeable (vanilla's
    # minecraft:coals tag). 1 coal/charcoal + 1 stick -> 4 torches, no table.
    bridge._add_to_inventory("charcoal", 1)
    bridge._add_to_inventory("stick", 1)
    result = await prims["craft"]("torch", 4)
    assert "Crafted" in result
    assert any(i["name"] == "torch" and i["count"] == 4 for i in bridge._inventory)
    assert not any(i["name"] == "charcoal" for i in bridge._inventory)


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
async def test_craft_oak_planks_rejects_foreign_log(bridge, prims):
    # Regression: crafting a variant-specific output (oak_planks) must NOT
    # consume a foreign log (acacia_log). Pre-fix, the oak_log ingredient was
    # in VARIANT_SUFFIXES and matched any *_log, so this silently produced the
    # wrong wood — the real bridge then tripped its output-slot guard with
    # "Output slot showed 'acacia_planks', expected 'oak_planks'".
    bridge._add_to_inventory("acacia_log", 56)
    with pytest.raises(RuntimeError, match="missing"):
        await prims["craft"]("oak_planks", 4)
    # Foreign log untouched on the failed (pre-flight) craft.
    assert any(i["name"] == "acacia_log" and i["count"] == 56 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_planks_prefers_matching_log(bridge, prims):
    # When both the matching and a foreign log are present, the exact wood is
    # consumed and the foreign log is left alone.
    bridge._add_to_inventory("oak_log", 1)
    bridge._add_to_inventory("acacia_log", 56)
    result = await prims["craft"]("oak_planks", 4)
    assert "Crafted" in result
    assert not any(i["name"] == "oak_log" for i in bridge._inventory)
    assert any(i["name"] == "acacia_log" and i["count"] == 56 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_craft_oak_stairs_rejects_foreign_planks(bridge, prims):
    # Same bug class one level up: a typed output (oak_stairs) must require its
    # own planks exactly, not any *_planks.
    bridge._nearby_blocks.append({"name": "crafting_table", "x": 1, "y": 64, "z": 1, "distance": 1.4})
    bridge._add_to_inventory("spruce_planks", 64)
    with pytest.raises(RuntimeError, match="missing"):
        await prims["craft"]("oak_stairs", 4)
    assert any(i["name"] == "spruce_planks" and i["count"] == 64 for i in bridge._inventory)


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
async def test_chest_store_take_round_trip(bridge, prims):
    """Store dumps inventory into the chest; take pulls it back."""
    bridge._add_to_inventory("cobblestone", 64)
    bridge._add_to_inventory("dirt", 20)
    bridge._nearby_blocks.append({"name": "chest", "x": 5, "y": 64, "z": 5, "distance": 5.0})

    # Tuple form, mixed "all" + explicit count.
    result = await prims["chestStore"](5, 64, 5, [("cobblestone", "all"), ("dirt", 10)])
    assert {"item": "cobblestone", "count": 64} in result["stored"]
    assert {"item": "dirt", "count": 10} in result["stored"]
    # Inventory drained per spec.
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "cobblestone") == 0
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "dirt") == 10

    # Inspect should show what's in there.
    state = await prims["chestInspect"](5, 64, 5)
    assert state["totals"]["cobblestone"] == 64
    assert state["totals"]["dirt"] == 10

    # Take half the cobble + all the dirt back.
    result = await prims["chestTake"](5, 64, 5, [("cobblestone", 30), ("dirt", "all")])
    assert {"item": "cobblestone", "count": 30} in result["taken"]
    assert {"item": "dirt", "count": 10} in result["taken"]
    # Inventory regained those.
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "cobblestone") == 30
    assert sum(i["count"] for i in bridge._inventory if i["name"] == "dirt") == 20
    # Chest still has the leftover cobble.
    state = await prims["chestInspect"](5, 64, 5)
    assert state["totals"].get("cobblestone") == 34
    assert "dirt" not in state["totals"]


@pytest.mark.asyncio
async def test_chest_store_skipped_when_item_missing(bridge, prims):
    """Items not in inventory are reported as skipped rather than failing."""
    bridge._add_to_inventory("cobblestone", 5)
    bridge._nearby_blocks.append({"name": "chest", "x": 5, "y": 64, "z": 5, "distance": 5.0})

    result = await prims["chestStore"](5, 64, 5, [("cobblestone", "all"), ("diamond", 1)])
    assert {"item": "cobblestone", "count": 5} in result["stored"]
    skipped_items = [s["item"] for s in result["skipped"]]
    assert "diamond" in skipped_items


@pytest.mark.asyncio
async def test_chest_inspect_rejects_non_chest(bridge, prims):
    bridge._nearby_blocks.append({"name": "furnace", "x": 5, "y": 64, "z": 5, "distance": 5.0})
    with pytest.raises(RuntimeError, match="not a chest"):
        await prims["chestInspect"](5, 64, 5)


@pytest.mark.asyncio
async def test_chest_dict_and_string_input_forms(bridge, prims):
    """Sandbox accepts dict entries and bare-string entries (== 'all')."""
    bridge._add_to_inventory("oak_log", 3)
    bridge._add_to_inventory("acacia_log", 2)
    bridge._nearby_blocks.append({"name": "chest", "x": 5, "y": 64, "z": 5, "distance": 5.0})

    # Dict form for one, bare-string for the other.
    result = await prims["chestStore"](5, 64, 5, [{"name": "oak_log", "count": 2}, "acacia_log"])
    assert {"item": "oak_log", "count": 2} in result["stored"]
    assert {"item": "acacia_log", "count": 2} in result["stored"]


@pytest.mark.asyncio
async def test_log(prims):
    from mineclaude.primitives import _log_buffer
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


@pytest.mark.asyncio
async def test_attack_loops_to_kill(bridge):
    """Mock bridge mirrors the real /attack: loop swings until the target
    is dead, then return killed + swing count."""
    bridge._nearby_entities.append({
        "name": "pig", "type": "pig", "x": 1, "y": 64, "z": 1, "distance": 1.4, "health": 10,
    })
    resp = await bridge.attack("pig")
    assert resp.status == "success"
    assert resp.data["reason"] == "killed"
    assert resp.data["swings"] == 2  # 10 hp / 5-per-swing
    assert resp.data["attacked"] is True
    # Entity removed after kill.
    assert all(e["name"] != "pig" for e in bridge._nearby_entities)


@pytest.mark.asyncio
async def test_attack_not_found_returns_error(bridge):
    resp = await bridge.attack("nonexistent_mob")
    assert resp.status == "error"
    assert resp.data["reason"] == "not_found"
    assert resp.data["swings"] == 0


@pytest.mark.asyncio
async def test_attack_stop_cancels_in_flight_loop(bridge):
    """attack_stop sets the cancel flag; an in-flight attack returns
    cancelled at the next loop boundary. Swing count depends on how many
    iterations completed before the stop landed — could be 0 if the cancel
    arrives before the first swing tick."""
    bridge._nearby_entities.append({
        "name": "iron_golem", "type": "iron_golem", "x": 1, "y": 64, "z": 1,
        "distance": 1.4, "health": 100,
    })
    import asyncio as _asyncio
    task = _asyncio.create_task(bridge.attack("iron_golem"))
    # Yield enough for at least one swing to land before cancel.
    for _ in range(3):
        await _asyncio.sleep(0)
    stop_resp = await bridge.attack_stop()
    assert stop_resp.data["cancelled"] is True
    resp = await task
    assert resp.data["reason"] == "cancelled"
    # Golem still alive (health 100, 5/swing → ≤20 swings) → not killed.
    assert resp.data["swings"] < 20


# --- unified use() primitive ------------------------------------------------

@pytest.mark.asyncio
async def test_use_eats_food_in_air(bridge, prims):
    """use(item) with no look_at is a pure in-air use: food consumes one and
    reports the inventory delta."""
    bridge._add_to_inventory("bread", 3)
    result = await prims["use"]("bread")
    assert result["used"] is True
    assert result["dispatch"] == "item"
    assert result["inventory_delta"] == {"bread": -1}
    assert any(i["name"] == "bread" and i["count"] == 2 for i in bridge._inventory)


@pytest.mark.asyncio
async def test_use_block_dispatch_on_aim(bridge, prims):
    """look_at landing on a known block dispatches the block branch and
    reports the hit."""
    # mock seeds a grass_block at (1, 64, 0)
    bridge._add_to_inventory("torch", 5)
    result = await prims["use"]("torch", look_at=(1.5, 64.5, 0.5))
    assert result["dispatch"] == "block"
    assert result["used"] is True
    assert result["hit"]["block"] == "grass_block"
    assert result["hit"]["x"] == 1 and result["hit"]["y"] == 64 and result["hit"]["z"] == 0


@pytest.mark.asyncio
async def test_use_falls_through_to_item_when_aim_misses(bridge, prims):
    """look_at over empty space → no block → item-use fall-through (vanilla
    doItemUse semantics). Food still eats."""
    bridge._add_to_inventory("bread", 1)
    result = await prims["use"]("bread", look_at=(50.0, 64.0, 50.0))
    assert result["dispatch"] == "item"
    assert result["used"] is True
    assert result["inventory_delta"] == {"bread": -1}


@pytest.mark.asyncio
async def test_use_missing_item_raises(bridge, prims):
    """Equipping an item we don't have is a hard error."""
    with pytest.raises(RuntimeError, match="No diamond_sword in inventory"):
        await prims["use"]("diamond_sword")


@pytest.mark.asyncio
async def test_use_empty_hand_over_air_is_noop(bridge, prims):
    """No item, no block hit → used: False (not an exception)."""
    result = await prims["use"](look_at=(50.0, 64.0, 50.0))
    assert result["used"] is False
    assert result["dispatch"] == "item"


@pytest.mark.asyncio
async def test_use_bridge_strips_minecraft_prefix(bridge):
    """Bridge-level: namespaced item id is normalized."""
    bridge._add_to_inventory("apple", 2)
    resp = await bridge.use("minecraft:apple")
    assert resp.status == "success"
    assert resp.data["item"] == "apple"
    assert resp.data["used"] is True
