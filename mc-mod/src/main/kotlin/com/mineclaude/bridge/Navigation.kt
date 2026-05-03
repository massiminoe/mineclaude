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
 * Polls reach every 500 ms with a 15 s deadline as a hard backstop, plus a
 * 5 s motion-stall check (mirrors GotoRoute) so unreachable targets fail in
 * ~5 s instead of burning the full 15 s. Stall is the common case — Baritone
 * almost always settles within a few hundred ms of giving up — so the
 * deadline rarely fires unless the path is genuinely long+slow.
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
    // 10 polls × 500 ms = 5 s of no rounded-position change → stall.
    private const val STUCK_POLLS = 10

    sealed class Result {
        object Arrived : Result()
        /** Failure with a descriptive reason — caller composes the final error message. */
        data class Failed(val reason: String) : Result()
    }

    /**
     * Walk to within [reach] of [target]. Returns [Result.Arrived] on success
     * or [Result.Failed] with a descriptive reason on stall / deadline.
     * Bounded by ~5 s on stall, hard-capped at 15 s.
     */
    fun navigateNear(target: BlockPos, reach: Double = WorldHelpers.NAV_REACH): Result {
        // Already in range? Nothing to do — preserves the legacy fast path.
        val already = TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(p, target, reach)
        }
        if (already) return Result.Arrived

        // Snapshot the held slot before Baritone touches it.
        val savedSlot = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: 0
        }

        try {
            sendChat("#goto ${target.x} ${target.y} ${target.z}")
            val deadline = System.currentTimeMillis() + DEADLINE_MS
            var lastRounded: Triple<Double, Double, Double>? = null
            var staleCount = 0

            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(POLL_MS)
                val snap = TickThread.submitAndWait(timeoutMs = 1_000) {
                    val p = MinecraftClient.getInstance().player ?: return@submitAndWait null
                    Snap(
                        arrived = WorldHelpers.isBlockWithinReach(p, target, reach),
                        x = p.x, y = p.y, z = p.z,
                        dist = WorldHelpers.eyeToBlockDistance(p, target),
                        dy = WorldHelpers.eyeToBlockDy(p, target),
                    )
                } ?: continue

                if (snap.arrived) {
                    sendChat("#stop")
                    Thread.sleep(200)
                    return Result.Arrived
                }

                val rounded = Triple(round1(snap.x), round1(snap.y), round1(snap.z))
                if (rounded == lastRounded) {
                    staleCount += 1
                    if (staleCount >= STUCK_POLLS) {
                        sendChat("#stop")
                        log.warn("nav: stalled near ({},{},{}), dist={}", target.x, target.y, target.z, "%.1f".format(snap.dist))
                        return Result.Failed(stalledReason(snap))
                    }
                } else {
                    staleCount = 0
                    lastRounded = rounded
                }
            }
            sendChat("#stop")
            val finalSnap = TickThread.submitAndWait(timeoutMs = 1_000) {
                val p = MinecraftClient.getInstance().player ?: return@submitAndWait null
                Snap(false, p.x, p.y, p.z, WorldHelpers.eyeToBlockDistance(p, target), WorldHelpers.eyeToBlockDy(p, target))
            }
            log.warn("nav: deadline reached near ({},{},{})", target.x, target.y, target.z)
            return Result.Failed(deadlineReason(finalSnap))
        } finally {
            // Restore the held slot we had before #goto, regardless of
            // what Baritone did. Done in a finally so a thrown timeout
            // still re-equips the caller's choice.
            restoreSelectedSlot(savedSlot)
        }
    }

    private data class Snap(
        val arrived: Boolean,
        val x: Double, val y: Double, val z: Double,
        val dist: Double, val dy: Double,
    )

    private fun round1(v: Double) = Math.round(v * 10.0) / 10.0

    private fun stalledReason(snap: Snap): String {
        val secs = (POLL_MS * STUCK_POLLS / 1000).toInt()
        return "stalled %.1f blocks from target (%+.1fy), no movement for %ds — Baritone couldn't find a path; likely unreachable without pillaring or clearing surrounding blocks"
            .format(snap.dist, snap.dy, secs)
    }

    private fun deadlineReason(snap: Snap?): String {
        val secs = (DEADLINE_MS / 1000).toInt()
        return if (snap != null) {
            "timed out after %ds — still %.1f blocks from target (path may be very long or oscillating)"
                .format(secs, snap.dist)
        } else {
            "timed out after ${secs}s"
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
