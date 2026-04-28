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

    data class SmeltingRecipe(
        val output: String,
        val input: String,
        val outputCount: Int = 1,
    )

    /**
     * Ingredients that accept any variant with the same suffix.
     * e.g. recipe says "oak_planks" but any *_planks works.
     */
    private val VARIANT_SUFFIXES: Map<String, String> = mapOf(
        "oak_planks" to "_planks",
        "oak_log" to "_log",
    )

    private val SMELTING_VARIANT_SUFFIXES: Map<String, String> = mapOf(
        "oak_log" to "_log",
    )

    private val FUEL_VARIANT_SUFFIXES: Map<String, String> = mapOf(
        "oak_planks" to "_planks",
        "oak_log" to "_log",
    )

    /** Items per fuel unit. */
    private val FUEL_VALUES: Map<String, Double> = mapOf(
        "coal" to 8.0,
        "charcoal" to 8.0,
        "coal_block" to 80.0,
        "lava_bucket" to 100.0,
        "blaze_rod" to 12.0,
        "stick" to 0.5,
        "oak_planks" to 1.5,
        "oak_log" to 1.5,
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

        // --- Iron tools ---
        "iron_pickaxe" to Recipe("iron_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_axe" to Recipe("iron_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_shovel" to Recipe("iron_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),
        "iron_sword" to Recipe("iron_sword", 1, listOf("#", "#", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true),

        // --- Armor ---
        "iron_helmet" to Recipe("iron_helmet", 1, listOf("###", "# #"), mapOf('#' to "iron_ingot"), true),
        "iron_chestplate" to Recipe("iron_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "iron_ingot"), true),
        "iron_leggings" to Recipe("iron_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "iron_ingot"), true),
        "iron_boots" to Recipe("iron_boots", 1, listOf("# #", "# #"), mapOf('#' to "iron_ingot"), true),

        // --- Misc ---
        "bucket" to Recipe("bucket", 1, listOf("# #", " # "), mapOf('#' to "iron_ingot"), true),
        "ladder" to Recipe("ladder", 3, listOf("# #", "###", "# #"), mapOf('#' to "stick"), true),
        "bowl" to Recipe("bowl", 4, listOf("# #", " # "), mapOf('#' to "oak_planks"), true),
    )

    val SMELTING_RECIPES: Map<String, SmeltingRecipe> = mapOf(
        "iron_ingot" to SmeltingRecipe("iron_ingot", "raw_iron"),
        "gold_ingot" to SmeltingRecipe("gold_ingot", "raw_gold"),
        "copper_ingot" to SmeltingRecipe("copper_ingot", "raw_copper"),
        "glass" to SmeltingRecipe("glass", "sand"),
        "stone" to SmeltingRecipe("stone", "cobblestone"),
        "smooth_stone" to SmeltingRecipe("smooth_stone", "stone"),
        "brick" to SmeltingRecipe("brick", "clay_ball"),
        "nether_brick" to SmeltingRecipe("nether_brick", "netherrack"),
        "charcoal" to SmeltingRecipe("charcoal", "oak_log"),
        "cooked_beef" to SmeltingRecipe("cooked_beef", "beef"),
        "cooked_porkchop" to SmeltingRecipe("cooked_porkchop", "porkchop"),
        "cooked_chicken" to SmeltingRecipe("cooked_chicken", "chicken"),
        "cooked_mutton" to SmeltingRecipe("cooked_mutton", "mutton"),
        "cooked_cod" to SmeltingRecipe("cooked_cod", "cod"),
        "cooked_salmon" to SmeltingRecipe("cooked_salmon", "salmon"),
        "dried_kelp" to SmeltingRecipe("dried_kelp", "kelp"),
    )

    fun matchesIngredient(required: String, available: String): Boolean {
        if (required == available) return true
        val suffix = VARIANT_SUFFIXES[required] ?: return false
        return available.endsWith(suffix)
    }

    fun matchesSmeltingInput(required: String, available: String): Boolean {
        if (required == available) return true
        val suffix = SMELTING_VARIANT_SUFFIXES[required] ?: return false
        return available.endsWith(suffix)
    }

    /** Items per unit of this fuel, or 0 if not fuel. */
    fun getFuelValue(item: String): Double {
        val name = item.removePrefix("minecraft:")
        FUEL_VALUES[name]?.let { return it }
        for ((canonical, suffix) in FUEL_VARIANT_SUFFIXES) {
            if (name.endsWith(suffix)) {
                FUEL_VALUES[canonical]?.let { return it }
            }
        }
        return 0.0
    }

    fun getRecipe(item: String): Recipe? {
        val name = item.removePrefix("minecraft:")
        return RECIPES[name]
    }

    fun getSmeltingRecipe(item: String): SmeltingRecipe? {
        val name = item.removePrefix("minecraft:")
        return SMELTING_RECIPES[name]
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
