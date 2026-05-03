package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.item.ItemStack
import net.minecraft.registry.Registries
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.state.property.Properties
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * Three primitives that mirror real furnace mechanics one-to-one:
 *
 *   `POST /furnace/load    {input_item, input_count, fuel_item, fuel_count, [x,y,z]}`
 *   `GET  /furnace/inspect [?x&y&z]`
 *   `POST /furnace/extract [{x,y,z}]`
 *
 * The bridge does NO recipe lookup, NO fuel selection, NO smelt-count
 * inference. Whatever the caller asked to load is loaded; whatever is in
 * the slots when extract runs is what comes back out. This replaces the
 * old `/smelt` endpoint, whose auto-fuel-pick + recipe-keyed-by-output
 * shape was the root cause of two distinct production bugs.
 *
 * Load is non-blocking: it inserts and returns immediately, leaving the
 * furnace cooking on its own. Inspect reads the BlockEntity directly so
 * polling is cheap (no UI flicker). Extract pulls slot 2 (output), then
 * slot 0 (input remainder), then slot 1 (fuel remainder) — equivalent to
 * shift-clicking each slot — and reports what came out per slot.
 */
object FurnaceRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.furnace")!!

    private const val INPUT_SLOT = 0
    private const val FUEL_SLOT = 1
    private const val OUTPUT_SLOT = 2

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/furnace/load") { ex -> handleLoad(ex) }
        bridge.addRoute("GET", "/furnace/inspect") { ex -> handleInspect(ex) }
        bridge.addRoute("POST", "/furnace/extract") { ex -> handleExtract(ex) }
    }

    // -----------------------------------------------------------------------
    // /furnace/load
    // -----------------------------------------------------------------------

    private fun handleLoad(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val inputItem = (body["input_item"] as? String).orEmpty().removePrefix("minecraft:")
        val fuelItem = (body["fuel_item"] as? String).orEmpty().removePrefix("minecraft:")
        val inputCount = (body["input_count"] as? Number)?.toInt() ?: 0
        val fuelCount = (body["fuel_count"] as? Number)?.toInt() ?: 0
        if (inputItem.isEmpty() || fuelItem.isEmpty()) {
            return HttpBridge.err("Missing 'input_item' or 'fuel_item'", status = 400)
        }
        if (inputCount <= 0 || fuelCount <= 0) {
            return HttpBridge.err(
                "input_count and fuel_count must be > 0",
                status = 400,
            )
        }

        val furnacePos = resolveFurnacePos(body)
            ?: return HttpBridge.err("No furnace nearby. Place one first.")

        // Preflight inventory check on the tick thread.
        val have = TickThread.submitAndWait(timeoutMs = 1_000) { CraftRoute.inventoryCounts() }
        val haveInput = have[inputItem] ?: 0
        if (haveInput < inputCount) {
            return HttpBridge.err(
                "Not enough $inputItem in inventory: need $inputCount, have $haveInput",
            )
        }
        val haveFuel = have[fuelItem] ?: 0
        if (haveFuel < fuelCount) {
            return HttpBridge.err(
                "Not enough $fuelItem in inventory: need $fuelCount, have $haveFuel",
            )
        }

        ensureInReach(furnacePos)?.let { return it }

        var err: String? = null
        try {
            MenuClicker.withOpenedBlock(furnacePos) { handler ->
                val (placedInput, ierr) = insertExact(
                    handler, inputItem, inputCount, INPUT_SLOT,
                )
                if (placedInput < inputCount) {
                    err = ierr ?: "Only placed $placedInput/$inputCount $inputItem in input slot"
                    return@withOpenedBlock
                }
                val (placedFuel, ferr) = insertExact(
                    handler, fuelItem, fuelCount, FUEL_SLOT,
                )
                if (placedFuel < fuelCount) {
                    err = ferr ?: "Only placed $placedFuel/$fuelCount $fuelItem in fuel slot"
                    return@withOpenedBlock
                }
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }
        err?.let { return HttpBridge.err(it) }

        log.info(
            "furnace/load: {}x {} + {}x {} into furnace at {}",
            inputCount, inputItem, fuelCount, fuelItem, furnacePos,
        )
        return HttpBridge.ok(
            mapOf(
                "loaded_input" to inputCount,
                "loaded_fuel" to fuelCount,
                "position" to mapOf("x" to furnacePos.x, "y" to furnacePos.y, "z" to furnacePos.z),
                "method" to "real",
            ),
            "Loaded $inputCount $inputItem and $fuelCount $fuelItem into furnace",
        )
    }

    // -----------------------------------------------------------------------
    // /furnace/inspect
    // -----------------------------------------------------------------------

    private fun handleInspect(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val explicitPos = parseExplicitPos(
            params["x"]?.toIntOrNull(),
            params["y"]?.toIntOrNull(),
            params["z"]?.toIntOrNull(),
        )
        val furnacePos = explicitPos ?: TickThread.submitAndWait(timeoutMs = 1_000) {
            findNearestFurnace(radius = 16)
        } ?: return HttpBridge.err("No furnace nearby.")

        // `lit` is a regular block property and IS synced to the client
        // independently of the menu, so we read it cheaply up front.
        val lit = TickThread.submitAndWait(timeoutMs = 1_000) {
            val world = MinecraftClient.getInstance().world ?: return@submitAndWait false
            try {
                world.getBlockState(furnacePos).get(Properties.LIT)
            } catch (_: Throwable) { false }
        }

        ensureInReach(furnacePos)?.let { return it }

        // Slot contents and cook timing are server-authoritative — the
        // client only learns them once the screen handler is open. So
        // briefly open the menu, snapshot, close. Mirrors the legacy
        // Python path. The ~200ms UI flicker is the price of accuracy.
        var input = emptySlot()
        var fuel = emptySlot()
        var output = emptySlot()
        try {
            MenuClicker.withOpenedBlock(furnacePos) { handler ->
                val snap = TickThread.submitAndWait(timeoutMs = 1_000) {
                    Triple(
                        stackInfo(handler.slots.getOrNull(INPUT_SLOT)?.stack),
                        stackInfo(handler.slots.getOrNull(FUEL_SLOT)?.stack),
                        stackInfo(handler.slots.getOrNull(OUTPUT_SLOT)?.stack),
                    )
                }
                input = snap.first
                fuel = snap.second
                output = snap.third
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        return HttpBridge.ok(
            mapOf(
                "position" to mapOf("x" to furnacePos.x, "y" to furnacePos.y, "z" to furnacePos.z),
                "lit" to lit,
                "input" to input,
                "fuel" to fuel,
                "output" to output,
                "method" to "real",
            ),
            "Furnace inspected",
        )
    }

    // -----------------------------------------------------------------------
    // /furnace/extract
    // -----------------------------------------------------------------------

    private fun handleExtract(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val furnacePos = resolveFurnacePos(body)
            ?: return HttpBridge.err("No furnace nearby.")

        ensureInReach(furnacePos)?.let { return it }

        // Slot contents are server-authoritative and only visible to the
        // client once the screen handler is open — so the snapshot has to
        // happen INSIDE withOpenedBlock, not before. (An earlier version
        // snapshotted via getBlockEntity(pos).getStack(...) on the closed
        // BE; that always returned 0 because the client hasn't received
        // those slots yet.)
        var outputBefore = emptySlot()
        var inputBefore = emptySlot()
        var fuelBefore = emptySlot()

        try {
            MenuClicker.withOpenedBlock(furnacePos) { handler ->
                val snap = TickThread.submitAndWait(timeoutMs = 1_000) {
                    Triple(
                        stackInfo(handler.slots.getOrNull(OUTPUT_SLOT)?.stack),
                        stackInfo(handler.slots.getOrNull(INPUT_SLOT)?.stack),
                        stackInfo(handler.slots.getOrNull(FUEL_SLOT)?.stack),
                    )
                }
                outputBefore = snap.first
                inputBefore = snap.second
                fuelBefore = snap.third

                // Order matters: pull output first so any final tick of
                // smelting that lands during the click doesn't get pulled
                // back as "input remainder."
                shiftMoveIfNonEmpty(handler, OUTPUT_SLOT)
                shiftMoveIfNonEmpty(handler, INPUT_SLOT)
                shiftMoveIfNonEmpty(handler, FUEL_SLOT)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        log.info(
            "furnace/extract: output={}, input_left={}, fuel_left={} at {}",
            outputBefore, inputBefore, fuelBefore, furnacePos,
        )
        return HttpBridge.ok(
            mapOf(
                "position" to mapOf("x" to furnacePos.x, "y" to furnacePos.y, "z" to furnacePos.z),
                "output" to outputBefore,
                "input_left" to inputBefore,
                "fuel_left" to fuelBefore,
                "method" to "real",
            ),
            extractMessage(outputBefore, inputBefore, fuelBefore),
        )
    }

    // -----------------------------------------------------------------------
    // helpers
    // -----------------------------------------------------------------------

    private fun resolveFurnacePos(body: Map<String, Any?>): BlockPos? {
        val explicit = parseExplicitPos(
            (body["x"] as? Number)?.toInt(),
            (body["y"] as? Number)?.toInt(),
            (body["z"] as? Number)?.toInt(),
        )
        if (explicit != null) return explicit
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            findNearestFurnace(radius = 16)
        }
    }

    private fun parseExplicitPos(x: Int?, y: Int?, z: Int?): BlockPos? {
        if (x == null || y == null || z == null) return null
        return BlockPos(x, y, z)
    }

    /** Returns null on success, or a populated error response if we can't reach [pos]. */
    private fun ensureInReach(pos: BlockPos): BridgeResponse? {
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(player, pos, WorldHelpers.NAV_REACH)
        }
        if (inReach) return null
        val nav = Navigation.navigateNear(pos, WorldHelpers.NAV_REACH)
        if (nav is Navigation.Result.Failed) {
            return HttpBridge.err(
                "Couldn't reach furnace at (${pos.x}, ${pos.y}, ${pos.z}): ${nav.reason}",
            )
        }
        return null
    }

    /** Tick-thread-only. Scans a cube around the player for a furnace block. */
    private fun findNearestFurnace(radius: Int): BlockPos? {
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
                    val id = Registries.BLOCK.getId(state.block).path
                    if (id != "furnace" && id != "lit_furnace") continue
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

    private fun emptySlot(): Map<String, Any?> = mapOf("item" to null, "count" to 0)

    private fun stackInfo(stack: ItemStack?): Map<String, Any?> {
        if (stack == null || stack.isEmpty) return emptySlot()
        return mapOf(
            "item" to Registries.ITEM.getId(stack.item).path,
            "count" to stack.count,
        )
    }

    private fun extractMessage(
        output: Map<String, Any?>,
        input: Map<String, Any?>,
        fuel: Map<String, Any?>,
    ): String {
        val parts = mutableListOf<String>()
        val outCount = output["count"] as? Int ?: 0
        if (outCount > 0) parts += "${outCount} ${output["item"]}"
        val inCount = input["count"] as? Int ?: 0
        if (inCount > 0) parts += "${inCount} ${input["item"]} (input left)"
        val fuelCount = fuel["count"] as? Int ?: 0
        if (fuelCount > 0) parts += "${fuelCount} ${fuel["item"]} (fuel left)"
        return if (parts.isEmpty()) "Furnace was empty"
        else "Extracted " + parts.joinToString(", ")
    }

    /**
     * Place exactly [amount] of [itemName] from the player inventory into
     * a single container [targetSlot]. Strict equality match — no variant
     * fuzzing. Mirrors the click model in SmeltRoute.insertStackIntoSlot
     * but doesn't inherit its "smelting input variant" lookup since this
     * route is a pure pass-through.
     *
     * Click model per source stack:
     *   1. PICKUP src — pick up the entire stack onto the cursor
     *   2. PICKUP-RIGHT target × N — drop one at a time into the slot
     *   3. PICKUP src — drop cursor remainder back; same item re-stacks.
     *      Skipped when step 2 drained the cursor.
     */
    private fun insertExact(
        handler: ScreenHandler,
        itemName: String,
        amount: Int,
        targetSlot: Int,
    ): Pair<Int, String?> {
        if (amount <= 0) return 0 to null
        var placed = 0
        var remaining = amount

        for (attempt in 0 until 4) {
            if (remaining <= 0) return placed to null

            val source = TickThread.submitAndWait(timeoutMs = 1_000) {
                findSourceSlot(handler, itemName)
            } ?: return placed to (
                "No more $itemName in inventory; placed $placed/$amount"
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

        return if (remaining > 0) placed to "Could not place all $amount $itemName; placed $placed"
        else placed to null
    }

    private fun findSourceSlot(handler: ScreenHandler, itemName: String): Pair<Int, Int>? {
        for (i in MenuClicker.FURNACE_INV_RANGE) {
            val slot = handler.slots.getOrNull(i) ?: continue
            val stack = slot.stack
            if (stack.isEmpty) continue
            if (Registries.ITEM.getId(stack.item).path == itemName) return i to stack.count
        }
        return null
    }

    private fun shiftMoveIfNonEmpty(handler: ScreenHandler, slot: Int) {
        val nonEmpty = TickThread.submitAndWait(timeoutMs = 1_000) {
            val s = handler.slots.getOrNull(slot) ?: return@submitAndWait false
            !s.stack.isEmpty
        }
        if (!nonEmpty) return
        try {
            tickClick(handler, slot, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
        } catch (t: Throwable) {
            log.warn("furnace/extract: shift-click slot {} threw: {}", slot, t.message)
        }
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
}
