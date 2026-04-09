"""Tests for bridge.recipes slot-mapping helpers."""

import pytest

from bridge.recipes import (
    Recipe,
    get_recipe,
    pattern_to_inventory_slots,
    pattern_to_table_slots,
)


# ---------------------------------------------------------------------------
# pattern_to_table_slots — always 3x3 numbering, top-left positioning for 2x2
# ---------------------------------------------------------------------------


def test_table_slots_wooden_pickaxe():
    """3x3 recipe maps row-major: row 0 = slots 1-3, row 1 = 4-6, row 2 = 7-9."""
    recipe = get_recipe("wooden_pickaxe")
    slots = pattern_to_table_slots(recipe)
    assert slots == {
        1: "oak_planks", 2: "oak_planks", 3: "oak_planks",
        5: "stick",
        8: "stick",
    }


def test_table_slots_iron_chestplate():
    """Pattern with internal gaps: ['# #', '###', '###'] -> 1,3,4,5,6,7,8,9."""
    recipe = get_recipe("iron_chestplate")
    slots = pattern_to_table_slots(recipe)
    assert slots == {
        1: "iron_ingot", 3: "iron_ingot",
        4: "iron_ingot", 5: "iron_ingot", 6: "iron_ingot",
        7: "iron_ingot", 8: "iron_ingot", 9: "iron_ingot",
    }


def test_table_slots_2x2_recipe_uses_top_left():
    """A 2x2 pattern (e.g. crafting_table) lands in slots 1, 2, 4, 5 of the 3x3."""
    recipe = get_recipe("crafting_table")
    slots = pattern_to_table_slots(recipe)
    assert slots == {1: "oak_planks", 2: "oak_planks", 4: "oak_planks", 5: "oak_planks"}


def test_table_slots_1x2_stick():
    """Stick recipe is 1 column x 2 rows: ['#', '#'] -> slots 1 and 4."""
    recipe = get_recipe("stick")
    slots = pattern_to_table_slots(recipe)
    assert slots == {1: "oak_planks", 4: "oak_planks"}


def test_table_slots_1x1_planks():
    """oak_planks is a single cell: ['#'] -> slot 1."""
    recipe = get_recipe("oak_planks")
    slots = pattern_to_table_slots(recipe)
    assert slots == {1: "oak_log"}


def test_table_slots_1x2_torch():
    """Torch is ['C', 'S'] -> slot 1 = coal, slot 4 = stick."""
    recipe = get_recipe("torch")
    slots = pattern_to_table_slots(recipe)
    assert slots == {1: "coal", 4: "stick"}


def test_table_slots_pattern_too_large_raises():
    """A pattern with width > 3 or height > 3 is invalid for the 3x3 grid."""
    bad = Recipe(
        output="bogus", output_count=1,
        pattern=["####"],  # 4 wide
        key={"#": "stone"},
        needs_table=True,
    )
    with pytest.raises(ValueError, match="too large"):
        pattern_to_table_slots(bad)

    bad2 = Recipe(
        output="bogus", output_count=1,
        pattern=["#", "#", "#", "#"],  # 4 tall
        key={"#": "stone"},
        needs_table=True,
    )
    with pytest.raises(ValueError, match="too large"):
        pattern_to_table_slots(bad2)


# ---------------------------------------------------------------------------
# pattern_to_inventory_slots — 2x2 numbering for player inventory crafter
# ---------------------------------------------------------------------------


def test_inventory_slots_crafting_table():
    """2x2 recipe in 2x2 player crafter: ['##','##'] -> slots 1, 2, 3, 4."""
    recipe = get_recipe("crafting_table")
    slots = pattern_to_inventory_slots(recipe)
    assert slots == {1: "oak_planks", 2: "oak_planks", 3: "oak_planks", 4: "oak_planks"}


def test_inventory_slots_planks_single():
    """oak_planks (1x1) in 2x2 crafter -> slot 1."""
    recipe = get_recipe("oak_planks")
    slots = pattern_to_inventory_slots(recipe)
    assert slots == {1: "oak_log"}


def test_inventory_slots_stick():
    """stick is ['#', '#'] -> slots 1 and 3 (column 0 of two rows)."""
    recipe = get_recipe("stick")
    slots = pattern_to_inventory_slots(recipe)
    assert slots == {1: "oak_planks", 3: "oak_planks"}


def test_inventory_slots_torch():
    """torch is ['C', 'S'] -> slot 1 = coal, slot 3 = stick (1 col x 2 rows)."""
    recipe = get_recipe("torch")
    slots = pattern_to_inventory_slots(recipe)
    assert slots == {1: "coal", 3: "stick"}


def test_inventory_slots_3x3_recipe_raises():
    """3x3 recipes don't fit the 2x2 inventory crafter — must error."""
    recipe = get_recipe("wooden_pickaxe")
    with pytest.raises(ValueError, match="3x3 crafting table"):
        pattern_to_inventory_slots(recipe)
