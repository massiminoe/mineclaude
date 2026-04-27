"""Block-id classification helpers — pure data, no Minescript dependency.

Kept minescript-free so the underlying tables (replaceable set, denylists,
etc.) can be unit-tested from the repo root without booting the MC client.
"""

from __future__ import annotations


# Blocks that vanilla MC silently replaces when something is placed in
# their cell — `BlockBehaviour.canBeReplaced` returns true for these.
# Pinned to MC 1.21.5; revisit if we bump the server version. Plant
# overlays (the "grass texture sticking out of the ground") are the most
# common trip-up: previously _place_real treated any non-air block as
# blocking, so a placeBlock onto a cell with short_grass would error
# even though placing it manually in the game just replaces the grass.
REPLACEABLE_BLOCKS = frozenset({
    "air", "cave_air", "void_air",
    # Plant overlays
    "short_grass", "tall_grass", "fern", "large_fern",
    "dead_bush", "seagrass", "tall_seagrass",
    "vine", "glow_lichen", "hanging_roots",
    "pink_petals", "leaf_litter", "wildflowers",
    # Flowers
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "red_tulip", "orange_tulip", "white_tulip", "pink_tulip",
    "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose",
    "torchflower", "pitcher_plant",
    "sunflower", "lilac", "rose_bush", "peony",
    # Saplings + small plants
    "oak_sapling", "spruce_sapling", "birch_sapling", "jungle_sapling",
    "acacia_sapling", "dark_oak_sapling", "mangrove_propagule",
    "cherry_sapling", "azalea", "flowering_azalea",
    "brown_mushroom", "red_mushroom",
    # Snow layer (snow_block is NOT replaceable)
    "snow",
    # Liquids
    "water", "lava", "bubble_column",
    # Fire-y / technical
    "fire", "soul_fire", "light",
})


def normalize_block_id(block_id: str) -> str:
    """Strip blockstate suffix and `minecraft:` namespace.

    `minecraft:short_grass[snowy=false]` → `short_grass`.
    """
    name = block_id.split("[", 1)[0]
    return name.split(":", 1)[-1]


def is_replaceable(block_id: str | None) -> bool:
    """Return True if a block at a target cell should not block placement.

    A None or empty id is treated as replaceable (optimistic — let the
    placement attempt happen and fail honestly rather than spuriously
    refusing to even try).
    """
    if not block_id:
        return True
    return normalize_block_id(block_id) in REPLACEABLE_BLOCKS
