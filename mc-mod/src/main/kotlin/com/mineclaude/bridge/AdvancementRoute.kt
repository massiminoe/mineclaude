package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import org.slf4j.LoggerFactory

/**
 * Advancement (achievement) routes.
 *
 *   GET  /advancements        — earned + in-progress snapshot
 *   POST /advancements/reset  — revoke all advancements (eval reset)
 *
 * The live signal is the `advancement` event stream ([AdvancementTracker] →
 * events WS → get_state.events); these routes are the ground-truth / resume
 * read and the per-trial reset for the advancement-timing eval. Recipe +
 * technical advancements are excluded (the tracker only follows the real,
 * display-bearing tree), so the counts here are real achievements, not the
 * ~1300 `minecraft:recipes/...` unlocks the server also tracks.
 */
object AdvancementRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.advancements")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/advancements") { ex -> handle(ex) }
        bridge.addRoute("POST", "/advancements/reset") { ex -> reset(ex) }
    }

    private fun handle(@Suppress("UNUSED_PARAMETER") ex: HttpExchange): BridgeResponse {
        val snap = AdvancementTracker.snapshot()
        return HttpBridge.ok(snap, "advancements: ${snap["earned_count"]} earned")
    }

    /**
     * Reset the bot's advancements for a fresh eval trial (without a world
     * regen). Runs `/advancement revoke @s everything` — the bot is opped
     * (Claude, level 2), so the command path works. The server revokes ALL
     * advancements (including the recipe unlocks the tracker ignores) and
     * streams progress updates back; [AdvancementTracker] flips them not-done,
     * dropping them from `seen` so a later re-earn emits a fresh event.
     *
     * Truth-in-return: the revoke is async, so we poll the tracker's earned
     * count until it drains to zero (or a deadline), and report the real
     * before/after. `reset:false` means packets were still arriving at the
     * deadline — re-poll GET /advancements to confirm.
     */
    private fun reset(@Suppress("UNUSED_PARAMETER") ex: HttpExchange): BridgeResponse {
        val before = earnedCount()
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val player = MinecraftClient.getInstance().player
                ?: error("no player — not connected to a world")
            player.networkHandler.sendChatCommand("advancement revoke @s everything")
        }
        // Poll for the revoke packets to land. The whole earned set clears in a
        // burst, usually within a second; cap the wait so the worker thread
        // can't park indefinitely if the command no-ops (e.g. not opped).
        var after = before
        var waited = 0L
        while (waited < RESET_DEADLINE_MS) {
            Thread.sleep(RESET_POLL_MS)
            waited += RESET_POLL_MS
            after = earnedCount()
            if (after == 0) break
        }
        val cleared = before - after
        log.info("advancements reset: {} → {} ({} cleared)", before, after, cleared)
        val msg = if (after == 0) {
            "revoked $cleared advancement(s)"
        } else {
            "revoked ${cleared.coerceAtLeast(0)}; $after still reported (packets in flight — re-check /advancements)"
        }
        return HttpBridge.ok(
            mapOf("before" to before, "after" to after, "cleared" to cleared, "reset" to (after == 0)),
            msg,
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun earnedCount(): Int = AdvancementTracker.snapshot()["earned_count"] as? Int ?: 0

    private const val RESET_DEADLINE_MS = 3_000L
    private const val RESET_POLL_MS = 100L
}
