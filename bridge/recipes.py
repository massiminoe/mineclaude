"""Crafting and smelting recipe tables for essential survival items.

Each crafting recipe maps an output item to its crafting pattern and ingredients.
Patterns use single-char keys mapped to item names.

Slot layout for 3x3 crafting table:
  1 2 3
  4 5 6
  7 8 9

Slot layout for 2x2 inventory crafting:
  1 2
  3 4

Smelting recipes map output → input item. Require a placed furnace within 4 blocks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Recipe:
    output: str
    output_count: int
    pattern: list[str]  # rows of the pattern, e.g. ["##", "##"]
    key: dict[str, str]  # char → minecraft item name
    needs_table: bool  # True if requires 3x3 crafting table


# Pattern convention: space = empty slot, char = ingredient key
RECIPES: dict[str, Recipe] = {
    # --- Wood processing ---
    "oak_planks": Recipe(
        output="oak_planks", output_count=4,
        pattern=["#"],
        key={"#": "oak_log"},
        needs_table=False,
    ),
    "spruce_planks": Recipe(
        output="spruce_planks", output_count=4,
        pattern=["#"],
        key={"#": "spruce_log"},
        needs_table=False,
    ),
    "birch_planks": Recipe(
        output="birch_planks", output_count=4,
        pattern=["#"],
        key={"#": "birch_log"},
        needs_table=False,
    ),
    "stick": Recipe(
        output="stick", output_count=4,
        pattern=["#", "#"],
        key={"#": "oak_planks"},
        needs_table=False,
    ),

    # --- Basic blocks ---
    "crafting_table": Recipe(
        output="crafting_table", output_count=1,
        pattern=["##", "##"],
        key={"#": "oak_planks"},
        needs_table=False,
    ),
    "furnace": Recipe(
        output="furnace", output_count=1,
        pattern=["###", "# #", "###"],
        key={"#": "cobblestone"},
        needs_table=True,
    ),
    "chest": Recipe(
        output="chest", output_count=1,
        pattern=["###", "# #", "###"],
        key={"#": "oak_planks"},
        needs_table=True,
    ),

    # --- Torches ---
    "torch": Recipe(
        output="torch", output_count=4,
        pattern=["C", "S"],
        key={"C": "coal", "S": "stick"},
        needs_table=False,
    ),

    # --- Wooden tools ---
    "wooden_pickaxe": Recipe(
        output="wooden_pickaxe", output_count=1,
        pattern=["###", " S ", " S "],
        key={"#": "oak_planks", "S": "stick"},
        needs_table=True,
    ),
    "wooden_axe": Recipe(
        output="wooden_axe", output_count=1,
        pattern=["##", "#S", " S"],
        key={"#": "oak_planks", "S": "stick"},
        needs_table=True,
    ),
    "wooden_shovel": Recipe(
        output="wooden_shovel", output_count=1,
        pattern=["#", "S", "S"],
        key={"#": "oak_planks", "S": "stick"},
        needs_table=True,
    ),
    "wooden_sword": Recipe(
        output="wooden_sword", output_count=1,
        pattern=["#", "#", "S"],
        key={"#": "oak_planks", "S": "stick"},
        needs_table=True,
    ),
    "wooden_hoe": Recipe(
        output="wooden_hoe", output_count=1,
        pattern=["##", " S", " S"],
        key={"#": "oak_planks", "S": "stick"},
        needs_table=True,
    ),

    # --- Stone tools ---
    "stone_pickaxe": Recipe(
        output="stone_pickaxe", output_count=1,
        pattern=["###", " S ", " S "],
        key={"#": "cobblestone", "S": "stick"},
        needs_table=True,
    ),
    "stone_axe": Recipe(
        output="stone_axe", output_count=1,
        pattern=["##", "#S", " S"],
        key={"#": "cobblestone", "S": "stick"},
        needs_table=True,
    ),
    "stone_shovel": Recipe(
        output="stone_shovel", output_count=1,
        pattern=["#", "S", "S"],
        key={"#": "cobblestone", "S": "stick"},
        needs_table=True,
    ),
    "stone_sword": Recipe(
        output="stone_sword", output_count=1,
        pattern=["#", "#", "S"],
        key={"#": "cobblestone", "S": "stick"},
        needs_table=True,
    ),

    # --- Iron tools ---
    "iron_pickaxe": Recipe(
        output="iron_pickaxe", output_count=1,
        pattern=["###", " S ", " S "],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),
    "iron_axe": Recipe(
        output="iron_axe", output_count=1,
        pattern=["##", "#S", " S"],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),
    "iron_shovel": Recipe(
        output="iron_shovel", output_count=1,
        pattern=["#", "S", "S"],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),
    "iron_sword": Recipe(
        output="iron_sword", output_count=1,
        pattern=["#", "#", "S"],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),

    # --- Armor ---
    "iron_helmet": Recipe(
        output="iron_helmet", output_count=1,
        pattern=["###", "# #"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "iron_chestplate": Recipe(
        output="iron_chestplate", output_count=1,
        pattern=["# #", "###", "###"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "iron_leggings": Recipe(
        output="iron_leggings", output_count=1,
        pattern=["###", "# #", "# #"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "iron_boots": Recipe(
        output="iron_boots", output_count=1,
        pattern=["# #", "# #"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),

    # --- Misc ---
    "bucket": Recipe(
        output="bucket", output_count=1,
        pattern=["# #", " # "],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "ladder": Recipe(
        output="ladder", output_count=3,
        pattern=["# #", "###", "# #"],
        key={"#": "stick"},
        needs_table=True,
    ),
    "bowl": Recipe(
        output="bowl", output_count=4,
        pattern=["# #", " # "],
        key={"#": "oak_planks"},
        needs_table=True,
    ),
}


# Ingredients that accept any variant with the same suffix.
# e.g. recipe says "oak_planks" but any *_planks works (matches real MC behavior).
VARIANT_SUFFIXES: dict[str, str] = {
    "oak_planks": "_planks",
    "oak_log": "_log",
}


def _matches_ingredient(required: str, available: str) -> bool:
    """Check if an inventory item can satisfy a required ingredient."""
    if required == available:
        return True
    suffix = VARIANT_SUFFIXES.get(required)
    return suffix is not None and available.endswith(suffix)


def resolve_ingredients(
    required: dict[str, int], inventory: dict[str, int]
) -> dict[str, int] | None:
    """Map required (canonical) ingredients to actual inventory items.

    Returns {actual_item: count_to_consume} or None if insufficient.
    """
    resolved: dict[str, int] = {}
    for ingredient, needed in required.items():
        remaining = needed
        for inv_item, inv_count in inventory.items():
            if _matches_ingredient(ingredient, inv_item) and remaining > 0:
                already_claimed = resolved.get(inv_item, 0)
                available = inv_count - already_claimed
                if available <= 0:
                    continue
                take = min(available, remaining)
                resolved[inv_item] = already_claimed + take
                remaining -= take
        if remaining > 0:
            return None
    return resolved


def get_recipe(item: str) -> Recipe | None:
    """Look up a recipe by item name (with or without minecraft: prefix)."""
    item = item.replace("minecraft:", "")
    return RECIPES.get(item)


def get_required_ingredients(item: str, count: int = 1) -> dict[str, int] | None:
    """Calculate total ingredients needed to craft `count` of an item.

    Returns {ingredient_name: total_count} or None if recipe unknown.
    """
    recipe = get_recipe(item)
    if recipe is None:
        return None

    # Count how many crafts needed
    crafts_needed = math.ceil(count / recipe.output_count)

    # Count ingredients per craft from pattern
    ingredient_counts: dict[str, int] = {}
    for row in recipe.pattern:
        for ch in row:
            if ch != " " and ch in recipe.key:
                ing = recipe.key[ch]
                ingredient_counts[ing] = ingredient_counts.get(ing, 0) + 1

    # Scale by number of crafts
    return {k: v * crafts_needed for k, v in ingredient_counts.items()}


def pattern_to_slots(recipe: Recipe) -> dict[int, str]:
    """Convert a recipe pattern to {slot_number: ingredient_item}.

    For 2x2 patterns (no table): slots 1-4
    For 3x3 patterns (table): slots 1-9
    """
    slots: dict[int, str] = {}
    if recipe.needs_table:
        # Pad pattern to 3 rows of 3
        rows = [row.ljust(3) for row in recipe.pattern]
        while len(rows) < 3:
            rows.append("   ")
        for r, row in enumerate(rows):
            for c, ch in enumerate(row[:3]):
                if ch != " " and ch in recipe.key:
                    slot = r * 3 + c + 1  # 1-indexed
                    slots[slot] = recipe.key[ch]
    else:
        # Pad pattern to 2 rows of 2
        rows = [row.ljust(2) for row in recipe.pattern]
        while len(rows) < 2:
            rows.append("  ")
        for r, row in enumerate(rows):
            for c, ch in enumerate(row[:2]):
                if ch != " " and ch in recipe.key:
                    slot = r * 2 + c + 1  # 1-indexed
                    slots[slot] = recipe.key[ch]
    return slots


# ---------------------------------------------------------------------------
# Smelting recipes
# ---------------------------------------------------------------------------


@dataclass
class SmeltingRecipe:
    output: str
    input: str
    output_count: int = 1


# Inputs that accept any variant with the same suffix (e.g. any _log → charcoal).
SMELTING_VARIANT_SUFFIXES: dict[str, str] = {
    "oak_log": "_log",
}


SMELTING_RECIPES: dict[str, SmeltingRecipe] = {
    # --- Ores ---
    "iron_ingot": SmeltingRecipe(output="iron_ingot", input="raw_iron"),
    "gold_ingot": SmeltingRecipe(output="gold_ingot", input="raw_gold"),
    "copper_ingot": SmeltingRecipe(output="copper_ingot", input="raw_copper"),
    # --- Blocks ---
    "glass": SmeltingRecipe(output="glass", input="sand"),
    "stone": SmeltingRecipe(output="stone", input="cobblestone"),
    "smooth_stone": SmeltingRecipe(output="smooth_stone", input="stone"),
    "brick": SmeltingRecipe(output="brick", input="clay_ball"),
    "nether_brick": SmeltingRecipe(output="nether_brick", input="netherrack"),
    # --- Charcoal ---
    "charcoal": SmeltingRecipe(output="charcoal", input="oak_log"),  # any _log variant
    # --- Food ---
    "cooked_beef": SmeltingRecipe(output="cooked_beef", input="beef"),
    "cooked_porkchop": SmeltingRecipe(output="cooked_porkchop", input="porkchop"),
    "cooked_chicken": SmeltingRecipe(output="cooked_chicken", input="chicken"),
    "cooked_mutton": SmeltingRecipe(output="cooked_mutton", input="mutton"),
    "cooked_cod": SmeltingRecipe(output="cooked_cod", input="cod"),
    "cooked_salmon": SmeltingRecipe(output="cooked_salmon", input="salmon"),
    "dried_kelp": SmeltingRecipe(output="dried_kelp", input="kelp"),
}

# Fuel values: how many items one unit of fuel can smelt.
FUEL_VALUES: dict[str, float] = {
    "coal": 8,
    "charcoal": 8,
    "coal_block": 80,
    "lava_bucket": 100,
    "blaze_rod": 12,
    "stick": 0.5,
    # Variants handled by suffix matching below
    "oak_planks": 1.5,
    "oak_log": 1.5,
}

# Fuel items that accept any variant with the same suffix.
FUEL_VARIANT_SUFFIXES: dict[str, str] = {
    "oak_planks": "_planks",
    "oak_log": "_log",
}


def _matches_smelting_input(required: str, available: str) -> bool:
    """Check if an inventory item can satisfy a smelting input."""
    if required == available:
        return True
    suffix = SMELTING_VARIANT_SUFFIXES.get(required)
    return suffix is not None and available.endswith(suffix)


def get_fuel_value(item: str) -> float:
    """Return how many items one unit of this fuel can smelt, or 0 if not fuel."""
    item = item.replace("minecraft:", "")
    if item in FUEL_VALUES:
        return FUEL_VALUES[item]
    # Check variant suffixes
    for canonical, suffix in FUEL_VARIANT_SUFFIXES.items():
        if item.endswith(suffix) and canonical in FUEL_VALUES:
            return FUEL_VALUES[canonical]
    return 0


def get_smelting_recipe(item: str) -> SmeltingRecipe | None:
    """Look up a smelting recipe by output item name."""
    item = item.replace("minecraft:", "")
    return SMELTING_RECIPES.get(item)


