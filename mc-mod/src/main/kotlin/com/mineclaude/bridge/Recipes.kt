package com.mineclaude.bridge

import kotlin.math.ceil

/**
 * Crafting + smelting recipe tables.
 *
 * Direct port of `bridge/recipes.py`. Kept as a Kotlin object so the native
 * bridge doesn't have to round-trip recipe data through Python — the table
 * is static, tiny (~30 + ~15 entries), and the helpers are pure functions.
 *
 * # Slot conventions
 *
 * 3x3 crafting table grid (PSH slots 1..9 inside a CraftingScreenHandler):
 * ```
 *   1 2 3
 *   4 5 6
 *   7 8 9
 * ```
 *
 * 2x2 player-inventory crafter (PSH slots 1..4 inside PlayerScreenHandler):
 * ```
 *   1 2
 *   3 4
 * ```
 *
 * Patterns use single-char keys mapped to item names. Space = empty slot.
 * Vanilla MC's recipe matcher trims empty rows/cols, so any sub-sized
 * pattern positioned at the top-left is recognized correctly.
 */
internal object Recipes {

    data class Recipe(
        val output: String,
        val outputCount: Int,
        val pattern: List<String>,
        val key: Map<Char, String>,
        val needsTable: Boolean,
    )

    /**
     * Ingredients that accept any variant with the same suffix.
     * e.g. recipe says "oak_planks" but any *_planks works.
     */
    private val VARIANT_SUFFIXES: Map<String, String> = mapOf(
        "oak_planks" to "_planks",
        "oak_log" to "_log",
    )

