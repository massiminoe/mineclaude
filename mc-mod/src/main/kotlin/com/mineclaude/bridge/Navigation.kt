package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos

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
 * Mirrors `bridge.player_control.navigate_near` so the agent's reach +
 * timeout expectations are unchanged after Phase 3.
 */
internal object Navigation {
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
    }

    private fun sendChat(message: String) {
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            player.networkHandler.sendChatMessage(message)
            Unit
        }
    }
}
