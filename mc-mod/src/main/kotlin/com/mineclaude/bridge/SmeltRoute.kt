package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.registry.Registries
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.state.property.Properties
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory
import kotlin.math.ceil

/**
 * `POST /smelt {item, count}` — smelt items in a nearby furnace via
 * real container clicks.
 *
 * Mirrors `bridge.minescript_api.smelt_item` line-by-line:
 *
 *   1. Find the smelting recipe; cap count at 64 (furnace slot limit).
 *   2. Resolve input + fuel from player inventory (variant-aware).
 *   3. Compute smelt_count + fuel_needed.
 *   4. Find a nearby furnace, navigate if out of reach.
 *   5. Open the FurnaceScreenHandler.
 *   6. Pre-extract any pre-existing output (so the before/after delta is
 *      meaningful).
 *   7. Insert input into slot 0, fuel into slot 1, via the 3-click
 *      pattern (left-click source → right-click N times into target →
 *      left-click source to re-stack remainder; step 3 skipped if
 *      step 2 drained the cursor).
 *   8. Poll `BlockState.get(Properties.LIT)` until the furnace is no
 *      longer lit, with a deadline of `count * 10 + 5` seconds.
 *   9. Shift-click slot 2 to extract the output, verify via inventory
 *      delta.
 *
 * Container APIs keep working while the furnace menu is open, so we
 * don't close-and-reopen for the lit poll. Cleanup `closeHandledScreen`
 * runs in a `finally` via `MenuClicker.withOpenedBlock`.
 */
object SmeltRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.smelt")!!

    /** PSH slot indices in AbstractFurnaceScreenHandler. */
    private const val FURNACE_INPUT_SLOT = 0
    private const val FURNACE_FUEL_SLOT = 1
    private const val FURNACE_OUTPUT_SLOT = 2

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/smelt") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty().removePrefix("minecraft:")
        var count = (body["count"] as? Number)?.toInt() ?: 1
        if (item.isEmpty()) {
            return HttpBridge.err("Missing 'item' parameter", status = 400)
        }
        count = count.coerceAtMost(64)  // furnace input slot cap
        if (count <= 0) {
            return HttpBridge.ok(mapOf("smelted" to 0, "method" to "real"))
        }

        val recipe = Recipes.getSmeltingRecipe(item)
            ?: return HttpBridge.err("Unknown smelting recipe: $item")

        // Find furnace position.
        val furnacePos = TickThread.submitAndWait(timeoutMs = 1_000) {
            findNearestBlock("furnace", radius = 16)
        } ?: return HttpBridge.err("No furnace nearby. Place one first.")

        // Resolve input + fuel.
        val have = TickThread.submitAndWait(timeoutMs = 1_000) { CraftRoute.inventoryCounts() }
        val inputItem = recipe.input
        val actualInput = have.entries.firstOrNull {
            Recipes.matchesSmeltingInput(inputItem, it.key) && it.value > 0
        }?.key ?: return HttpBridge.err("No $inputItem in inventory.")

        val availableInput = have[actualInput] ?: 0
        var smeltCount = minOf(count, availableInput)

        val fuelEntry = have.entries.firstOrNull {
            Recipes.getFuelValue(it.key) > 0 && it.value > 0
        } ?: return HttpBridge.err("No fuel in inventory (need coal, logs, planks, etc.).")
        val actualFuel = fuelEntry.key
        val fuelValue = Recipes.getFuelValue(actualFuel)

        var fuelNeeded = ceil(smeltCount / fuelValue).toInt()
        val fuelAvailable = have[actualFuel] ?: 0
        if (fuelAvailable < fuelNeeded) {
            smeltCount = (fuelAvailable * fuelValue).toInt()
            fuelNeeded = fuelAvailable
            if (smeltCount <= 0) {
                return HttpBridge.err("Not enough fuel.")
            }
        }

        // Navigate if out of reach.
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(player, furnacePos, WorldHelpers.NAV_REACH)
        }
        if (!inReach) {
            if (!Navigation.navigateNear(furnacePos, WorldHelpers.NAV_REACH)) {
                return HttpBridge.err("Could not reach furnace.")
            }
        }

        val beforeOutput = TickThread.submitAndWait(timeoutMs = 1_000) {
            CraftRoute.inventoryCounts()[recipe.output] ?: 0
        }
        var delta = 0
        var err: String? = null

        try {
            MenuClicker.withOpenedBlock(furnacePos) { handler ->
                // Pre-extract any leftover output so before/after delta is meaningful.
                preExtractOutput(handler)

                val (placedInput, ierr) = insertStackIntoSlot(
                    handler = handler,
                    itemName = actualInput,
                    amount = smeltCount,
                    targetSlot = FURNACE_INPUT_SLOT,
                    invRange = MenuClicker.FURNACE_INV_RANGE,
                    matches = { req, avail -> Recipes.matchesSmeltingInput(req, avail) },
                )
                if (placedInput < smeltCount) {
                    err = ierr ?: "Only placed $placedInput/$smeltCount $actualInput in input slot"
                    return@withOpenedBlock
                }

                val (placedFuel, ferr) = insertStackIntoSlot(
                    handler = handler,
                    itemName = actualFuel,
                    amount = fuelNeeded,
                    targetSlot = FURNACE_FUEL_SLOT,
                    invRange = MenuClicker.FURNACE_INV_RANGE,
                    matches = { req, avail -> req == avail },
                )
                if (placedFuel < fuelNeeded) {
                    err = ferr ?: "Only placed $placedFuel/$fuelNeeded $actualFuel in fuel slot"
                    return@withOpenedBlock
                }

                // Wait for smelting via lit-state polling. ~10s per item +5s buffer.
                val smeltTimeMs = (smeltCount * 10 + 5) * 1000L
                val deadline = System.currentTimeMillis() + smeltTimeMs
                Thread.sleep(2_000)  // let the furnace light
                while (System.currentTimeMillis() < deadline) {
                    val lit = TickThread.submitAndWait(timeoutMs = 1_000) {
                        val world = MinecraftClient.getInstance().world ?: return@submitAndWait true
                        val state = world.getBlockState(furnacePos)
                        try {
                            state.get(Properties.LIT)
                        } catch (t: Throwable) {
                            true  // unknown — assume still cooking
                        }
                    }
                    if (!lit) break
                    Thread.sleep(2_000)
                }
                Thread.sleep(500)  // let the final tick's output land

                // Extract output.
                try {
                    tickClick(handler, FURNACE_OUTPUT_SLOT, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
                } catch (t: Throwable) {
                    err = "Failed to shift-click output: ${t.message}"
                    return@withOpenedBlock
                }
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        val afterOutput = TickThread.submitAndWait(timeoutMs = 1_000) {
            CraftRoute.inventoryCounts()[recipe.output] ?: 0
        }
        delta = afterOutput - beforeOutput
        if (delta <= 0) {
            return HttpBridge.err(err ?: "Output extraction yielded nothing (inventory full?)")
        }

        log.info(
            "smelt: smelted {} {} from {} with {}x {} ({} requested)",
            delta, recipe.output, actualInput, fuelNeeded, actualFuel, smeltCount,
        )
        return HttpBridge.ok(
            mapOf(
                "smelted" to delta,
                "output" to recipe.output,
                "fuel_used" to fuelNeeded,
                "method" to "real",
            ),
            "Smelted $delta ${recipe.output}",
        )
    }

    /** Shift-click any pre-existing item out of the output slot. */
    private fun preExtractOutput(handler: ScreenHandler) {
        val hasItem = TickThread.submitAndWait(timeoutMs = 1_000) {
            val stack = handler.slots.getOrNull(FURNACE_OUTPUT_SLOT)?.stack
            stack != null && !stack.isEmpty
        }
        if (hasItem) {
            try {
                tickClick(handler, FURNACE_OUTPUT_SLOT, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
            } catch (t: Throwable) {
                log.warn("smelt: pre-extract output threw: {}", t.message)
            }
        }
    }

    /**
     * Move [amount] units of [itemName] from player inventory into a
     * single container slot. Mirrors legacy `_insert_stack_into_container_slot`.
     * Click model per source stack:
     *   1. left-click source — pick up entire stack
     *   2. right-click target N times (N = min(stack_count, remaining))
     *   3. left-click source — drop remainder back; re-stacks. Skipped
     *      if step 2 drained the cursor.
     * Tries up to 4 source stacks so a request split across slots
     * (partial stacks) still succeeds.
     */
    private fun insertStackIntoSlot(
        handler: ScreenHandler,
        itemName: String,
        amount: Int,
        targetSlot: Int,
        invRange: IntRange,
        matches: (String, String) -> Boolean,
    ): Pair<Int, String?> {
        if (amount <= 0) return 0 to null
        var placed = 0
        var remaining = amount

        for (attempt in 0 until 4) {
            if (remaining <= 0) return placed to null

            val source = TickThread.submitAndWait(timeoutMs = 1_000) {
                findSourceSlot(handler, itemName, invRange, matches)
            } ?: return placed to (
                "No more $itemName in inventory (PSH slots ${invRange.first}-${invRange.last}); " +
                    "placed $placed/$amount"
                )

            val (invSlot, stackCount) = source
            try {
                tickClick(handler, invSlot, button = 0, action = SlotActionType.PICKUP)
            } catch (t: Throwable) {
                return placed to "pickup click failed: ${t.message}"
            }

            val n = minOf(stackCount, remaining)
            try {
                for (i in 0 until n) {
                    tickClick(handler, targetSlot, button = 1, action = SlotActionType.PICKUP)
                }
            } catch (t: Throwable) {
                return placed to "deposit click failed after $placed placed: ${t.message}"
            }
            placed += n
            remaining -= n

            if (n < stackCount) {
                try {
                    tickClick(handler, invSlot, button = 0, action = SlotActionType.PICKUP)
                } catch (t: Throwable) {
                    return placed to "re-stack click failed after $placed placed: ${t.message}"
                }
            }
            @Suppress("UNUSED_EXPRESSION") attempt
        }

        return if (remaining > 0) {
            placed to "Could not place all $amount $itemName; placed $placed"
        } else {
            placed to null
        }
    }

    private fun findSourceSlot(
        handler: ScreenHandler,
        itemName: String,
        invRange: IntRange,
        matches: (String, String) -> Boolean,
    ): Pair<Int, Int>? {
        for (i in invRange) {
            val slot = handler.slots.getOrNull(i) ?: continue
            val stack = slot.stack
            if (stack.isEmpty) continue
            val name = Registries.ITEM.getId(stack.item).path
            if (matches(itemName, name)) return i to stack.count
        }
        return null
    }

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

    /** Same simple cube-scan as CraftRoute uses. */
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
}
