"""Crafting and smelting recipe tables for essential survival items.

Used by the agent to validate craft requests pre-flight (ingredient
availability, table requirement). The native bridge mod has its own
authoritative recipe table in `mc-mod/.../Recipes.kt`; keep both in
sync when adding recipes.

Each recipe maps an output item to its crafting pattern and ingredients.
Patterns use single-char keys mapped to item names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Recipe:
    output: str
    output_count: int
    pattern: list[str]  # rows of the pattern, e.g. ["##", "##"]
    key: dict[str, str]  # char -> minecraft item name
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


# ---------------------------------------------------------------------------
# Smelting recipes
# ---------------------------------------------------------------------------


@dataclass
class SmeltingRecipe:
    output: str
    input: str
    output_count: int = 1


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


def get_smelting_recipe(item: str) -> SmeltingRecipe | None:
    """Look up a smelting recipe by output item name."""
    item = item.replace("minecraft:", "")
    return SMELTING_RECIPES.get(item)


def get_smelting_by_input(input_item: str) -> SmeltingRecipe | None:
    """Look up a smelting recipe by INPUT item name (mock-only convenience)."""
    input_item = input_item.replace("minecraft:", "")
    for recipe in SMELTING_RECIPES.values():
        if recipe.input == input_item:
            return recipe
        # _log variants — recipe input is "oak_log", any _log smelts to charcoal.
        if recipe.input.endswith("_log") and input_item.endswith("_log"):
            return recipe
    return None
