"""Tests for bridge.blocks — replaceable-block classification."""

from bridge.blocks import is_replaceable, normalize_block_id, REPLACEABLE_BLOCKS


def test_normalize_strips_namespace():
    assert normalize_block_id("minecraft:short_grass") == "short_grass"


def test_normalize_strips_blockstate():
    assert normalize_block_id("minecraft:grass_block[snowy=false]") == "grass_block"


def test_normalize_strips_both():
    assert normalize_block_id("minecraft:snow[layers=1]") == "snow"


def test_normalize_no_prefix():
    assert normalize_block_id("short_grass[]") == "short_grass"


def test_air_is_replaceable():
    assert is_replaceable("minecraft:air")
    assert is_replaceable("minecraft:cave_air")
    assert is_replaceable("air")


def test_short_grass_is_replaceable():
    """The exact case the user described — grass texture sticking out of the ground."""
    assert is_replaceable("minecraft:short_grass")
    assert is_replaceable("minecraft:short_grass[snowy=false]")


def test_flowers_are_replaceable():
    assert is_replaceable("minecraft:dandelion")
    assert is_replaceable("minecraft:poppy")
    assert is_replaceable("minecraft:oxeye_daisy")


def test_snow_layer_is_replaceable():
    """Single-layer snow only; we accept all `snow` here per design choice."""
    assert is_replaceable("minecraft:snow")
    assert is_replaceable("minecraft:snow[layers=1]")
    # snow_block is the full cube — NOT replaceable
    assert not is_replaceable("minecraft:snow_block")


def test_grass_block_is_not_replaceable():
    """grass_block is the full dirt+grass cube — placing on it should fail."""
    assert not is_replaceable("minecraft:grass_block")
    assert not is_replaceable("minecraft:grass_block[snowy=false]")


def test_solid_blocks_are_not_replaceable():
    assert not is_replaceable("minecraft:stone")
    assert not is_replaceable("minecraft:dirt")
    assert not is_replaceable("minecraft:oak_log")
    assert not is_replaceable("minecraft:cobblestone")
    assert not is_replaceable("minecraft:crafting_table")


def test_water_lava_replaceable():
    assert is_replaceable("minecraft:water")
    assert is_replaceable("minecraft:lava")


def test_unknown_block_is_not_replaceable():
    """Conservative default for unrecognized blocks."""
    assert not is_replaceable("minecraft:some_modded_block")


def test_none_is_replaceable():
    """getblock failure → optimistic, let press_use try."""
    assert is_replaceable(None)
    assert is_replaceable("")


def test_replaceable_set_has_air_variants():
    """Sanity: all three air variants must be in the set."""
    assert "air" in REPLACEABLE_BLOCKS
    assert "cave_air" in REPLACEABLE_BLOCKS
    assert "void_air" in REPLACEABLE_BLOCKS
