package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.item.ItemStack
import net.minecraft.registry.Registries
import net.minecraft.screen.GenericContainerScreenHandler
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * Three primitives for using vanilla chests as bulk storage:
 *
 *   `POST /chest/store    {x, y, z, items: [{name, count|"all"}]}`
 *   `POST /chest/take     {x, y, z, items: [{name, count|"all"}]}`
 *   `GET  /chest/inspect  ?x&y&z`
 *
 * Coords are required — chests usually come in clusters at base, and
 * "find nearest" guesses the wrong one. Discovery is the agent's job.
 *
 * Double chests just work: opening either half via `interactBlock` fires
 * the merged `GenericContainerScreenHandler` on the server, so all 54
 * slots show up in the screen handler regardless of which half we hit.
 *
 * `count` accepts an integer or the string `"all"`. `"all"` on store =
 * everything in the player's inventory matching `name`; on take =
 * everything in the chest matching `name`. The dominant pattern is
 * "dump all my junk before mining," and forcing the agent to call
 * `getInventory` first to read counts is wasted overhead.
 *
 * Partial success is the response shape, not an error: each call returns
 * `{stored | taken: [...], skipped: [...]}`. A chest filling up mid-store
 * doesn't fail the call — the agent sees what landed and what didn't.
 */
object ChestRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.chest")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/chest/store") { ex -> handleStore(ex) }
        bridge.addRoute("POST", "/chest/take") { ex -> handleTake(ex) }
        bridge.addRoute("GET", "/chest/inspect") { ex -> handleInspect(ex) }
    }

    // -----------------------------------------------------------------------
    // /chest/store
    // -----------------------------------------------------------------------

    private fun handleStore(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val pos = parseExplicitPos(body)
            ?: return HttpBridge.err("Missing x/y/z", status = 400)
        val items = parseItemsList(body)
            ?: return HttpBridge.err(
                "Missing or invalid 'items' array (expected [{name, count|\"all\"}])",
                status = 400,
            )

        assertIsChest(pos)?.let { return it }
        ensureInReach(pos)?.let { return it }

        val stored = mutableListOf<Map<String, Any?>>()
        val skipped = mutableListOf<Map<String, Any?>>()

        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (containerRange, inventoryRange) = handlerRanges(handler)
                for ((name, countSpec) in items) {
                    val haveTotal = countItemInRange(handler, inventoryRange, name)
                    val target = resolveCount(countSpec, haveTotal)
                    if (target <= 0) {
                        skipped += mapOf(
                            "item" to name,
                            "reason" to "not in inventory",
                            "requested" to formatCountSpec(countSpec),
                        )
                        continue
                    }
                    val moved = transferBetweenRanges(
                        handler, name, target,
                        sourceRange = inventoryRange,
                        destRange = containerRange,
                    )
                    stored += mapOf("item" to name, "count" to moved)
                    if (moved < target) {
                        skipped += mapOf(
                            "item" to name,
                            "reason" to "chest full",
                            "remaining" to (target - moved),
                        )
                    }
                }
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        log.info("chest/store at {}: stored={} skipped={}", pos, stored, skipped)
        return HttpBridge.ok(
            mapOf(
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "stored" to stored,
                "skipped" to skipped,
                "method" to "real",
            ),
            summarizeTransfer(stored, skipped, verb = "Stored"),
        )
    }

    // -----------------------------------------------------------------------
    // /chest/take
    // -----------------------------------------------------------------------

    private fun handleTake(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val pos = parseExplicitPos(body)
            ?: return HttpBridge.err("Missing x/y/z", status = 400)
        val items = parseItemsList(body)
            ?: return HttpBridge.err(
                "Missing or invalid 'items' array (expected [{name, count|\"all\"}])",
                status = 400,
            )

        assertIsChest(pos)?.let { return it }
        ensureInReach(pos)?.let { return it }

        val taken = mutableListOf<Map<String, Any?>>()
        val skipped = mutableListOf<Map<String, Any?>>()

        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (containerRange, inventoryRange) = handlerRanges(handler)
                for ((name, countSpec) in items) {
                    val haveTotal = countItemInRange(handler, containerRange, name)
                    val target = resolveCount(countSpec, haveTotal)
                    if (target <= 0) {
                        skipped += mapOf(
                            "item" to name,
                            "reason" to "not in chest",
                            "requested" to formatCountSpec(countSpec),
                        )
                        continue
                    }
                    val moved = transferBetweenRanges(
                        handler, name, target,
                        sourceRange = containerRange,
                        destRange = inventoryRange,
                    )
                    taken += mapOf("item" to name, "count" to moved)
                    if (moved < target) {
                        skipped += mapOf(
                            "item" to name,
                            "reason" to "inventory full",
                            "remaining" to (target - moved),
                        )
                    }
                }
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        log.info("chest/take at {}: taken={} skipped={}", pos, taken, skipped)
        return HttpBridge.ok(
            mapOf(
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "taken" to taken,
                "skipped" to skipped,
                "method" to "real",
            ),
            summarizeTransfer(taken, skipped, verb = "Took"),
        )
    }

    // -----------------------------------------------------------------------
    // /chest/inspect
    // -----------------------------------------------------------------------

    private fun handleInspect(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val pos = parseExplicitPosFromParams(
            params["x"]?.toIntOrNull(),
            params["y"]?.toIntOrNull(),
            params["z"]?.toIntOrNull(),
        ) ?: return HttpBridge.err("Missing x/y/z query params", status = 400)

        assertIsChest(pos)?.let { return it }
        ensureInReach(pos)?.let { return it }

        // Slot contents are server-authoritative — the client only sees them
        // once the screen handler is open. Snapshot inside withOpenedBlock,
        // close on exit so a stale ScreenHandler can't wedge later writes.
        var size = 0
        val slots = mutableListOf<Map<String, Any?>>()
        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (containerRange, _) = handlerRanges(handler)
                size = containerRange.last - containerRange.first + 1
                val snap = TickThread.submitAndWait(timeoutMs = 1_000) {
                    containerRange.map { i ->
                        val stack = handler.slots.getOrNull(i)?.stack
                        slotInfo(i - containerRange.first, stack)
                    }
                }
                slots.addAll(snap)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }

        // Aggregate {item: total} for the agent's most common question.
        val totals = mutableMapOf<String, Int>()
        for (s in slots) {
            val item = s["item"] as? String ?: continue
            val count = s["count"] as? Int ?: 0
            totals[item] = (totals[item] ?: 0) + count
        }

        return HttpBridge.ok(
            mapOf(
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "size" to size,
                "slots" to slots,
                "totals" to totals,
                "method" to "real",
            ),
            "Chest at (${pos.x}, ${pos.y}, ${pos.z}) — $size slots, ${totals.size} item types",
        )
    }

    // -----------------------------------------------------------------------
    // helpers — body parsing
    // -----------------------------------------------------------------------

    private fun parseExplicitPos(body: Map<String, Any?>): BlockPos? = parseExplicitPosFromParams(
        (body["x"] as? Number)?.toInt(),
        (body["y"] as? Number)?.toInt(),
        (body["z"] as? Number)?.toInt(),
    )

    private fun parseExplicitPosFromParams(x: Int?, y: Int?, z: Int?): BlockPos? {
        if (x == null || y == null || z == null) return null
        return BlockPos(x, y, z)
    }

    /** Returns [(name, countSpec)] where countSpec is Int or "all". null on malformed. */
    private fun parseItemsList(body: Map<String, Any?>): List<Pair<String, Any>>? {
        val raw = body["items"] as? List<*> ?: return null
        val result = mutableListOf<Pair<String, Any>>()
        for (entry in raw) {
            val m = entry as? Map<*, *> ?: return null
            val name = (m["name"] as? String)?.removePrefix("minecraft:") ?: return null
            val rawCount = m["count"]
            val spec: Any = when (rawCount) {
                is Number -> rawCount.toInt()
                is String -> if (rawCount.equals("all", ignoreCase = true)) "all" else return null
                null -> "all"   // omitting count means "all" — the common case
                else -> return null
            }
            if (name.isBlank()) return null
            result += name to spec
        }
        return result
    }

    private fun resolveCount(spec: Any, haveTotal: Int): Int = when (spec) {
        is String -> haveTotal
        is Int -> minOf(spec, haveTotal).coerceAtLeast(0)
        else -> 0
    }

    private fun formatCountSpec(spec: Any): Any = spec

    // -----------------------------------------------------------------------
    // helpers — chest discovery + reach
    // -----------------------------------------------------------------------

    private fun assertIsChest(pos: BlockPos): BridgeResponse? {
        val name = TickThread.submitAndWait(timeoutMs = 1_000) {
            val world = MinecraftClient.getInstance().world ?: return@submitAndWait null
            val state = world.getBlockState(pos)
            if (state.isAir) return@submitAndWait "air"
            Registries.BLOCK.getId(state.block).path
        }
        if (name == null) {
            return HttpBridge.err("World not loaded — cannot inspect block at $pos")
        }
        if (name !in CHEST_BLOCKS) {
            return HttpBridge.err(
                "Block at (${pos.x}, ${pos.y}, ${pos.z}) is '$name', not a chest. " +
                    "Pass coords pointing at a chest or trapped_chest block."
            )
        }
        return null
    }

    private fun ensureInReach(pos: BlockPos): BridgeResponse? {
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(player, pos, WorldHelpers.NAV_REACH)
        }
        if (inReach) return null
        val nav = Navigation.navigateNear(pos, WorldHelpers.NAV_REACH)
        if (nav is Navigation.Result.Failed) {
            return HttpBridge.err(
                "Couldn't reach chest at (${pos.x}, ${pos.y}, ${pos.z}): ${nav.reason}",
            )
        }
        return null
    }

    // -----------------------------------------------------------------------
    // helpers — slot ranges + transfer
    // -----------------------------------------------------------------------

    /**
     * Returns (containerRange, inventoryRange) for a `GenericContainerScreenHandler`
     * (or any chest-like). Layout: [container slots, main inv (27), hotbar (9)].
     * Computing from `slots.size` keeps single (27) vs double (54) chests
     * uniform — both have exactly 36 trailing inventory slots.
     */
    private fun handlerRanges(handler: ScreenHandler): Pair<IntRange, IntRange> {
        val total = handler.slots.size
        val containerSize = (total - 36).coerceAtLeast(0)
        val containerRange = 0 until containerSize
        val inventoryRange = containerSize until total
        return containerRange to inventoryRange
    }

    /** Tick-thread-only sum of [name] across [range]. */
    private fun countItemInRange(handler: ScreenHandler, range: IntRange, name: String): Int =
        TickThread.submitAndWait(timeoutMs = 1_000) {
            var total = 0
            for (i in range) {
                val stack = handler.slots.getOrNull(i)?.stack ?: continue
                if (stack.isEmpty) continue
                if (Registries.ITEM.getId(stack.item).path == name) total += stack.count
            }
            total
        }

    /**
     * Move up to [target] units of [name] from [sourceRange] to [destRange].
     * Strategy:
     *  - If a source stack is fully consumed by the remaining count, shift-click
     *    the whole stack — one click per stack, fast for the "dump everything"
     *    pattern.
     *  - Else (partial — last source stack, leftover < stack count), do the
     *    careful pickup → right-click-N → return-cursor sequence so we move
     *    exactly the right number of units.
     *
     * Returns the actual count moved, which may be less than [target] if the
     * destination side fills up.
     */
    private fun transferBetweenRanges(
        handler: ScreenHandler,
        name: String,
        target: Int,
        sourceRange: IntRange,
        destRange: IntRange,
    ): Int {
        var moved = 0
        var remaining = target
        var safety = 0
        while (remaining > 0) {
            if (++safety > 64) {
                log.warn("chest transfer: safety break after $safety iterations, name=$name moved=$moved")
                break
            }
            val src = TickThread.submitAndWait(timeoutMs = 1_000) {
                findSlotWithItem(handler, sourceRange, name)
            } ?: break
            val (srcSlot, srcCount) = src

            if (remaining >= srcCount) {
                // Whole-stack move via shift-click.
                tickClick(handler, srcSlot, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
                val nowAtSrc = TickThread.submitAndWait(timeoutMs = 1_000) {
                    val s = handler.slots.getOrNull(srcSlot)?.stack
                    if (s == null || s.isEmpty) 0
                    else if (Registries.ITEM.getId(s.item).path == name) s.count
                    else 0   // shift-click swapped the slot — treat as fully consumed
                }
                val delta = srcCount - nowAtSrc
                if (delta <= 0) break   // dest side has no space for this item
                moved += delta
                remaining -= delta
            } else {
                // Partial move: pick up source stack, drop `remaining` into a
                // dest slot one click at a time, return cursor remainder.
                val dest = TickThread.submitAndWait(timeoutMs = 1_000) {
                    findDepositSlot(handler, destRange, name)
                } ?: break
                tickClick(handler, srcSlot, button = 0, action = SlotActionType.PICKUP)
                for (i in 0 until remaining) {
                    tickClick(handler, dest, button = 1, action = SlotActionType.PICKUP)
                }
                tickClick(handler, srcSlot, button = 0, action = SlotActionType.PICKUP)
                moved += remaining
                remaining = 0
            }
        }
        return moved
    }

    /** Tick-thread-only. First slot in [range] holding [name]. */
    private fun findSlotWithItem(handler: ScreenHandler, range: IntRange, name: String): Pair<Int, Int>? {
        for (i in range) {
            val stack = handler.slots.getOrNull(i)?.stack ?: continue
            if (stack.isEmpty) continue
            if (Registries.ITEM.getId(stack.item).path == name) return i to stack.count
        }
        return null
    }

    /**
     * Tick-thread-only. Pick a destination slot for partial drops:
     *  1. Prefer existing matching stacks with room (won't trigger a swap).
     *  2. Fall back to empty slots.
     *  Never returns a slot holding a different item (would swap on right-click).
     */
    private fun findDepositSlot(handler: ScreenHandler, range: IntRange, name: String): Int? {
        var firstEmpty: Int? = null
        for (i in range) {
            val slot = handler.slots.getOrNull(i) ?: continue
            val stack = slot.stack
            if (stack.isEmpty) {
                if (firstEmpty == null) firstEmpty = i
                continue
            }
            if (Registries.ITEM.getId(stack.item).path == name && stack.count < stack.maxCount) {
                return i
            }
        }
        return firstEmpty
    }

    private fun slotInfo(displayIndex: Int, stack: ItemStack?): Map<String, Any?> {
        if (stack == null || stack.isEmpty) {
            return mapOf("slot" to displayIndex, "item" to null, "count" to 0)
        }
        return mapOf(
            "slot" to displayIndex,
            "item" to Registries.ITEM.getId(stack.item).path,
            "count" to stack.count,
        )
    }

    private fun summarizeTransfer(
        moved: List<Map<String, Any?>>,
        skipped: List<Map<String, Any?>>,
        verb: String,
    ): String {
        if (moved.isEmpty() && skipped.isEmpty()) return "$verb nothing"
        val parts = mutableListOf<String>()
        for (m in moved) {
            val c = m["count"] as? Int ?: 0
            if (c > 0) parts += "${c}× ${m["item"]}"
        }
        var msg = if (parts.isEmpty()) "$verb nothing" else "$verb " + parts.joinToString(", ")
        if (skipped.isNotEmpty()) {
            val skipParts = skipped.map { "${it["item"]} (${it["reason"]})" }
            msg += "; skipped: " + skipParts.joinToString(", ")
        }
        return msg
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

    @Suppress("unused")
    private fun GenericContainerScreenHandler.unused() = this

    private val CHEST_BLOCKS = setOf("chest", "trapped_chest")
}
