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
    "jungle_planks": Recipe(
        output="jungle_planks", output_count=4,
        pattern=["#"],
        key={"#": "jungle_log"},
        needs_table=False,
    ),
    "acacia_planks": Recipe(
        output="acacia_planks", output_count=4,
        pattern=["#"],
        key={"#": "acacia_log"},
        needs_table=False,
    ),
    "dark_oak_planks": Recipe(
        output="dark_oak_planks", output_count=4,
        pattern=["#"],
        key={"#": "dark_oak_log"},
        needs_table=False,
    ),
    "mangrove_planks": Recipe(
        output="mangrove_planks", output_count=4,
        pattern=["#"],
        key={"#": "mangrove_log"},
        needs_table=False,
    ),
    "cherry_planks": Recipe(
        output="cherry_planks", output_count=4,
        pattern=["#"],
        key={"#": "cherry_log"},
        needs_table=False,
    ),
    "crimson_planks": Recipe(
        output="crimson_planks", output_count=4,
        pattern=["#"],
        key={"#": "crimson_stem"},
        needs_table=False,
    ),
    "warped_planks": Recipe(
        output="warped_planks", output_count=4,
        pattern=["#"],
        key={"#": "warped_stem"},
        needs_table=False,
    ),
    "bamboo_planks": Recipe(
        output="bamboo_planks", output_count=2,
        pattern=["#"],
        key={"#": "bamboo_block"},
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
    "stone_hoe": Recipe(
        output="stone_hoe", output_count=1,
        pattern=["##", " S", " S"],
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
    "iron_hoe": Recipe(
        output="iron_hoe", output_count=1,
        pattern=["##", " S", " S"],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),

    # --- Diamond tools ---
    "diamond_pickaxe": Recipe(
        output="diamond_pickaxe", output_count=1,
        pattern=["###", " S ", " S "],
        key={"#": "diamond", "S": "stick"},
        needs_table=True,
    ),
    "diamond_axe": Recipe(
        output="diamond_axe", output_count=1,
        pattern=["##", "#S", " S"],
        key={"#": "diamond", "S": "stick"},
        needs_table=True,
    ),
    "diamond_shovel": Recipe(
        output="diamond_shovel", output_count=1,
        pattern=["#", "S", "S"],
        key={"#": "diamond", "S": "stick"},
        needs_table=True,
    ),
    "diamond_sword": Recipe(
        output="diamond_sword", output_count=1,
        pattern=["#", "#", "S"],
        key={"#": "diamond", "S": "stick"},
        needs_table=True,
    ),
    "diamond_hoe": Recipe(
        output="diamond_hoe", output_count=1,
        pattern=["##", " S", " S"],
        key={"#": "diamond", "S": "stick"},
        needs_table=True,
    ),

    # --- Golden tools ---
    "golden_pickaxe": Recipe(
        output="golden_pickaxe", output_count=1,
        pattern=["###", " S ", " S "],
        key={"#": "gold_ingot", "S": "stick"},
        needs_table=True,
    ),
    "golden_axe": Recipe(
        output="golden_axe", output_count=1,
        pattern=["##", "#S", " S"],
        key={"#": "gold_ingot", "S": "stick"},
        needs_table=True,
    ),
    "golden_shovel": Recipe(
        output="golden_shovel", output_count=1,
        pattern=["#", "S", "S"],
        key={"#": "gold_ingot", "S": "stick"},
        needs_table=True,
    ),
    "golden_sword": Recipe(
        output="golden_sword", output_count=1,
        pattern=["#", "#", "S"],
        key={"#": "gold_ingot", "S": "stick"},
        needs_table=True,
    ),
    "golden_hoe": Recipe(
        output="golden_hoe", output_count=1,
        pattern=["##", " S", " S"],
        key={"#": "gold_ingot", "S": "stick"},
        needs_table=True,
    ),

    # --- Iron armor ---
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

    # --- Diamond armor ---
    "diamond_helmet": Recipe(
        output="diamond_helmet", output_count=1,
        pattern=["###", "# #"],
        key={"#": "diamond"},
        needs_table=True,
    ),
    "diamond_chestplate": Recipe(
        output="diamond_chestplate", output_count=1,
        pattern=["# #", "###", "###"],
        key={"#": "diamond"},
        needs_table=True,
    ),
    "diamond_leggings": Recipe(
        output="diamond_leggings", output_count=1,
        pattern=["###", "# #", "# #"],
        key={"#": "diamond"},
        needs_table=True,
    ),
    "diamond_boots": Recipe(
        output="diamond_boots", output_count=1,
        pattern=["# #", "# #"],
        key={"#": "diamond"},
        needs_table=True,
    ),

    # --- Golden armor ---
    "golden_helmet": Recipe(
        output="golden_helmet", output_count=1,
        pattern=["###", "# #"],
        key={"#": "gold_ingot"},
        needs_table=True,
    ),
    "golden_chestplate": Recipe(
        output="golden_chestplate", output_count=1,
        pattern=["# #", "###", "###"],
        key={"#": "gold_ingot"},
        needs_table=True,
    ),
    "golden_leggings": Recipe(
        output="golden_leggings", output_count=1,
        pattern=["###", "# #", "# #"],
        key={"#": "gold_ingot"},
        needs_table=True,
    ),
    "golden_boots": Recipe(
        output="golden_boots", output_count=1,
        pattern=["# #", "# #"],
        key={"#": "gold_ingot"},
        needs_table=True,
    ),

    # --- Leather armor ---
    "leather_helmet": Recipe(
        output="leather_helmet", output_count=1,
        pattern=["###", "# #"],
        key={"#": "leather"},
        needs_table=True,
    ),
    "leather_chestplate": Recipe(
        output="leather_chestplate", output_count=1,
        pattern=["# #", "###", "###"],
        key={"#": "leather"},
        needs_table=True,
    ),
    "leather_leggings": Recipe(
        output="leather_leggings", output_count=1,
        pattern=["###", "# #", "# #"],
        key={"#": "leather"},
        needs_table=True,
    ),
    "leather_boots": Recipe(
        output="leather_boots", output_count=1,
        pattern=["# #", "# #"],
        key={"#": "leather"},
        needs_table=True,
    ),

    # --- Storage blocks (compress 9 → 1) ---
    "iron_block": Recipe(
        output="iron_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "gold_block": Recipe(
        output="gold_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "gold_ingot"},
        needs_table=True,
    ),
    "diamond_block": Recipe(
        output="diamond_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "diamond"},
        needs_table=True,
    ),
    "redstone_block": Recipe(
        output="redstone_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "redstone"},
        needs_table=True,
    ),
    "emerald_block": Recipe(
        output="emerald_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "emerald"},
        needs_table=True,
    ),
    "lapis_block": Recipe(
        output="lapis_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "lapis_lazuli"},
        needs_table=True,
    ),
    "coal_block": Recipe(
        output="coal_block", output_count=1,
        pattern=["###", "###", "###"],
        key={"#": "coal"},
        needs_table=True,
    ),

    # --- Survival essentials ---
    # `bed` aliases white_bed. Other-colored beds need the matching wool color.
    "bed": Recipe(
        output="white_bed", output_count=1,
        pattern=["WWW", "###"],
        key={"W": "white_wool", "#": "oak_planks"},
        needs_table=True,
    ),
    "white_bed": Recipe(
        output="white_bed", output_count=1,
        pattern=["WWW", "###"],
        key={"W": "white_wool", "#": "oak_planks"},
        needs_table=True,
    ),
    "shield": Recipe(
        output="shield", output_count=1,
        pattern=["#I#", "###", " # "],
        key={"#": "oak_planks", "I": "iron_ingot"},
        needs_table=True,
    ),
    "bread": Recipe(
        output="bread", output_count=1,
        pattern=["###"],
        key={"#": "wheat"},
        needs_table=True,
    ),
    "flint_and_steel": Recipe(
        output="flint_and_steel", output_count=1,
        pattern=["I ", " F"],
        key={"I": "iron_ingot", "F": "flint"},
        needs_table=False,
    ),
    "fishing_rod": Recipe(
        output="fishing_rod", output_count=1,
        pattern=["  #", " #X", "# X"],
        key={"#": "stick", "X": "string"},
        needs_table=True,
    ),
    "bow": Recipe(
        output="bow", output_count=1,
        pattern=[" #X", "# X", " #X"],
        key={"#": "stick", "X": "string"},
        needs_table=True,
    ),
    "arrow": Recipe(
        output="arrow", output_count=4,
        pattern=["F", "S", "T"],
        key={"F": "flint", "S": "stick", "T": "feather"},
        needs_table=True,
    ),
    "cake": Recipe(
        output="cake", output_count=1,
        pattern=["MMM", "SES", "WWW"],
        key={"M": "milk_bucket", "S": "sugar", "E": "egg", "W": "wheat"},
        needs_table=True,
    ),

    # --- Wool & decoration ---
    "white_wool": Recipe(
        output="white_wool", output_count=1,
        pattern=["##", "##"],
        key={"#": "string"},
        needs_table=False,
    ),
    "bricks": Recipe(
        output="bricks", output_count=1,
        pattern=["##", "##"],
        key={"#": "brick"},
        needs_table=False,
    ),
    "jack_o_lantern": Recipe(
        output="jack_o_lantern", output_count=1,
        pattern=["P", "T"],
        key={"P": "carved_pumpkin", "T": "torch"},
        needs_table=False,
    ),

    # --- Paper, books, navigation ---
    "paper": Recipe(
        output="paper", output_count=3,
        pattern=["###"],
        key={"#": "sugar_cane"},
        needs_table=True,
    ),
    "book": Recipe(
        output="book", output_count=1,
        pattern=["PP", "PL"],
        key={"P": "paper", "L": "leather"},
        needs_table=False,
    ),
    "bookshelf": Recipe(
        output="bookshelf", output_count=1,
        pattern=["###", "BBB", "###"],
        key={"#": "oak_planks", "B": "book"},
        needs_table=True,
    ),
    "compass": Recipe(
        output="compass", output_count=1,
        pattern=[" I ", "IRI", " I "],
        key={"I": "iron_ingot", "R": "redstone"},
        needs_table=True,
    ),
    "clock": Recipe(
        output="clock", output_count=1,
        pattern=[" G ", "GRG", " G "],
        key={"G": "gold_ingot", "R": "redstone"},
        needs_table=True,
    ),
    # `map` recipe yields filled_map (the empty/explorable version).
    "map": Recipe(
        output="filled_map", output_count=1,
        pattern=["PPP", "PCP", "PPP"],
        key={"P": "paper", "C": "compass"},
        needs_table=True,
    ),

    # --- Transport ---
    "oak_boat": Recipe(
        output="oak_boat", output_count=1,
        pattern=["# #", "###"],
        key={"#": "oak_planks"},
        needs_table=True,
    ),
    "minecart": Recipe(
        output="minecart", output_count=1,
        pattern=["# #", "###"],
        key={"#": "iron_ingot"},
        needs_table=True,
    ),
    "rail": Recipe(
        output="rail", output_count=16,
        pattern=["# #", "#S#", "# #"],
        key={"#": "iron_ingot", "S": "stick"},
        needs_table=True,
    ),

    # --- Redstone components ---
    "redstone_torch": Recipe(
        output="redstone_torch", output_count=1,
        pattern=["R", "S"],
        key={"R": "redstone", "S": "stick"},
        needs_table=False,
    ),
    "redstone_lamp": Recipe(
        output="redstone_lamp", output_count=1,
        pattern=[" R ", "RGR", " R "],
        key={"R": "redstone", "G": "glowstone"},
        needs_table=True,
    ),
    "repeater": Recipe(
        output="repeater", output_count=1,
        pattern=["TRT", "###"],
        key={"T": "redstone_torch", "R": "redstone", "#": "stone"},
        needs_table=True,
    ),

    # --- Utility blocks ---
    "hopper": Recipe(
        output="hopper", output_count=1,
        pattern=["I I", "ICI", " I "],
        key={"I": "iron_ingot", "C": "chest"},
        needs_table=True,
    ),
    "dispenser": Recipe(
        output="dispenser", output_count=1,
        pattern=["###", "#B#", "#R#"],
        key={"#": "cobblestone", "B": "bow", "R": "redstone"},
        needs_table=True,
    ),
    "dropper": Recipe(
        output="dropper", output_count=1,
        pattern=["###", "# #", "#R#"],
        key={"#": "cobblestone", "R": "redstone"},
        needs_table=True,
    ),
    "piston": Recipe(
        output="piston", output_count=1,
        pattern=["###", "CIC", "CRC"],
        key={"#": "oak_planks", "C": "cobblestone", "I": "iron_ingot", "R": "redstone"},
        needs_table=True,
    ),

    # --- Misc tools ---
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


# --- Wood-variant outputs: stairs, slabs, doors, trapdoors, fences, gates,
# buttons, pressure plates, signs, planks, boats. Generated per-wood-type so
# each variant uses its own plank ingredient (vanilla MC won't accept
# spruce_planks placed for an oak_door recipe). ---
_WOOD_TYPES: list[tuple[str, str, int, str | None]] = [
    # (name, log_or_stem, planks_per_craft, boat_output_or_None)
    ("oak", "oak_log", 4, "oak_boat"),
    ("spruce", "spruce_log", 4, "spruce_boat"),
    ("birch", "birch_log", 4, "birch_boat"),
    ("jungle", "jungle_log", 4, "jungle_boat"),
    ("acacia", "acacia_log", 4, "acacia_boat"),
    ("dark_oak", "dark_oak_log", 4, "dark_oak_boat"),
    ("mangrove", "mangrove_log", 4, "mangrove_boat"),
    ("cherry", "cherry_log", 4, "cherry_boat"),
    ("crimson", "crimson_stem", 4, None),
    ("warped", "warped_stem", 4, None),
    # bamboo: 1 bamboo_block → 2 bamboo_planks. Boat is bamboo_raft.
    ("bamboo", "bamboo_block", 2, "bamboo_raft"),
]

for _name, _log, _planks_n, _boat in _WOOD_TYPES:
    _planks = f"{_name}_planks"
    RECIPES[_planks] = Recipe(_planks, _planks_n, ["#"], {"#": _log}, False)
    RECIPES[f"{_name}_stairs"] = Recipe(f"{_name}_stairs", 4, ["#  ", "## ", "###"], {"#": _planks}, True)
    RECIPES[f"{_name}_slab"] = Recipe(f"{_name}_slab", 6, ["###"], {"#": _planks}, True)
    RECIPES[f"{_name}_door"] = Recipe(f"{_name}_door", 3, ["##", "##", "##"], {"#": _planks}, True)
    RECIPES[f"{_name}_trapdoor"] = Recipe(f"{_name}_trapdoor", 2, ["###", "###"], {"#": _planks}, True)
    RECIPES[f"{_name}_fence"] = Recipe(f"{_name}_fence", 3, ["#S#", "#S#"], {"#": _planks, "S": "stick"}, True)
    RECIPES[f"{_name}_fence_gate"] = Recipe(f"{_name}_fence_gate", 1, ["S#S", "S#S"], {"#": _planks, "S": "stick"}, True)
    RECIPES[f"{_name}_button"] = Recipe(f"{_name}_button", 1, ["#"], {"#": _planks}, False)
    RECIPES[f"{_name}_pressure_plate"] = Recipe(f"{_name}_pressure_plate", 1, ["##"], {"#": _planks}, True)
    RECIPES[f"{_name}_sign"] = Recipe(f"{_name}_sign", 3, ["###", "###", " S "], {"#": _planks, "S": "stick"}, True)
    if _boat is not None:
        RECIPES[_boat] = Recipe(_boat, 1, ["# #", "###"], {"#": _planks}, True)

# --- Stone-family stairs + slabs. ---
# (ingredient_block, output_prefix). smooth_stone has slab only (no stairs).
_STONE_TYPES: list[tuple[str, str]] = [
    ("cobblestone", "cobblestone"),
    ("stone", "stone"),
    ("stone_bricks", "stone_brick"),
    ("mossy_cobblestone", "mossy_cobblestone"),
    ("mossy_stone_bricks", "mossy_stone_brick"),
    ("smooth_stone", "smooth_stone"),
    ("sandstone", "sandstone"),
    ("red_sandstone", "red_sandstone"),
    ("nether_bricks", "nether_brick"),
    ("bricks", "brick"),
]
for _ingred, _prefix in _STONE_TYPES:
    if _ingred != "smooth_stone":
        RECIPES[f"{_prefix}_stairs"] = Recipe(f"{_prefix}_stairs", 4, ["#  ", "## ", "###"], {"#": _ingred}, True)
    RECIPES[f"{_prefix}_slab"] = Recipe(f"{_prefix}_slab", 6, ["###"], {"#": _ingred}, True)
RECIPES["stone_button"] = Recipe("stone_button", 1, ["#"], {"#": "stone"}, False)
RECIPES["stone_pressure_plate"] = Recipe("stone_pressure_plate", 1, ["##"], {"#": "stone"}, True)
RECIPES["stone_bricks"] = Recipe("stone_bricks", 4, ["##", "##"], {"#": "stone"}, False)


# Ingredients that accept any variant with the same suffix.
# e.g. recipe says "oak_planks" but any *_planks works (matches real MC behavior).
VARIANT_SUFFIXES: dict[str, str] = {
    "oak_planks": "_planks",
    "oak_log": "_log",
}

# Ingredients with explicit interchangeable alternatives that don't share a
# suffix. Mirrors the vanilla item tags the recipe matcher honors but a suffix
# rule can't express. e.g. the torch recipe accepts coal OR charcoal (the
# `minecraft:coals` tag).
INGREDIENT_ALTERNATIVES: dict[str, set[str]] = {
    "coal": {"charcoal"},
}


def _matches_ingredient(required: str, available: str) -> bool:
    """Check if an inventory item can satisfy a required ingredient."""
    if required == available:
        return True
    if available in INGREDIENT_ALTERNATIVES.get(required, ()):
        return True
    suffix = VARIANT_SUFFIXES.get(required)
    return suffix is not None and available.endswith(suffix)


def resolve_ingredients(
    required: dict[str, int], inventory: dict[str, int]
) -> dict[str, int] | None:
    """Map required (canonical) ingredients to actual inventory items.

    Returns {actual_item: count_to_consume} or None if insufficient.

    Two-pass pick: exact matches first, then variant fallbacks. Without this,
    an `oak_planks` requirement could grab `spruce_planks` even when oak is
    available — fine for `stick` (any plank works) but wrong for variant
    outputs where the recipe key was set to the canonical `oak_planks`.
    """
    resolved: dict[str, int] = {}
    for ingredient, needed in required.items():
        remaining = needed
        for pass_idx in (0, 1):
            if remaining <= 0:
                break
            for inv_item, inv_count in inventory.items():
                if remaining <= 0:
                    break
                if pass_idx == 0:
                    matches = inv_item == ingredient
                else:
                    matches = inv_item != ingredient and _matches_ingredient(ingredient, inv_item)
                if not matches:
                    continue
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
