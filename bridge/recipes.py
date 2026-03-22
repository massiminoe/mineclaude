"""Crafting recipe table for essential survival items.

Each recipe maps an output item to its crafting pattern and ingredients.
Patterns use single-char keys mapped to item names.

Slot layout for 3x3 crafting table:
  1 2 3
  4 5 6
  7 8 9

Slot layout for 2x2 inventory crafting:
  1 2
  3 4
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