    val RECIPES: Map<String, Recipe> = mapOf(
        // --- Wood processing ---
        "oak_planks" to Recipe("oak_planks", 4, listOf("#"), mapOf('#' to "oak_log"), false),
        "spruce_planks" to Recipe("spruce_planks", 4, listOf("#"), mapOf('#' to "spruce_log"), false),
        "birch_planks" to Recipe("birch_planks", 4, listOf("#"), mapOf('#' to "birch_log"), false),
        "stick" to Recipe("stick", 4, listOf("#", "#"), mapOf('#' to "oak_planks"), false),

        // --- Basic blocks ---
        "crafting_table" to Recipe("crafting_table", 1, listOf("##", "##"), mapOf('#' to "oak_planks"), false),
        "furnace" to Recipe("furnace", 1, listOf("###", "# #", "###"), mapOf('#' to "cobblestone"), true),
        "chest" to Recipe("chest", 1, listOf("###", "# #", "###"), mapOf('#' to "oak_planks"), true),

        // --- Torches ---
        "torch" to Recipe("torch", 4, listOf("C", "S"), mapOf('C' to "coal", 'S' to "stick"), false),

        // --- Wooden tools ---
        "wooden_pickaxe" to Recipe("wooden_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "oak_planks", 'S' to "stick"), true),
        "wooden_axe" to Recipe("wooden_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "oak_planks", 'S' to "stick"), true),
        "wooden_shovel" to Recipe("wooden_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "oak_planks", 'S' to "stick"), true),
        "wooden_sword" to Recipe("wooden_sword", 1, listOf("#", "#", "S"), mapOf('#' to "oak_planks", 'S' to "stick"), true),
        "wooden_hoe" to Recipe("wooden_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "oak_planks", 'S' to "stick"), true),

        // --- Stone tools ---
        "stone_pickaxe" to Recipe("stone_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "cobblestone", 'S' to "stick"), true),
        "stone_axe" to Recipe("stone_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "cobblestone", 'S' to "stick"), true),
        "stone_shovel" to Recipe("stone_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "cobblestone", 'S' to "stick"), true),
        "stone_sword" to Recipe("stone_sword", 1, listOf("#", "#", "S"), mapOf('#' to "cobblestone", 'S' to "stick"), true),
        "stone_hoe" to Recipe("stone_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "cobblestone", 'S' to "stick"), true),

        // --- Iron tools ---
        "iron_pickaxe" to Recipe("iron_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_axe" to Recipe("iron_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_shovel" to Recipe("iron_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_sword" to Recipe("iron_sword", 1, listOf("#", "#", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_hoe" to Recipe("iron_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),

        // --- Diamond tools ---
        "diamond_pickaxe" to Recipe("diamond_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "diamond", 'S' to "stick"), true),
        "diamond_axe" to Recipe("diamond_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "diamond", 'S' to "stick"), true),
        "diamond_shovel" to Recipe("diamond_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "diamond", 'S' to "stick"), true),
        "diamond_sword" to Recipe("diamond_sword", 1, listOf("#", "#", "S"), mapOf('#' to "diamond", 'S' to "stick"), true),
        "diamond_hoe" to Recipe("diamond_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "diamond", 'S' to "stick"), true),

        // --- Golden tools ---
        "golden_pickaxe" to Recipe("golden_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "gold_ingot", 'S' to "stick"), true),
        "golden_axe" to Recipe("golden_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true),
        "golden_shovel" to Recipe("golden_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true),
        "golden_sword" to Recipe("golden_sword", 1, listOf("#", "#", "S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true),
        "golden_hoe" to Recipe("golden_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true),

        // --- Iron armor ---
        "iron_helmet" to Recipe("iron_helmet", 1, listOf("###", "# #"), mapOf('#' to "iron_ingot"), true),
        "iron_chestplate" to Recipe("iron_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "iron_ingot"), true),
        "iron_leggings" to Recipe("iron_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "iron_ingot"), true),
        "iron_boots" to Recipe("iron_boots", 1, listOf("# #", "# #"), mapOf('#' to "iron_ingot"), true),

        // --- Diamond armor ---
        "diamond_helmet" to Recipe("diamond_helmet", 1, listOf("###", "# #"), mapOf('#' to "diamond"), true),
        "diamond_chestplate" to Recipe("diamond_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "diamond"), true),
        "diamond_leggings" to Recipe("diamond_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "diamond"), true),
        "diamond_boots" to Recipe("diamond_boots", 1, listOf("# #", "# #"), mapOf('#' to "diamond"), true),

        // --- Golden armor ---
        "golden_helmet" to Recipe("golden_helmet", 1, listOf("###", "# #"), mapOf('#' to "gold_ingot"), true),
        "golden_chestplate" to Recipe("golden_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "gold_ingot"), true),
        "golden_leggings" to Recipe("golden_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "gold_ingot"), true),
        "golden_boots" to Recipe("golden_boots", 1, listOf("# #", "# #"), mapOf('#' to "gold_ingot"), true),

        // --- Leather armor ---
        "leather_helmet" to Recipe("leather_helmet", 1, listOf("###", "# #"), mapOf('#' to "leather"), true),
        "leather_chestplate" to Recipe("leather_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "leather"), true),
        "leather_leggings" to Recipe("leather_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "leather"), true),
        "leather_boots" to Recipe("leather_boots", 1, listOf("# #", "# #"), mapOf('#' to "leather"), true),

        // --- Storage blocks (compress 9 → 1) ---
        "iron_block" to Recipe("iron_block", 1, listOf("###", "###", "###"), mapOf('#' to "iron_ingot"), true),
        "gold_block" to Recipe("gold_block", 1, listOf("###", "###", "###"), mapOf('#' to "gold_ingot"), true),
        "diamond_block" to Recipe("diamond_block", 1, listOf("###", "###", "###"), mapOf('#' to "diamond"), true),
        "redstone_block" to Recipe("redstone_block", 1, listOf("###", "###", "###"), mapOf('#' to "redstone"), true),
        "emerald_block" to Recipe("emerald_block", 1, listOf("###", "###", "###"), mapOf('#' to "emerald"), true),
        "lapis_block" to Recipe("lapis_block", 1, listOf("###", "###", "###"), mapOf('#' to "lapis_lazuli"), true),
        "coal_block" to Recipe("coal_block", 1, listOf("###", "###", "###"), mapOf('#' to "coal"), true),

        // --- Survival essentials ---
        // Bed: alias `bed` → white_bed. Other wool colors yield other-colored beds; only white is stocked here for now.
        "bed" to Recipe("white_bed", 1, listOf("WWW", "###"), mapOf('W' to "white_wool", '#' to "oak_planks"), true),
        "white_bed" to Recipe("white_bed", 1, listOf("WWW", "###"), mapOf('W' to "white_wool", '#' to "oak_planks"), true),
        "shield" to Recipe("shield", 1, listOf("#I#", "###", " # "), mapOf('#' to "oak_planks", 'I' to "iron_ingot"), true),
        "bread" to Recipe("bread", 1, listOf("###"), mapOf('#' to "wheat"), true),
        "flint_and_steel" to Recipe("flint_and_steel", 1, listOf("I ", " F"), mapOf('I' to "iron_ingot", 'F' to "flint"), false),
        "fishing_rod" to Recipe("fishing_rod", 1, listOf("  #", " #X", "# X"), mapOf('#' to "stick", 'X' to "string"), true),
        "bow" to Recipe("bow", 1, listOf(" #X", "# X", " #X"), mapOf('#' to "stick", 'X' to "string"), true),
        "arrow" to Recipe("arrow", 4, listOf("F", "S", "T"), mapOf('F' to "flint", 'S' to "stick", 'T' to "feather"), true),
        "cake" to Recipe("cake", 1, listOf("MMM", "SES", "WWW"), mapOf('M' to "milk_bucket", 'S' to "sugar", 'E' to "egg", 'W' to "wheat"), true),

        // --- Wool & decoration ---
        "white_wool" to Recipe("white_wool", 1, listOf("##", "##"), mapOf('#' to "string"), false),
        "bricks" to Recipe("bricks", 1, listOf("##", "##"), mapOf('#' to "brick"), false),
        "jack_o_lantern" to Recipe("jack_o_lantern", 1, listOf("P", "T"), mapOf('P' to "carved_pumpkin", 'T' to "torch"), false),

        // --- Paper, books, navigation ---
        "paper" to Recipe("paper", 3, listOf("###"), mapOf('#' to "sugar_cane"), true),
        "book" to Recipe("book", 1, listOf("PP", "PL"), mapOf('P' to "paper", 'L' to "leather"), false),
        "bookshelf" to Recipe("bookshelf", 1, listOf("###", "BBB", "###"), mapOf('#' to "oak_planks", 'B' to "book"), true),
        "compass" to Recipe("compass", 1, listOf(" I ", "IRI", " I "), mapOf('I' to "iron_ingot", 'R' to "redstone"), true),
        "clock" to Recipe("clock", 1, listOf(" G ", "GRG", " G "), mapOf('G' to "gold_ingot", 'R' to "redstone"), true),
        // `map` recipe yields filled_map (the empty/explorable version) — name resolution at the slot-0 check needs the actual item id.
        "map" to Recipe("filled_map", 1, listOf("PPP", "PCP", "PPP"), mapOf('P' to "paper", 'C' to "compass"), true),

        // --- Transport ---
        "oak_boat" to Recipe("oak_boat", 1, listOf("# #", "###"), mapOf('#' to "oak_planks"), true),
        "minecart" to Recipe("minecart", 1, listOf("# #", "###"), mapOf('#' to "iron_ingot"), true),
        "rail" to Recipe("rail", 16, listOf("# #", "#S#", "# #"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),

        // --- Redstone components ---
        "redstone_torch" to Recipe("redstone_torch", 1, listOf("R", "S"), mapOf('R' to "redstone", 'S' to "stick"), false),
        "redstone_lamp" to Recipe("redstone_lamp", 1, listOf(" R ", "RGR", " R "), mapOf('R' to "redstone", 'G' to "glowstone"), true),
        "repeater" to Recipe("repeater", 1, listOf("TRT", "###"), mapOf('T' to "redstone_torch", 'R' to "redstone", '#' to "stone"), true),

        // --- Utility blocks ---
        "hopper" to Recipe("hopper", 1, listOf("I I", "ICI", " I "), mapOf('I' to "iron_ingot", 'C' to "chest"), true),
        "dispenser" to Recipe("dispenser", 1, listOf("###", "#B#", "#R#"), mapOf('#' to "cobblestone", 'B' to "bow", 'R' to "redstone"), true),
        "dropper" to Recipe("dropper", 1, listOf("###", "# #", "#R#"), mapOf('#' to "cobblestone", 'R' to "redstone"), true),
        "piston" to Recipe("piston", 1, listOf("###", "CIC", "CRC"), mapOf('#' to "oak_planks", 'C' to "cobblestone", 'I' to "iron_ingot", 'R' to "redstone"), true),

        // --- Misc tools ---
        "bucket" to Recipe("bucket", 1, listOf("# #", " # "), mapOf('#' to "iron_ingot"), true),
        "ladder" to Recipe("ladder", 3, listOf("# #", "###", "# #"), mapOf('#' to "stick"), true),
        "bowl" to Recipe("bowl", 4, listOf("# #", " # "), mapOf('#' to "oak_planks"), true),
    )

    fun matchesIngredient(required: String, available: String): Boolean {
        if (required == available) return true
        val suffix = VARIANT_SUFFIXES[required] ?: return false
        return available.endsWith(suffix)
    }

    fun getRecipe(item: String): Recipe? {
        val name = item.removePrefix("minecraft:")
        return RECIPES[name]
    }

    /**
     * Total ingredients to craft `count` of `item`. Returns null if the
     * recipe is unknown.
     */
    fun getRequiredIngredients(item: String, count: Int = 1): Map<String, Int>? {
        val recipe = getRecipe(item) ?: return null
        val craftsNeeded = ceil(count.toDouble() / recipe.outputCount).toInt()
        val perCraft = mutableMapOf<String, Int>()
        for (row in recipe.pattern) {
            for (ch in row) {
                if (ch == ' ') continue
                val ing = recipe.key[ch] ?: continue
                perCraft[ing] = (perCraft[ing] ?: 0) + 1
            }
        }
        return perCraft.mapValues { it.value * craftsNeeded }
    }

    /**
     * Map required canonical ingredients onto actual inventory items,
     * honoring variant suffixes. Returns null if any ingredient is short.
     */
    fun resolveIngredients(
        required: Map<String, Int>,
        inventory: Map<String, Int>,
    ): Map<String, Int>? {
        val resolved = mutableMapOf<String, Int>()
        for ((ingredient, needed) in required) {
            var remaining = needed
            for ((invItem, invCount) in inventory) {
                if (remaining <= 0) break
                if (!matchesIngredient(ingredient, invItem)) continue
                val claimed = resolved[invItem] ?: 0
                val available = invCount - claimed
                if (available <= 0) continue
                val take = minOf(available, remaining)
                resolved[invItem] = claimed + take
                remaining -= take
            }
            if (remaining > 0) return null
        }
        return resolved
    }

    /**
     * 3x3 grid layout for a recipe — returns 1-indexed slot → ingredient
     * canonical name. 2x2 patterns land at top-left (slots 1, 2, 4, 5);
     * MC's matcher trims empty rows/cols.
     */
    fun patternToTableSlots(recipe: Recipe): Map<Int, String> {
        if (recipe.pattern.isEmpty()) return emptyMap()
        val width = recipe.pattern.maxOf { it.length }
        val height = recipe.pattern.size
        require(width <= 3 && height <= 3) {
            "Recipe pattern ${width}x$height too large for 3x3 crafting grid"
        }
        val slots = mutableMapOf<Int, String>()
        for ((r, row) in recipe.pattern.withIndex()) {
            for ((c, ch) in row.withIndex()) {
                if (ch == ' ') continue
                val ing = recipe.key[ch] ?: continue
                slots[r * 3 + c + 1] = ing
            }
        }
        return slots
    }

    /**
     * 2x2 inventory-crafter layout — 1-indexed slot → ingredient. Throws
     * if the recipe needs a 3x3 grid.
     */
    fun patternToInventorySlots(recipe: Recipe): Map<Int, String> {
        require(!recipe.needsTable) {
            "Recipe ${recipe.output} needs a 3x3 crafting table, cannot fit in 2x2 inventory crafter"
        }
        if (recipe.pattern.isEmpty()) return emptyMap()
        val width = recipe.pattern.maxOf { it.length }
        val height = recipe.pattern.size
        require(width <= 2 && height <= 2) {
            "Recipe pattern ${width}x$height too large for 2x2 inventory crafter"
        }
        val slots = mutableMapOf<Int, String>()
        for ((r, row) in recipe.pattern.withIndex()) {
            for ((c, ch) in row.withIndex()) {
                if (ch == ' ') continue
                val ing = recipe.key[ch] ?: continue
                slots[r * 2 + c + 1] = ing
            }
        }
        return slots
    }

    /**
     * Format ingredient counts for error messages, hinting at variants —
     * e.g. an `oak_planks` requirement matches any `*_planks`, so we
     * surface "3x planks (any variant)" rather than the misleading literal.
     */
    fun formatRequiredIngredients(required: Map<String, Int>): String {
        if (required.isEmpty()) return "nothing"
        return required.entries.joinToString(", ") { (name, count) ->
            val suffix = VARIANT_SUFFIXES[name]
            if (suffix != null) {
                "${count}x ${suffix.removePrefix("_")} (any variant)"
            } else {
                "${count}x $name"
            }
        }
    }
}
