package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.item.ItemStack
import net.minecraft.registry.Registries
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * Shared slot mechanics for the forging-style stations (anvil, smithing
 * table, enchanting table). Each opens a ScreenHandler whose trailing 36
 * slots are the player inventory (main + hotbar); the leading slots are the
 * station's inputs/output.
 *
 * The click model mirrors FurnaceRoute's insert/extract exactly — PICKUP the
 * source stack, right-click N times into the target, drop the remainder back;
 * QUICK_MOVE to pull a result out. The one difference: the inventory range is
 * computed dynamically as `slots.size - 36` (the way ChestRoute handles single
 * vs. double chests), so one helper set serves all three station layouts
 * without hardcoding per-handler slot constants.
 */
internal object StationMenu {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.station")!!

    /** PSH-relative range of the player-inventory portion (main + hotbar). */
    fun invRange(handler: ScreenHandler): IntRange {
        val start = (handler.slots.size - 36).coerceAtLeast(0)
        return start until handler.slots.size
    }

    fun emptySlot(): Map<String, Any?> = mapOf("item" to null, "count" to 0)

    fun stackInfo(stack: ItemStack?): Map<String, Any?> {
        if (stack == null || stack.isEmpty) return emptySlot()
        return mapOf(
            "item" to Registries.ITEM.getId(stack.item).path,
            "count" to stack.count,
        )
    }

    fun findSourceSlot(handler: ScreenHandler, itemName: String): Pair<Int, Int>? {
        for (i in invRange(handler)) {
            val slot = handler.slots.getOrNull(i) ?: continue
            val stack = slot.stack
            if (stack.isEmpty) continue
            if (Registries.ITEM.getId(stack.item).path == itemName) return i to stack.count
        }
        return null
    }

    /**
     * Place up to [amount] of [itemName] from the player inventory into a
     * single station [targetSlot]. Strict equality match. Returns
     * (placed, errorOrNull).
     */
    fun insertExact(
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
            } ?: return placed to "No $itemName in inventory; placed $placed/$amount"
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

    /** Shift-click [slot] back into the inventory if it holds anything. */
    fun shiftMoveIfNonEmpty(handler: ScreenHandler, slot: Int): Boolean {
        val nonEmpty = TickThread.submitAndWait(timeoutMs = 1_000) {
            val s = handler.slots.getOrNull(slot) ?: return@submitAndWait false
            !s.stack.isEmpty
        }
        if (!nonEmpty) return false
        try {
            tickClick(handler, slot, button = 0, action = SlotActionType.QUICK_MOVE, ticks = 2)
        } catch (t: Throwable) {
            log.warn("station shift-click slot {} threw: {}", slot, t.message)
        }
        return true
    }

    fun snapshot(handler: ScreenHandler, slot: Int): Map<String, Any?> =
        TickThread.submitAndWait(timeoutMs = 1_000) {
            stackInfo(handler.slots.getOrNull(slot)?.stack)
        }

    fun tickClick(
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

    /** Player XP level, read on the tick thread. 0 if no player. */
    fun playerLevel(): Int = TickThread.submitAndWait(timeoutMs = 1_000) {
        MinecraftClient.getInstance().player?.experienceLevel ?: 0
    }

    /** Tick-thread-only. Nearest block whose id is in [ids] within [radius]. */
    fun findNearestBlock(ids: Set<String>, radius: Int): BlockPos? {
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
                    if (Registries.BLOCK.getId(state.block).path !in ids) continue
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

    /** Returns null on success, or an error response if [pos] can't be reached. */
    fun ensureInReach(pos: BlockPos, label: String): BridgeResponse? {
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(player, pos, WorldHelpers.NAV_REACH)
        }
        if (inReach) return null
        val nav = Navigation.navigateNear(pos, WorldHelpers.NAV_REACH)
        if (nav is Navigation.Result.Failed) {
            return HttpBridge.err(
                "Couldn't reach $label at (${pos.x}, ${pos.y}, ${pos.z}): ${nav.reason}",
            )
        }
        return null
    }

    fun parsePos(x: Int?, y: Int?, z: Int?): BlockPos? =
        if (x == null || y == null || z == null) null else BlockPos(x, y, z)
}
