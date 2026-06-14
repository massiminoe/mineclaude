package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.Hand
import org.slf4j.LoggerFactory

/**
 * `POST /block {duration_s?, item?, look_at_x/y/z?}` — raise a shield and
 * actively block for a window, then lower it.
 *
 * A shield in the offhand does nothing on its own — vanilla only mitigates
 * damage while the player is *actively blocking* (holding the use key). This
 * route owns that lifecycle, the same way [SurfaceRoute] owns "hold jump":
 *
 *   1. Ensure a shield is in the offhand ([EquipRoute.ensureOffhand]; auto-
 *      equips from inventory if it isn't already there).
 *   2. (optional) Aim the eye at `look_at` first — blocking only protects the
 *      direction you face, so a caller tanking a specific threat should point
 *      at it.
 *   3. Start the use on the *offhand* explicitly (`interactItem(OFF_HAND)`)
 *      and hold `useKey` down. Using the offhand directly — rather than
 *      letting the tick loop's use-key handler run `doItemUse` — avoids the
 *      crosshair raycast accidentally placing a block / opening a door with
 *      the main hand.
 *   4. Hold for `duration_s`, polling `player.isBlocking()` to confirm the
 *      block pose actually engaged (it takes ~5 ticks).
 *   5. Always release the key and `stopUsingItem` in `finally`, so a thrown
 *      timeout can't leave the shield raised forever.
 *
 * Time-boxed (not a raise/lower pair) because the single-flight slot can't
 * hold a sustained state across two calls — same shape as `/surface` and
 * `/sleep`. Truth-in-return `{blocking, held_ms, item}`: `blocking` is whether
 * the block pose was ever confirmed during the hold (headless input can be
 * finicky), so a caller can tell a real block from a no-op.
 *
 * Note: a single block and a single swing are mutually exclusive (you lower
 * to hit). This `/block` is the *standalone* defensive window the agent
 * composes around a fight. The `/attack` loop also raises an offhand shield on
 * its own in the gaps between swings (see [AttackRoute]), so for an in-melee
 * guard you usually don't need a separate `/block` — reach for this when you
 * want to tank *without* swinging (a creeper's approach, a skeleton volley) or
 * hold a guard outside an attack entirely.
 */
object ShieldRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.block")!!

    private const val DEFAULT_DURATION_S = 2.0

    /** Hard cap — blocking holds the single-flight slot; don't let it hang. */
    private const val MAX_DURATION_S = 30.0

    private const val POLL_MS = 50L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/block") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String)?.takeIf { it.isNotEmpty() } ?: "shield"
        val durationS = ((body["duration_s"] as? Number)?.toDouble() ?: DEFAULT_DURATION_S)
            .coerceIn(0.0, MAX_DURATION_S)

        val lx = (body["look_at_x"] as? Number)?.toDouble()
        val ly = (body["look_at_y"] as? Number)?.toDouble()
        val lz = (body["look_at_z"] as? Number)?.toDouble()
        val anyLook = body.containsKey("look_at_x") || body.containsKey("look_at_y") ||
            body.containsKey("look_at_z")
        if (anyLook && (lx == null || ly == null || lz == null)) {
            return HttpBridge.err(
                "look_at requires all three of look_at_x, look_at_y, look_at_z",
                status = 400,
            )
        }

        // Get the shield into the offhand (idempotent if already there).
        EquipRoute.ensureOffhand(item)?.let { err ->
            return HttpBridge.err("can't block — $err")
        }

        return runBlock(item, durationS, if (lx != null && ly != null && lz != null) Triple(lx, ly, lz) else null)
    }

    private fun runBlock(item: String, durationS: Double, lookAt: Triple<Double, Double, Double>?): BridgeResponse {
        val holdMs = (durationS * 1000.0).toLong()

        // Raise: aim (optional), start the offhand use, hold the use key.
        val started = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait false
            val mgr = mc.interactionManager ?: return@submitAndWait false
            WorldHelpers.ensureNoScreenOpen(player)
            if (lookAt != null) WorldHelpers.lookAtPosition(player, lookAt.first, lookAt.second, lookAt.third)
            // Use the offhand shield directly, then pin the key so the client
            // tick loop won't stopUsingItem on us (it releases the block the
            // moment useKey reads not-pressed).
            mgr.interactItem(player, Hand.OFF_HAND)
            mc.options.useKey.setPressed(true)
            true
        }
        if (!started) return HttpBridge.err("no player — not connected to a world")

        var everBlocked = false
        val deadline = System.currentTimeMillis() + holdMs
        try {
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(POLL_MS)
                val blocking = TickThread.submitAndWait(timeoutMs = 1_000) {
                    MinecraftClient.getInstance().player?.isBlocking ?: false
                }
                if (blocking) everBlocked = true
            }
        } catch (_: InterruptedException) {
            // Preempted (e.g. interrupt()) — lower the shield and report what
            // we got; the finally handles the actual key/use release.
        } finally {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                val mc = MinecraftClient.getInstance()
                mc.options.useKey.setPressed(false)
                mc.player?.let { mc.interactionManager?.stopUsingItem(it) }
                Unit
            }
        }

        val data = mapOf(
            "blocking" to everBlocked,
            "held_ms" to holdMs,
            "item" to item,
            "method" to "real",
        )
        return if (everBlocked) {
            HttpBridge.ok(data, "Blocked with $item for ${holdMs}ms")
        } else {
            // Not a hard error (the shield was equipped + held), just a no-op:
            // the block pose never engaged — an off-hand that isn't actually a
            // shield, or input didn't register. Mirrors /use's "used:false"
            // convention so only a missing shield raises; the caller checks
            // `blocking` and can re-aim / retry.
            HttpBridge.ok(data, "Held $item for ${holdMs}ms but the block pose never engaged")
        }
    }
}
