package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import org.slf4j.LoggerFactory

/**
 * `POST /surface {timeout?}` — hold jump until the player is no longer
 * fully submerged in water, or [timeout] seconds elapse.
 *
 * Workaround for a known Baritone limitation: starting `#goto` from a
 * fully-submerged position, Baritone explores a single path node and
 * gives up (the start state has no valid movement neighbours), so the
 * player floats in place until the goto stall fires. The drowning reflex
 * uses this route to surface the player first; the subsequent
 * shore-finder goto then sees an in-air start state.
 *
 * Why only jump (not forward+sprint)? In 1.13+, forward+sprint while
 * submerged triggers the swimming pose — the player goes prone and
 * horizontal, which prevents vertical ascent. Jump alone makes the
 * player rise straight up, which is exactly what we want for a drowning
 * recovery: get the head above water as fast as possible, then let
 * Baritone handle the walk to shore.
 *
 * Pattern mirrors GotoRoute: outer poll loop on the HttpServer worker
 * thread, per-tick `submitAndWait` to read the submerged flag. Keys are
 * set on the tick thread to avoid touching MC state off-tick. Always
 * releases keys in `finally` so a thrown timeout doesn't leave jump
 * held down forever.
 */
object SurfaceRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.surface")!!

    private const val POLL_MS = 50L
    private const val DEFAULT_TIMEOUT_S = 2.0

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/surface") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val timeoutS = (body["timeout"] as? Number)?.toDouble() ?: DEFAULT_TIMEOUT_S
        return runSurface(timeoutS)
    }

    private fun runSurface(timeoutS: Double): BridgeResponse {
        val deadlineMs = System.currentTimeMillis() + (timeoutS * 1000.0).toLong()
        log.info("surface: holding jump, timeout={}s", timeoutS)
        setKeys(true)
        var ticks = 0
        try {
            while (System.currentTimeMillis() < deadlineMs) {
                Thread.sleep(POLL_MS)
                ticks++
                val submerged = TickThread.submitAndWait(timeoutMs = 1_000) {
                    MinecraftClient.getInstance().player?.isSubmergedInWater
                }
                if (submerged == null) {
                    log.warn("surface: no player, aborting after {} ticks", ticks)
                    return HttpBridge.err("no player")
                }
                if (!submerged) {
                    log.info("surface: surfaced after {} ticks", ticks)
                    return HttpBridge.ok(
                        mapOf("surfaced" to true, "ticks" to ticks),
                        "Surfaced after $ticks ticks",
                    )
                }
            }
            log.warn("surface: still submerged after {}s ({} ticks)", timeoutS, ticks)
            return HttpBridge.ok(
                mapOf("surfaced" to false, "ticks" to ticks),
                "Did not surface within ${timeoutS}s",
            )
        } finally {
            setKeys(false)
        }
    }

    private fun setKeys(pressed: Boolean) {
        try {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                MinecraftClient.getInstance().options.jumpKey.setPressed(pressed)
            }
        } catch (t: Throwable) {
            log.warn("surface: failed to {} jump key: {}", if (pressed) "press" else "release", t.message)
        }
    }
}
