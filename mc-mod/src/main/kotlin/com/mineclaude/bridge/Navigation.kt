package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * Navigate the player toward a target via Baritone (`#goto X Y Z`).
 *
 * Baritone hooks into [`ClientPlayNetworkHandler.sendChatMessage`] before
 * the message ships as a player-chat packet, so we route through that path.
 * Polls reach every 500 ms with a 15 s deadline — past 15 s, Baritone is
 * almost always blocked on something it can't path through (leaves over
 * tree-tops, walled-off ore) and the agent is better off failing fast and
 * trying a different approach.
 *
 * # Held-slot leak
 *
 * Baritone's path/mine processes auto-equip throwaway blocks + tools as
 * they navigate (controlled by settings like `allowAutoTool`,
 * `acceptableThrowawayItems`, etc — and at least some of these are *not*
 * disabled by `#set allowAutoTool false` alone). After `#stop`, Baritone
 * does NOT restore the held slot we had before the `#goto`. That leaks
 * into the next /break or /equip verify: the agent calls /equip
 * wooden_pickaxe, gets success, then we fire /break which calls
 * navigateNear → Baritone swaps to a throwaway block → /break swings
 * bare-handed (or with the wrong tool) and the cobblestone doesn't drop.
 *
 * Fix: snapshot the held slot before `#goto`, restore it after the
 * arrival/timeout `#stop`. Agent-visible behaviour: the held slot the
 * caller set with /equip survives across navigateNear, regardless of
 * what Baritone did mid-path.
 */
internal object Navigation {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.nav")!!
    private const val POLL_MS = 500L
    private const val DEADLINE_MS = 15_000L

    /**
     * Walk to within [reach] of [target]. Returns true on arrival, false on
     * timeout. The HTTP worker thread holds the request open for up to 15 s
     * — long but bounded.
     */
    fun navigateNear(target: BlockPos, reach: Double = WorldHelpers.NAV_REACH): Boolean {
        // Already in range? Nothing to do — preserves the legacy fast path.
        val already = TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(p, target, reach)
        }
        if (already) return true

        // Snapshot the held slot before Baritone touches it.
        val savedSlot = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: 0
        }

        try {
            sendChat("#goto ${target.x} ${target.y} ${target.z}")
            val deadline = System.currentTimeMillis() + DEADLINE_MS
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(POLL_MS)
                val arrived = TickThread.submitAndWait(timeoutMs = 1_000) {
                    val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
                    WorldHelpers.isBlockWithinReach(p, target, reach)
                }
                if (arrived) {
                    sendChat("#stop")
                    Thread.sleep(200)
                    return true
                }
            }
            sendChat("#stop")
            return false
        } finally {
            // Restore the held slot we had before #goto, regardless of
            // what Baritone did. Done in a finally so a thrown timeout
            // still re-equips the caller's choice.
            restoreSelectedSlot(savedSlot)
        }
    }

    private fun sendChat(message: String) {
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            player.networkHandler.sendChatMessage(message)
            Unit
        }
    }

    /**
     * Re-select [slot] on the client and notify the server. Tick-paced so
     * the packet ships clean of any pending Baritone post-stop activity.
     */
    private fun restoreSelectedSlot(slot: Int) {
        Thread.sleep(100)  // let any final Baritone tick land
        val current = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: -1
        }
        if (current == slot) return
        log.info("nav: restoring held slot {} → {} after Baritone navigation", current, slot)
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            player.inventory.selectedSlot = slot
            player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
            Unit
        }
    }
}
