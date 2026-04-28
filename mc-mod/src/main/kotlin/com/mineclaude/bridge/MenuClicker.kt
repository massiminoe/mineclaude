package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.screen.PlayerScreenHandler
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Direction
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory

/**
 * Helpers for opening, clicking inside, and reliably closing container
 * menus (crafting table, furnace, …).
 *
 * Mirrors `bridge.minescript_api`'s `_open_*` / `_close_open_screen`
 * pacing. The legacy code's tick-cadence is load-bearing — MC processes
 * screen open/close as ScreenHandler factory invocations on the *server*
 * tick after the C2S packet lands, so we wait whole ticks between
 * submissions rather than racing the response.
 *
 * # Inventory slot ranges (PSH-relative, inclusive)
 *
 * ```
 *   CraftingScreenHandler     PlayerScreenHandler       AbstractFurnaceMenu
 *     0           output       0           output         0  input
 *     1..9        3x3 grid     1..4        2x2 grid       1  fuel
 *    10..36       main inv     5..8        armor          2  output
 *    37..45       hotbar       9..35       main inv       3..29 main inv
 *                              36..44      hotbar         30..38 hotbar
 *                              45          offhand
 * ```
 */
internal object MenuClicker {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.menu")!!

    /** Inventory portion (main + hotbar) of CraftingScreenHandler. */
    val TABLE_INV_RANGE: IntRange = 10..45

    /** Inventory portion of PlayerScreenHandler — same range as armor-skipping main+hotbar. */
    val PLAYER_INV_RANGE: IntRange = 9..44

    /** Inventory portion (main + hotbar) of AbstractFurnaceScreenHandler. */
    val FURNACE_INV_RANGE: IntRange = 3..38

    /** ms per MC game tick (50 ms = 20 TPS). */
    const val TICK_MS = 50L

    /**
     * Open the container at [pos] (e.g. a crafting_table or furnace),
     * run [body] against the resulting ScreenHandler, then close in a
     * `finally` so a leftover screen never wedges subsequent primitives.
     *
     * Returns the body's value, or throws if open/close failed.
     *
     * Implementation: ship `interactionManager.interactBlock(...)` aimed
     * at the block centre — same path PlaceRoute uses. Wait two ticks for
     * the server to send the open-screen packet back, then enter the body.
     */
    fun <R> withOpenedBlock(pos: BlockPos, body: (ScreenHandler) -> R): R {
        val opened = TickThread.submitAndWait(timeoutMs = 2_000) { openBlockOnTick(pos) }
        if (!opened) throw IllegalStateException("Failed to open container at $pos")

        // Two-tick settle: one for the C2S packet to ship, one for the
        // server's S2C OpenScreen response to land.
        Thread.sleep(TICK_MS * 2)

        // Confirm a non-PlayerScreenHandler is now active.
        val handlerClass = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait null
            player.currentScreenHandler.javaClass.simpleName
        }
        if (handlerClass == null || handlerClass == "PlayerScreenHandler") {
            forceClose()
            throw IllegalStateException(
                "Container at $pos did not open (currentScreenHandler=$handlerClass)"
            )
        }

        return try {
            // The handler reference is fetched fresh on the tick thread —
            // see `runOnHandler` callers below — so a brief ScreenHandler
            // identity flip during sync wouldn't cause a stale reference
            // to be used.
            val handlerHolder = TickThread.submitAndWait(timeoutMs = 1_000) {
                MinecraftClient.getInstance().player?.currentScreenHandler
                    ?: error("player gone")
            }
            body(handlerHolder)
        } finally {
            forceClose()
        }
    }

    /**
     * Run [body] against the player's PlayerScreenHandler. No UI is opened
     * — PlayerScreenHandler is always the player's `currentScreenHandler`
     * when no other screen is up, and `interactionManager.clickSlot`
     * works against it directly. So this is mostly a guard: refuse if
     * some other screen is up, refuse if the cursor is non-empty, then
     * run the body. The legacy code had to press the inventory key
     * because the Minescript `container_*` API only worked when a UI was
     * displaying; that constraint doesn't apply to native click-slot.
     */
    fun <R> withOpenedInventory(body: (ScreenHandler) -> R): R {
        val handler = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player
                ?: error("no player — not connected to a world")
            val h = player.currentScreenHandler
            if (h !is PlayerScreenHandler) {
                error("another screen is open (${h.javaClass.simpleName}) — close it first")
            }
            if (!h.cursorStack.isEmpty) {
                error("cursor is not empty — refusing to click")
            }
            h
        }
        return try {
            body(handler)
        } finally {
            // Cleanup wasn't strictly needed (no UI was opened) but a
            // defensive close protects against bodies that opened
            // something themselves and forgot to close.
            forceClose()
        }
    }

    /**
     * `interactionManager.interactBlock(...)` against [pos]. Synthetic
     * BlockHitResult aimed at the centre of the block (face = UP, which
     * MC accepts for top-of-block interaction). Returns true if the
     * call returned an "accepted" result, false otherwise — but the
     * authoritative check is "did currentScreenHandler change", done by
     * the caller after a settle.
     */
    private fun openBlockOnTick(pos: BlockPos): Boolean {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return false
        val mgr = mc.interactionManager ?: return false

        // Aim at the block first so the server-side ray-cast lands on it.
        WorldHelpers.lookAtBlock(player, pos)

        val hitPos = Vec3d(pos.x + 0.5, pos.y + 1.0, pos.z + 0.5)
        val hit = BlockHitResult(hitPos, Direction.UP, pos, /*insideBlock=*/ false)
        val result = mgr.interactBlock(player, Hand.MAIN_HAND, hit)
        player.swingHand(Hand.MAIN_HAND)
        return result.isAccepted
    }

    /**
     * Belt-and-braces close. Calls `closeHandledScreen` on the tick
     * thread, waits two ticks, asserts nothing's open, retries once if
     * not. Mirrors legacy `_close_open_screen`.
     */
    fun forceClose() {
        for (attempt in 1..2) {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
                player.closeHandledScreen()
                MinecraftClient.getInstance().setScreen(null)
                Unit
            }
            Thread.sleep(TICK_MS * 2)
            val stillOpen = TickThread.submitAndWait(timeoutMs = 1_000) {
                val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
                player.currentScreenHandler !is PlayerScreenHandler
            }
            if (!stillOpen) return
            log.warn("close: screen still open after attempt $attempt, retrying")
        }
        log.error("close: failed to close screen after retries")
    }

    /**
     * Convenience: clickSlot via interactionManager against the current
     * handler. Caller is responsible for being on the tick thread.
     */
    fun click(player: ClientPlayerEntity, handler: ScreenHandler, slot: Int, button: Int, action: SlotActionType) {
        val mc = MinecraftClient.getInstance()
        val mgr = mc.interactionManager ?: error("no interaction manager")
        mgr.clickSlot(handler.syncId, slot, button, action, player)
    }
}
