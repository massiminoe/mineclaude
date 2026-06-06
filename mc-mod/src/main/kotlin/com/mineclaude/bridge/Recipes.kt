package com.mineclaude.bridge

import kotlin.math.ceil

/**
 * Crafting + smelting recipe tables.
 *
 * Direct port of `bridge/recipes.py`. Kept as a Kotlin object so the native
 * bridge doesn't have to round-trip recipe data through Python — the table
 * is static and the helpers are pure functions.
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
     * Sentinel ingredients that accept any variant with the same suffix.
     * Used ONLY by recipes whose output doesn't depend on wood type (stick,
     * crafting_table, chest, wooden tools, bed, etc.) — `any_planks` matches
     * any `*_planks`, `any_log` any `*_log`.
     *
     * Deliberately NOT keyed on a real wood (e.g. `oak_planks`): a recipe whose
     * output *is* variant-specific (`oak_planks` from `oak_log`, `oak_stairs`
     * from `oak_planks`) must match its ingredient EXACTLY, or it would grab
     * the wrong wood and produce the wrong variant. Those recipes keep their
     * literal `oak_*`/`spruce_*`/... ingredient names, which aren't in this
     * map and therefore match exactly.
     */
    private val VARIANT_SUFFIXES: Map<String, String> = mapOf(
        "any_planks" to "_planks",
        "any_log" to "_log",
    )

    /**
     * Ingredients with explicit interchangeable alternatives that don't share
     * a suffix. Mirrors the vanilla item tags the recipe matcher honors but a
     * suffix rule can't express. e.g. the torch recipe accepts coal OR
     * charcoal (the `minecraft:coals` tag).
     */
    private val INGREDIENT_ALTERNATIVES: Map<String, Set<String>> = mapOf(
        "coal" to setOf("charcoal"),
    )

    /**
     * Per-wood-type metadata used to emit one recipe per wood for stairs,
     * slabs, doors, trapdoors, fences, gates, buttons, pressure plates,
     * signs, planks, and (where applicable) boats. Every variant output uses
     * its specific plank/log as ingredient (e.g. spruce_door wants
     * spruce_planks, oak_planks wants oak_log) so the ingredient matches
     * EXACTLY — vanilla MC won't accept a foreign plank, and a loose match
     * would silently craft the wrong variant. The "any plank works" affordance
     * lives only on the wood-agnostic recipes via the `any_planks`/`any_log`
     * sentinels, never here.
     */
    private data class WoodType(
        val name: String,
        val logOrStem: String,
        val planksPerCraft: Int = 4,
        val boatOutput: String? = null,  // null = no boat (nether woods)
    )

    private val WOOD_TYPES: List<WoodType> = listOf(
        WoodType("oak", "oak_log", boatOutput = "oak_boat"),
        WoodType("spruce", "spruce_log", boatOutput = "spruce_boat"),
        WoodType("birch", "birch_log", boatOutput = "birch_boat"),
        WoodType("jungle", "jungle_log", boatOutput = "jungle_boat"),
        WoodType("acacia", "acacia_log", boatOutput = "acacia_boat"),
        WoodType("dark_oak", "dark_oak_log", boatOutput = "dark_oak_boat"),
        WoodType("mangrove", "mangrove_log", boatOutput = "mangrove_boat"),
        WoodType("cherry", "cherry_log", boatOutput = "cherry_boat"),
        WoodType("crimson", "crimson_stem", boatOutput = null),
        WoodType("warped", "warped_stem", boatOutput = null),
        // bamboo: 1 bamboo_block → 2 bamboo_planks. Boat is bamboo_raft.
        WoodType("bamboo", "bamboo_block", planksPerCraft = 2, boatOutput = "bamboo_raft"),
    )

    /**
     * Stone-family stairs/slabs/buttons/pressure-plates. Output count and
     * patterns follow the wood layout (stairs `# /## /###` → 4, slab `###` → 6).
     */
    private data class StoneType(
        val name: String,    // ingredient name
        val prefix: String,  // output prefix; "" → uses ingredient name as prefix
    )

    private val STONE_TYPES: List<StoneType> = listOf(
        StoneType("cobblestone", "cobblestone"),
        StoneType("stone", "stone"),
        StoneType("stone_bricks", "stone_brick"),
        StoneType("mossy_cobblestone", "mossy_cobblestone"),
        StoneType("mossy_stone_bricks", "mossy_stone_brick"),
        StoneType("smooth_stone", "smooth_stone"),  // slab only — see filter below
        StoneType("sandstone", "sandstone"),
        StoneType("red_sandstone", "red_sandstone"),
        StoneType("nether_bricks", "nether_brick"),
        StoneType("bricks", "brick"),  // ingredient is the `bricks` block; outputs brick_stairs/brick_slab
    )

    val RECIPES: Map<String, Recipe> = buildMap {
        // --- Wood: planks, stairs, slabs, doors, trapdoors, fences, gates,
        // buttons, pressure plates, signs, boats (per variant). ---
        for (w in WOOD_TYPES) {
            put("${w.name}_planks", Recipe(
                "${w.name}_planks", w.planksPerCraft,
                listOf("#"), mapOf('#' to w.logOrStem), false))
            put("${w.name}_stairs", Recipe(
                "${w.name}_stairs", 4,
                listOf("#  ", "## ", "###"), mapOf('#' to "${w.name}_planks"), true))
            put("${w.name}_slab", Recipe(
                "${w.name}_slab", 6,
                listOf("###"), mapOf('#' to "${w.name}_planks"), true))
            put("${w.name}_door", Recipe(
                "${w.name}_door", 3,
                listOf("##", "##", "##"), mapOf('#' to "${w.name}_planks"), true))
            put("${w.name}_trapdoor", Recipe(
                "${w.name}_trapdoor", 2,
                listOf("###", "###"), mapOf('#' to "${w.name}_planks"), true))
            put("${w.name}_fence", Recipe(
                "${w.name}_fence", 3,
                listOf("#S#", "#S#"), mapOf('#' to "${w.name}_planks", 'S' to "stick"), true))
            put("${w.name}_fence_gate", Recipe(
                "${w.name}_fence_gate", 1,
                listOf("S#S", "S#S"), mapOf('#' to "${w.name}_planks", 'S' to "stick"), true))
            put("${w.name}_button", Recipe(
                "${w.name}_button", 1,
                listOf("#"), mapOf('#' to "${w.name}_planks"), false))
            put("${w.name}_pressure_plate", Recipe(
                "${w.name}_pressure_plate", 1,
                listOf("##"), mapOf('#' to "${w.name}_planks"), true))
            put("${w.name}_sign", Recipe(
                "${w.name}_sign", 3,
                listOf("###", "###", " S "), mapOf('#' to "${w.name}_planks", 'S' to "stick"), true))
            w.boatOutput?.let { boat ->
                put(boat, Recipe(boat, 1,
                    listOf("# #", "###"), mapOf('#' to "${w.name}_planks"), true))
            }
        }

        // --- Stone family: stairs, slabs, plus stone_button + stone_pressure_plate. ---
        for (s in STONE_TYPES) {
            // smooth_stone has no stairs in vanilla — only a slab.
            if (s.name != "smooth_stone") {
                put("${s.prefix}_stairs", Recipe(
                    "${s.prefix}_stairs", 4,
                    listOf("#  ", "## ", "###"), mapOf('#' to s.name), true))
            }
            put("${s.prefix}_slab", Recipe(
                "${s.prefix}_slab", 6,
                listOf("###"), mapOf('#' to s.name), true))
        }
        // stone-only: button + pressure plate (other stone families don't have these in vanilla).
        put("stone_button", Recipe("stone_button", 1, listOf("#"), mapOf('#' to "stone"), false))
        put("stone_pressure_plate", Recipe("stone_pressure_plate", 1, listOf("##"), mapOf('#' to "stone"), true))
        // 2x2 stone → 4 stone_bricks
        put("stone_bricks", Recipe("stone_bricks", 4, listOf("##", "##"), mapOf('#' to "stone"), false))

        // --- Stick (any plank works via the any_planks sentinel). ---
        put("stick", Recipe("stick", 4, listOf("#", "#"), mapOf('#' to "any_planks"), false))

        // --- Basic blocks ---
        put("crafting_table", Recipe("crafting_table", 1, listOf("##", "##"), mapOf('#' to "any_planks"), false))
        put("furnace", Recipe("furnace", 1, listOf("###", "# #", "###"), mapOf('#' to "cobblestone"), true))
        put("chest", Recipe("chest", 1, listOf("###", "# #", "###"), mapOf('#' to "any_planks"), true))

        // --- Torches ---
        put("torch", Recipe("torch", 4, listOf("C", "S"), mapOf('C' to "coal", 'S' to "stick"), false))

        // --- Wooden tools ---
        put("wooden_pickaxe", Recipe("wooden_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "any_planks", 'S' to "stick"), true))
        put("wooden_axe", Recipe("wooden_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "any_planks", 'S' to "stick"), true))
        put("wooden_shovel", Recipe("wooden_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "any_planks", 'S' to "stick"), true))
        put("wooden_sword", Recipe("wooden_sword", 1, listOf("#", "#", "S"), mapOf('#' to "any_planks", 'S' to "stick"), true))
        put("wooden_hoe", Recipe("wooden_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "any_planks", 'S' to "stick"), true))

        // --- Stone tools ---
        put("stone_pickaxe", Recipe("stone_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "cobblestone", 'S' to "stick"), true))
        put("stone_axe", Recipe("stone_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "cobblestone", 'S' to "stick"), true))
        put("stone_shovel", Recipe("stone_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "cobblestone", 'S' to "stick"), true))
        put("stone_sword", Recipe("stone_sword", 1, listOf("#", "#", "S"), mapOf('#' to "cobblestone", 'S' to "stick"), true))
        put("stone_hoe", Recipe("stone_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "cobblestone", 'S' to "stick"), true))

        // --- Iron tools ---
        put("iron_pickaxe", Recipe("iron_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "iron_ingot", 'S' to "stick"), true))
        put("iron_axe", Recipe("iron_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true))
        put("iron_shovel", Recipe("iron_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true))
        put("iron_sword", Recipe("iron_sword", 1, listOf("#", "#", "S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true))
        put("iron_hoe", Recipe("iron_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "iron_ingot", 'S' to "stick"), true))

        // --- Diamond tools ---
        put("diamond_pickaxe", Recipe("diamond_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "diamond", 'S' to "stick"), true))
        put("diamond_axe", Recipe("diamond_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "diamond", 'S' to "stick"), true))
        put("diamond_shovel", Recipe("diamond_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "diamond", 'S' to "stick"), true))
        put("diamond_sword", Recipe("diamond_sword", 1, listOf("#", "#", "S"), mapOf('#' to "diamond", 'S' to "stick"), true))
        put("diamond_hoe", Recipe("diamond_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "diamond", 'S' to "stick"), true))

        // --- Golden tools ---
        put("golden_pickaxe", Recipe("golden_pickaxe", 1, listOf("###", " S ", " S "), mapOf('#' to "gold_ingot", 'S' to "stick"), true))
        put("golden_axe", Recipe("golden_axe", 1, listOf("##", "#S", " S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true))
        put("golden_shovel", Recipe("golden_shovel", 1, listOf("#", "S", "S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true))
        put("golden_sword", Recipe("golden_sword", 1, listOf("#", "#", "S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true))
        put("golden_hoe", Recipe("golden_hoe", 1, listOf("##", " S", " S"), mapOf('#' to "gold_ingot", 'S' to "stick"), true))

        // --- Iron armor ---
        put("iron_helmet", Recipe("iron_helmet", 1, listOf("###", "# #"), mapOf('#' to "iron_ingot"), true))
        put("iron_chestplate", Recipe("iron_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "iron_ingot"), true))
        put("iron_leggings", Recipe("iron_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "iron_ingot"), true))
        put("iron_boots", Recipe("iron_boots", 1, listOf("# #", "# #"), mapOf('#' to "iron_ingot"), true))

        // --- Diamond armor ---
        put("diamond_helmet", Recipe("diamond_helmet", 1, listOf("###", "# #"), mapOf('#' to "diamond"), true))
        put("diamond_chestplate", Recipe("diamond_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "diamond"), true))
        put("diamond_leggings", Recipe("diamond_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "diamond"), true))
        put("diamond_boots", Recipe("diamond_boots", 1, listOf("# #", "# #"), mapOf('#' to "diamond"), true))

        // --- Golden armor ---
        put("golden_helmet", Recipe("golden_helmet", 1, listOf("###", "# #"), mapOf('#' to "gold_ingot"), true))
        put("golden_chestplate", Recipe("golden_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "gold_ingot"), true))
        put("golden_leggings", Recipe("golden_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "gold_ingot"), true))
        put("golden_boots", Recipe("golden_boots", 1, listOf("# #", "# #"), mapOf('#' to "gold_ingot"), true))

        // --- Leather armor ---
        put("leather_helmet", Recipe("leather_helmet", 1, listOf("###", "# #"), mapOf('#' to "leather"), true))
        put("leather_chestplate", Recipe("leather_chestplate", 1, listOf("# #", "###", "###"), mapOf('#' to "leather"), true))
        put("leather_leggings", Recipe("leather_leggings", 1, listOf("###", "# #", "# #"), mapOf('#' to "leather"), true))
        put("leather_boots", Recipe("leather_boots", 1, listOf("# #", "# #"), mapOf('#' to "leather"), true))

        // --- Storage blocks (compress 9 → 1) ---
        put("iron_block", Recipe("iron_block", 1, listOf("###", "###", "###"), mapOf('#' to "iron_ingot"), true))
        put("gold_block", Recipe("gold_block", 1, listOf("###", "###", "###"), mapOf('#' to "gold_ingot"), true))
        put("diamond_block", Recipe("diamond_block", 1, listOf("###", "###", "###"), mapOf('#' to "diamond"), true))
        put("redstone_block", Recipe("redstone_block", 1, listOf("###", "###", "###"), mapOf('#' to "redstone"), true))
        put("emerald_block", Recipe("emerald_block", 1, listOf("###", "###", "###"), mapOf('#' to "emerald"), true))
        put("lapis_block", Recipe("lapis_block", 1, listOf("###", "###", "###"), mapOf('#' to "lapis_lazuli"), true))
        put("coal_block", Recipe("coal_block", 1, listOf("###", "###", "###"), mapOf('#' to "coal"), true))

        // --- Survival essentials ---
        // Bed: alias `bed` → white_bed. Other wool colors yield other-colored beds; only white is stocked here for now.
        put("bed", Recipe("white_bed", 1, listOf("WWW", "###"), mapOf('W' to "white_wool", '#' to "any_planks"), true))
        put("white_bed", Recipe("white_bed", 1, listOf("WWW", "###"), mapOf('W' to "white_wool", '#' to "any_planks"), true))
        put("shield", Recipe("shield", 1, listOf("#I#", "###", " # "), mapOf('#' to "any_planks", 'I' to "iron_ingot"), true))
        put("bread", Recipe("bread", 1, listOf("###"), mapOf('#' to "wheat"), true))
        put("flint_and_steel", Recipe("flint_and_steel", 1, listOf("I ", " F"), mapOf('I' to "iron_ingot", 'F' to "flint"), false))
        put("fishing_rod", Recipe("fishing_rod", 1, listOf("  #", " #X", "# X"), mapOf('#' to "stick", 'X' to "string"), true))
        put("bow", Recipe("bow", 1, listOf(" #X", "# X", " #X"), mapOf('#' to "stick", 'X' to "string"), true))
        put("arrow", Recipe("arrow", 4, listOf("F", "S", "T"), mapOf('F' to "flint", 'S' to "stick", 'T' to "feather"), true))
        put("cake", Recipe("cake", 1, listOf("MMM", "SES", "WWW"), mapOf('M' to "milk_bucket", 'S' to "sugar", 'E' to "egg", 'W' to "wheat"), true))

        // --- Wool & decoration ---
        put("white_wool", Recipe("white_wool", 1, listOf("##", "##"), mapOf('#' to "string"), false))
        put("bricks", Recipe("bricks", 1, listOf("##", "##"), mapOf('#' to "brick"), false))
        put("jack_o_lantern", Recipe("jack_o_lantern", 1, listOf("P", "T"), mapOf('P' to "carved_pumpkin", 'T' to "torch"), false))

        // --- Paper, books, navigation ---
        put("paper", Recipe("paper", 3, listOf("###"), mapOf('#' to "sugar_cane"), true))
        put("book", Recipe("book", 1, listOf("PP", "PL"), mapOf('P' to "paper", 'L' to "leather"), false))
        put("bookshelf", Recipe("bookshelf", 1, listOf("###", "BBB", "###"), mapOf('#' to "any_planks", 'B' to "book"), true))
        put("compass", Recipe("compass", 1, listOf(" I ", "IRI", " I "), mapOf('I' to "iron_ingot", 'R' to "redstone"), true))
        put("clock", Recipe("clock", 1, listOf(" G ", "GRG", " G "), mapOf('G' to "gold_ingot", 'R' to "redstone"), true))
        // `map` recipe yields filled_map (the empty/explorable version) — name resolution at the slot-0 check needs the actual item id.
        put("map", Recipe("filled_map", 1, listOf("PPP", "PCP", "PPP"), mapOf('P' to "paper", 'C' to "compass"), true))

        // --- Transport ---
        put("minecart", Recipe("minecart", 1, listOf("# #", "###"), mapOf('#' to "iron_ingot"), true))
        put("rail", Recipe("rail", 16, listOf("# #", "#S#", "# #"), mapOf('#' to "iron_ingot", 'S' to "stick"), true))

        // --- Redstone components ---
        put("redstone_torch", Recipe("redstone_torch", 1, listOf("R", "S"), mapOf('R' to "redstone", 'S' to "stick"), false))
        put("redstone_lamp", Recipe("redstone_lamp", 1, listOf(" R ", "RGR", " R "), mapOf('R' to "redstone", 'G' to "glowstone"), true))
        put("repeater", Recipe("repeater", 1, listOf("TRT", "###"), mapOf('T' to "redstone_torch", 'R' to "redstone", '#' to "stone"), true))

        // --- Utility blocks ---
        put("hopper", Recipe("hopper", 1, listOf("I I", "ICI", " I "), mapOf('I' to "iron_ingot", 'C' to "chest"), true))
        put("dispenser", Recipe("dispenser", 1, listOf("###", "#B#", "#R#"), mapOf('#' to "cobblestone", 'B' to "bow", 'R' to "redstone"), true))
        put("dropper", Recipe("dropper", 1, listOf("###", "# #", "#R#"), mapOf('#' to "cobblestone", 'R' to "redstone"), true))
        put("piston", Recipe("piston", 1, listOf("###", "CIC", "CRC"), mapOf('#' to "any_planks", 'C' to "cobblestone", 'I' to "iron_ingot", 'R' to "redstone"), true))

        // --- Misc tools ---
        put("bucket", Recipe("bucket", 1, listOf("# #", " # "), mapOf('#' to "iron_ingot"), true))
        put("ladder", Recipe("ladder", 3, listOf("# #", "###", "# #"), mapOf('#' to "stick"), true))
        put("bowl", Recipe("bowl", 4, listOf("# #", " # "), mapOf('#' to "any_planks"), true))
    }

    fun matchesIngredient(required: String, available: String): Boolean {
        if (required == available) return true
        if (INGREDIENT_ALTERNATIVES[required]?.contains(available) == true) return true
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
            // Two-pass pick: exact-name matches first, then variant fallbacks.
            // Without this, an `oak_planks` requirement could grab `spruce_planks`
            // even when oak is available, and (worse for variant outputs)
            // could attempt the craft with the wrong variant.
            for (pass in 0..1) {
                if (remaining <= 0) break
                for ((invItem, invCount) in inventory) {
                    if (remaining <= 0) break
                    val matches = if (pass == 0) invItem == ingredient
                                  else matchesIngredient(ingredient, invItem) && invItem != ingredient
                    if (!matches) continue
                    val claimed = resolved[invItem] ?: 0
                    val available = invCount - claimed
                    if (available <= 0) continue
                    val take = minOf(available, remaining)
                    resolved[invItem] = claimed + take
                    remaining -= take
                }
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
            val alts = INGREDIENT_ALTERNATIVES[name]
            when {
                suffix != null -> "${count}x ${suffix.removePrefix("_")} (any variant)"
                alts != null -> "${count}x $name (or ${alts.joinToString(" or ")})"
                else -> "${count}x $name"
            }
        }
    }
}
