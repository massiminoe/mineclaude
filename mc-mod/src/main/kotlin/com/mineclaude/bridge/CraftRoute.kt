package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.registry.Registries
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory
import kotlin.math.ceil

/**
 * `POST /craft {item, count}` — open a crafting menu and click ingredients
 * into place to actually craft items, no `/give` workarounds.
 *
 * For 3x3 recipes (`needs_table`): finds a nearby crafting_table block
 * via the same scan path NearbyRoutes uses, navigates if out of reach,
 * opens it via `interactionManager.interactBlock`, runs crafts in the
 * resulting CraftingScreenHandler.
 *
 * For 2x2 recipes: clicks directly into the player's PlayerScreenHandler
 * crafting slots (PSH 1..4). PlayerScreenHandler is always the active
 * handler when no other screen is up, and clickSlot works against it
 * without any UI — but MenuClicker displays the InventoryScreen anyway
 * (cosmetic, client-side only) so the craft is visible in the recording.
 *
 * Click model per ingredient (mirrors legacy `_perform_crafts_in_open_menu`):
 *   1. PICKUP button=0 on source — picks up entire stack to cursor
 *   2. PICKUP button=1 on grid slot — drops 1 from cursor into grid
 *   3. PICKUP button=0 on source — drops cursor stack back, re-stacks
 * Cursor is empty between placements so the same source can feed
 * multiple grid slots in the same iteration.
 */
object CraftRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.craft")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/craft") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty().removePrefix("minecraft:")
        val count = (body["count"] as? Number)?.toInt() ?: 1
        if (item.isEmpty()) {
            return HttpBridge.err("Missing 'item' parameter", status = 400)
        }
        if (count <= 0) {
            return HttpBridge.ok(mapOf("crafted" to 0, "method" to "real"))
        }

        val recipe = Recipes.getRecipe(item)
            ?: return HttpBridge.err("Unknown recipe: $item. Cannot craft without a known recipe.")

        val required = Recipes.getRequiredIngredients(item, count)
            ?: return HttpBridge.err("Cannot calculate ingredients for $item")

        // Preflight: do we have enough? Inventory snapshot on the tick thread.
        val have = TickThread.submitAndWait(timeoutMs = 1_000) { inventoryCounts() }
        val resolved = Recipes.resolveIngredients(required, have)
        if (resolved == null) {
            val needStr = Recipes.formatRequiredIngredients(required)
            val haveStr = if (have.isEmpty()) "nothing"
                else have.entries.joinToString(", ") { (k, v) -> "${v}x $k" }
            return HttpBridge.err(
                "Cannot craft $item: missing ingredients. Need: $needStr. Have: $haveStr."
            )
        }

        val craftsNeeded = ceil(count.toDouble() / recipe.outputCount).toInt()
        return if (recipe.needsTable) {
            craftViaTable(recipe, craftsNeeded)
        } else {
            craftViaInventory(recipe, craftsNeeded)
        }
    }

    /**
     * Open a nearby crafting_table and run [craftsNeeded] iterations of
     * [recipe] in the resulting CraftingScreenHandler. Cleanup-grid
     * pass before close is load-bearing — the table doesn't persist
     * grid contents, so leftovers drop as item entities.
     */
    private fun craftViaTable(recipe: Recipes.Recipe, craftsNeeded: Int): BridgeResponse {
        val gridSlots = Recipes.patternToTableSlots(recipe)

        // Find nearest crafting_table on the tick thread.
        val tablePos = TickThread.submitAndWait(timeoutMs = 1_000) {
            findNearestBlock("crafting_table", radius = 16)
        } ?: return HttpBridge.err(
            "Cannot craft ${recipe.output}: no crafting table nearby. Place one first."
        )

        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(player, tablePos, WorldHelpers.NAV_REACH)
        }
        if (!inReach) {
            val nav = Navigation.navigateNear(tablePos, WorldHelpers.NAV_REACH)
            if (nav is Navigation.Result.Failed) {
                return HttpBridge.err(
                    "Couldn't reach crafting table at (${tablePos.x}, ${tablePos.y}, ${tablePos.z}): ${nav.reason}",
                )
            }
        }

        var crafted = 0
        var err: String? = null
        try {
            MenuClicker.withOpenedBlock(tablePos) { handler ->
                val (c, e) = performCraftsInOpenMenu(
                    handler = handler,
                    recipe = recipe,
                    craftsNeeded = craftsNeeded,
                    gridSlots = gridSlots,
                    invRange = MenuClicker.TABLE_INV_RANGE,
                )
                crafted = c
                err = e
                cleanupGridIntoInventory(handler, gridSlots.keys)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        return craftResultResponse(recipe, craftsNeeded, crafted, err, "crafting table")
    }

    /**
     * Click into the 2x2 PlayerScreenHandler crafter (PSH slots 1..4).
     * Cleanup pass shift-clicks any leftover grid items back to
     * inventory before returning.
     */
    private fun craftViaInventory(recipe: Recipes.Recipe, craftsNeeded: Int): BridgeResponse {
        val gridSlots = Recipes.patternToInventorySlots(recipe)

        var crafted = 0
        var err: String? = null
        try {
            MenuClicker.withOpenedInventory { handler ->
                val (c, e) = performCraftsInOpenMenu(
                    handler = handler,
                    recipe = recipe,
                    craftsNeeded = craftsNeeded,
                    gridSlots = gridSlots,
                    invRange = MenuClicker.PLAYER_INV_RANGE,
                )
                crafted = c
                err = e
                cleanupGridIntoInventory(handler, gridSlots.keys)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        return craftResultResponse(recipe, craftsNeeded, crafted, err, "inventory crafter")
    }

    private fun craftResultResponse(
        recipe: Recipes.Recipe,
        craftsNeeded: Int,
        crafted: Int,
        err: String?,
        via: String,
    ): BridgeResponse {
        val totalOutput = crafted * recipe.outputCount
        if (crafted == 0 && err != null) {
            return HttpBridge.err(err)
        }
        log.info("craft: crafted {} {} via {} ({}/{} iterations)", totalOutput, recipe.output, via, crafted, craftsNeeded)
        val data = mutableMapOf<String, Any>(
            "crafted" to totalOutput,
            "method" to "real",
        )
        if (err != null) data["error"] = err
        return HttpBridge.ok(data, "Crafted $totalOutput ${recipe.output}")
    }

    /**
     * Run [craftsNeeded] iterations against [handler]. Returns
     * (craftsCompleted, error?). Pacing matches legacy `_tick_sleep(1)`
     * — one game tick between clicks.
     */
    private fun performCraftsInOpenMenu(
        handler: ScreenHandler,
        recipe: Recipes.Recipe,
        craftsNeeded: Int,
        gridSlots: Map<Int, String>,
        invRange: IntRange,
    ): Pair<Int, String?> {
        var craftsCompleted = 0

        for (iteration in 0 until craftsNeeded) {
            // Snapshot the inventory portion as a {pshSlot: [name, remaining]} pool.
            // Decremented as ingredients are placed so one source feeds many grid slots.
            val invPool = TickThread.submitAndWait(timeoutMs = 1_000) {
                snapshotInvPool(handler, invRange)
            }

            var ingredientsPlaced = 0
            for ((gridSlot, ingredient) in gridSlots) {
                // Pick a source slot: any slot in the pool with >0 of a matching variant.
                val invSlot = invPool.entries.firstOrNull { (_, entry) ->
                    entry.count > 0 && Recipes.matchesIngredient(ingredient, entry.name)
                }?.key

                if (invSlot == null) {
                    return craftsCompleted to (
                        "Out of $ingredient after $craftsCompleted crafts " +
                            "(needed for grid slot $gridSlot)"
                        )
                }

                try {
                    // 1. left-click source — pick up entire stack
                    tickClick(handler, invSlot, button = 0, action = SlotActionType.PICKUP)
                    // 2. right-click grid — drop 1
                    tickClick(handler, gridSlot, button = 1, action = SlotActionType.PICKUP)
                    // 3. left-click source — drop remainder back (re-stacks)
                    tickClick(handler, invSlot, button = 0, action = SlotActionType.PICKUP)
                } catch (t: Throwable) {
                    return craftsCompleted to "Click failed placing $ingredient: ${t.message}"
                }
                invPool[invSlot]?.let { it.count -= 1 }
                ingredientsPlaced++
            }

            if (ingredientsPlaced != gridSlots.size) {
                return craftsCompleted to
                    "Could not place all ingredients (placed $ingredientsPlaced/${gridSlots.size})"
            }

            // Verify the output slot now shows recipe.output.
            val outputName = TickThread.submitAndWait(timeoutMs = 1_000) {
                val stack = handler.slots.getOrNull(0)?.stack
                if (stack == null || stack.isEmpty) "" else Registries.ITEM.getId(stack.item).path
            }
            if (outputName != recipe.output) {
                return craftsCompleted to (
                    "Output slot showed '$outputName', expected '${recipe.output}' " +
                        "(possible slot-layout mismatch or recipe not recognized)"
                    )
            }

            // Snapshot, shift-click extract, snapshot — assert delta >= outputCount.
            val before = TickThread.submitAndWait(timeoutMs = 1_000) {
                inventoryCounts()[recipe.output] ?: 0
            }
            try {
                tickClick(handler, slot = 0, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
            } catch (t: Throwable) {
                return craftsCompleted to "Failed to shift-click output: ${t.message}"
            }
            val after = TickThread.submitAndWait(timeoutMs = 1_000) {
                inventoryCounts()[recipe.output] ?: 0
            }
            val delta = after - before
            if (delta < recipe.outputCount) {
                return craftsCompleted to (
                    "Output extraction yielded +$delta ${recipe.output}, " +
                        "expected +${recipe.outputCount} (inventory full?)"
                    )
            }

            craftsCompleted++
            // Avoid unused-iteration-variable warning while keeping the
            // legacy iteration index available for future logging.
            @Suppress("UNUSED_EXPRESSION") iteration
        }

        return craftsCompleted to null
    }

    /**
     * Shift-click each grid slot to return any leftover ingredients back
     * into the player's inventory before the menu closes. Crafting
     * tables don't persist their grid; on close, leftovers drop as item
     * entities. Always called in a finally-style path.
     */
    private fun cleanupGridIntoInventory(handler: ScreenHandler, gridSlots: Set<Int>) {
        for (slot in gridSlots) {
            try {
                tickClick(handler, slot, button = 0, action = SlotActionType.QUICK_MOVE)
            } catch (t: Throwable) {
                log.warn("craft: cleanup click on slot {} threw: {}", slot, t.message)
            }
        }
    }

    /**
     * Submit a clickSlot on the tick thread, then sleep `ticks` MC game
     * ticks so MC has time to process the resulting screen-handler event
     * before the next click lands. Mirrors legacy `_tick_sleep(n)`.
     */
    private fun tickClick(
        handler: ScreenHandler,
        slot: Int,
        button: Int,
        action: SlotActionType,
        ticks: Int = 1,
    ) {
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val player = MinecraftClient.getInstance().player ?: error("no player")
            MenuClicker.click(player, handler, slot, button, action)
            Unit
        }
        Thread.sleep(MenuClicker.TICK_MS * ticks)
    }

    /**
     * Snapshot inventory items reachable through [handler] within
     * [invRange] (PSH-relative). Returns a mutable pool keyed by slot
     * so callers can decrement counts as ingredients are placed without
     * re-snapshotting between every click.
     */
    private data class PoolEntry(val name: String, var count: Int)

    private fun snapshotInvPool(
        handler: ScreenHandler,
        invRange: IntRange,
    ): MutableMap<Int, PoolEntry> {
        val pool = mutableMapOf<Int, PoolEntry>()
        for (i in invRange) {
            val slot = handler.slots.getOrNull(i) ?: continue
            val stack = slot.stack
            if (stack.isEmpty) continue
            val name = Registries.ITEM.getId(stack.item).path
            pool[i] = PoolEntry(name, stack.count)
        }
        return pool
    }

    /**
     * Read the player's inventory (hotbar + main + offhand) and return
     * {item_name: total_count}. Tick-thread only.
     */
    internal fun inventoryCounts(): Map<String, Int> {
        val player = MinecraftClient.getInstance().player ?: return emptyMap()
        val out = mutableMapOf<String, Int>()
        val inv = player.inventory
        // PI 0..35 (hotbar + main).
        for (i in 0 until 36) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            val name = Registries.ITEM.getId(stack.item).path
            out[name] = (out[name] ?: 0) + stack.count
        }
        // Offhand.
        val off = player.offHandStack
        if (!off.isEmpty) {
            val name = Registries.ITEM.getId(off.item).path
            out[name] = (out[name] ?: 0) + off.count
        }
        return out
    }

    /**
     * Find the BlockPos of the nearest [blockId] within [radius]. Returns
     * null if none found. Uses the same simple cube-scan as NearbyRoutes,
     * scoped by player position.
     */
    private fun findNearestBlock(blockId: String, radius: Int): BlockPos? {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return null
        val player = mc.player ?: return null
        val anchor = player.blockPos

        var best: BlockPos? = null
        var bestDistSq = Double.MAX_VALUE
        val mut = BlockPos.Mutable()
        val px = player.x; val py = player.y; val pz = player.z
        for (dx in -radius..radius) {
            for (dy in -radius..radius) {
                for (dz in -radius..radius) {
                    val bx = anchor.x + dx
                    val by = anchor.y + dy
                    val bz = anchor.z + dz
                    mut.set(bx, by, bz)
                    val state = world.getBlockState(mut)
                    if (state.isAir) continue
                    if (Registries.BLOCK.getId(state.block).path != blockId) continue
                    val ddx = bx - px; val ddy = by - py; val ddz = bz - pz
                    val distSq = ddx * ddx + ddy * ddy + ddz * ddz
                    if (distSq < bestDistSq) {
                        bestDistSq = distSq
                        best = BlockPos(bx, by, bz)
                    }
                }
            }
        }
        return best
    }

    @Suppress("unused")
    private fun ClientPlayerEntity.unused() = this
}
