package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import org.slf4j.LoggerFactory
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * `POST /goto {x, y, z, timeout?}` — drive Baritone to a coordinate and
 * block until arrival, stuck, or timeout.
 *
 * Mirrors `bridge.minescript_api.goto_and_wait` bit-for-bit so the agent
 * sees an identical response shape between bridges:
 *   - poll player position every 500 ms,
 *   - arrive at sqrt-distance ≤ 2.0,
 *   - stuck after 10 consecutive polls of unchanged 0.1-rounded position
 *     (≈ 5 s of no motion),
 *   - hard cap on `timeout` (default 60 s),
 *   - emit `#stop` on every exit branch (arrived, stuck, timeout) so the
 *     next request doesn't inherit a hot Baritone process.
 *
 * `Navigation.navigateNear` solves a related problem (block-reach polling
 * with a 15 s deadline used by /break /place /attack); /goto's contract is
 * looser (target is a free coordinate, not a block) and longer-running, so
 * it doesn't share the same deadline. Held-slot snapshot/restore *is*
 * shared — the same Baritone tool-swap leak applies — but we wire it
 * inline here to keep the response shape independent of Navigation's API.
 */
object GotoRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.goto")!!

    private const val POLL_MS = 500L
    private const val ARRIVE_THRESHOLD = 2.0
    private const val STUCK_POLLS = 10
    private const val DEFAULT_TIMEOUT_S = 60.0

    // How far the player must actually travel for the arrival to count as a
    // real walk vs. a no-op. A goto whose target is already within
    // ARRIVE_THRESHOLD returns on the first poll without moving — the response
    // flags that (`moved=false`) so an "Arrived" can't masquerade as motion.
    private const val MOVE_EPSILON = 1.0

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/goto") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val x = (body["x"] as? Number)?.toDouble() ?: 0.0
        val z = (body["z"] as? Number)?.toDouble() ?: 0.0
        val yParam = (body["y"] as? Number)?.toDouble()
        val timeoutS = (body["timeout"] as? Number)?.toDouble() ?: DEFAULT_TIMEOUT_S

        // Auto-resolve y from the heightmap when caller omitted it. Lets the
        // agent say "walk to (x, z)" without first probing the surface — the
        // case that motivated this whole endpoint surface.
        val y = yParam ?: when (val resolved = resolveStandableY(x.toInt(), z.toInt())) {
            is YResolve.Ok -> resolved.y.toDouble()
            is YResolve.Err -> return HttpBridge.err(resolved.message)
        }

        return runGoto(x, y, z, timeoutS)
    }

    private sealed interface YResolve {
        data class Ok(val y: Int) : YResolve
        data class Err(val message: String) : YResolve
    }

    private fun resolveStandableY(x: Int, z: Int): YResolve {
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            val mc = MinecraftClient.getInstance()
            mc.world ?: return@submitAndWait YResolve.Err("no world")
            val player = mc.player ?: return@submitAndWait YResolve.Err("no player")
            val nearY = kotlin.math.floor(player.pos.y).toInt()
            val cell = Heightmap.findStandable(x, z, nearY)
                ?: return@submitAndWait YResolve.Err(
                    "No standable y at ($x, $z) within ±${Heightmap.MAX_RANGE} of y=$nearY",
                )
            YResolve.Ok(cell.y)
        }
    }

    private fun runGoto(x: Double, y: Double, z: Double, timeoutS: Double): BridgeResponse {
        val savedSlot = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: 0
        }
        // Snapshot the entry position so the arrival response can report
        // whether the bot actually relocated (vs. a target already in reach).
        val startPos = playerPosition()

        val cmd = "#goto ${x.toInt()} ${y.toInt()} ${z.toInt()}"
        sendBaritoneCommand(cmd)
        log.info("goto: sent '{}', waiting for arrival (timeout={}s)", cmd, timeoutS)

        val deadlineMs = System.currentTimeMillis() + (timeoutS * 1000.0).toLong()
        var lastRoundedPos: Triple<Double, Double, Double>? = null
        var staleCount = 0

        try {
            while (System.currentTimeMillis() < deadlineMs) {
                Thread.sleep(POLL_MS)
                val pos = playerPosition() ?: continue
                val (px, py, pz) = pos
                val dist = sqrt((px - x) * (px - x) + (py - y) * (py - y) + (pz - z) * (pz - z))

                if (dist <= ARRIVE_THRESHOLD) {
                    log.info("goto: arrived at ({},{},{}), dist={}", px.toInt(), py.toInt(), pz.toInt(), round1(dist))
                    return arrivedResponse(startPos, px, py, pz, dist, x, y, z)
                }

                val rounded = Triple(round1(px), round1(py), round1(pz))
                if (rounded == lastRoundedPos) {
                    staleCount += 1
                    if (staleCount >= STUCK_POLLS) {
                        log.warn("goto: stuck at ({},{},{}), dist={}", px.toInt(), py.toInt(), pz.toInt(), round1(dist))
                        return HttpBridge.err("Stuck at distance ${round1(dist)} from target")
                    }
                } else {
                    staleCount = 0
                    lastRoundedPos = rounded
                }
            }

            log.warn("goto: timed out after {}s", timeoutS)
            val pos = playerPosition()
            return if (pos != null) {
                val (px, py, pz) = pos
                val dist = sqrt((px - x) * (px - x) + (py - y) * (py - y) + (pz - z) * (pz - z))
                HttpBridge.err("Timed out after ${timeoutS.toInt()}s, distance=${round1(dist)}")
            } else {
                HttpBridge.err("Timed out after ${timeoutS.toInt()}s")
            }
        } finally {
            // Always #stop so the next request doesn't inherit a running
            // Baritone process. Mirrors goto_and_wait's exit pattern.
            try { sendBaritoneCommand("#stop") } catch (t: Throwable) {
                log.warn("goto: failed to send #stop on exit: {}", t.message)
            }
            // Restore the held slot Baritone may have swapped to a throwaway
            // block / auto-tool while pathing. Same leak as Navigation.kt.
            restoreSelectedSlot(savedSlot)
        }
    }

    /**
     * Build the arrival response from the ACHIEVED position, not the requested
     * target. The old message echoed the target coords verbatim, so a no-op
     * (target already within ARRIVE_THRESHOLD) read as a successful walk. Now
     * the message states where the bot actually ended up, its residual distance
     * to the target, and `moved` — true only if it travelled more than
     * MOVE_EPSILON from where it started.
     */
    private fun arrivedResponse(
        start: Triple<Double, Double, Double>?,
        px: Double, py: Double, pz: Double, dist: Double,
        tx: Double, ty: Double, tz: Double,
    ): BridgeResponse {
        val traveled = if (start != null) {
            sqrt(
                (px - start.first) * (px - start.first) +
                    (py - start.second) * (py - start.second) +
                    (pz - start.third) * (pz - start.third)
            )
        } else {
            Double.NaN
        }
        // Unknown start (null) → assume moved rather than claim a no-op.
        val moved = traveled.isNaN() || traveled > MOVE_EPSILON
        val here = "(${round1(px)}, ${round1(py)}, ${round1(pz)})"
        val target = "(${tx.toInt()}, ${ty.toInt()}, ${tz.toInt()})"
        val message = if (moved) {
            "Walked to $here — ${round1(dist)} from target $target"
        } else {
            "Did not move — already at $here, ${round1(dist)} from target $target (within arrival range)"
        }
        val data = mutableMapOf<String, Any>(
            "arrived" to true,
            "moved" to moved,
            "position" to mapOf("x" to px, "y" to py, "z" to pz),
            "target" to mapOf("x" to tx, "y" to ty, "z" to tz),
            "distance" to round1(dist),
        )
        if (start != null) {
            data["start"] = mapOf("x" to start.first, "y" to start.second, "z" to start.third)
            data["traveled"] = round1(traveled)
        }
        return HttpBridge.ok(data, message)
    }

    private fun playerPosition(): Triple<Double, Double, Double>? {
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait null
            Triple(p.x, p.y, p.z)
        }
    }

    private fun restoreSelectedSlot(slot: Int) {
        try {
            Thread.sleep(100)
        } catch (_: InterruptedException) { /* fall through */ }
        val current = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: -1
        }
        if (current == slot) return
        log.info("goto: restoring held slot {} → {} after Baritone navigation", current, slot)
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            player.inventory.selectedSlot = slot
            player.networkHandler.sendPacket(
                net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket(slot)
            )
            Unit
        }
    }

    private fun round1(d: Double): Double = (d * 10.0).roundToInt() / 10.0
}
