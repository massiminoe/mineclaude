package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /interact {x, y, z}` — right-click the existing block at the given
 * coordinates (doors, trapdoors, fence gates, buttons, levers, beds,
 * jukeboxes, note blocks — "press use on this block").
 *
 * Thin backwards-compatible shim over [UseRoute.performUse]: it aims at the
 * block centre and dispatches on the real look-ray, using whatever the agent
 * is holding (no equip, no hold — the agent sets the hand beforehand). The
 * key improvement over the old synthetic-face implementation is that the
 * clicked face now comes from the actual raycast, so face-sensitive items
 * (torch on a wall, flint & steel) land where you're looking. Equivalent to
 * `/use {look_at_*: block centre}`.
 *
 * Preserves the legacy response shape `{interacted, target, result}` plus the
 * `opened_screen` partial when a stray click opens a chest/furnace screen
 * (which [UseRoute] closes for us).
 */
object InteractRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.interact")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/interact") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val x = (body["x"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'x' parameter", status = 400)
        val y = (body["y"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'y' parameter", status = 400)
        val z = (body["z"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'z' parameter", status = 400)
        val target = BlockPos(x, y, z)

        // Reject air up front — interactBlock on air would just fall through to
        // item-use, which is what /use_item is for.
        val targetBlock = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().world ?: return@submitAndWait null
            WorldHelpers.blockIdAt(target)
        } ?: return HttpBridge.err("no world")
        if (targetBlock == "air" || targetBlock == "void_air" || targetBlock == "cave_air") {
            return HttpBridge.err("Nothing to interact with at ($x, $y, $z) — block is $targetBlock")
        }

        val outcome = UseRoute.performUse(
            item = null,
            lookAt = WorldHelpers.blockCentre(target),
            holdMsOverride = 0L,
        )
        return when (outcome) {
            is UseRoute.Outcome.Err -> HttpBridge.err(outcome.message, outcome.status)
            is UseRoute.Outcome.Ok -> {
                if (!outcome.accepted) {
                    return HttpBridge.err(
                        "interact: block at ($x, $y, $z) didn't accept the click — " +
                            "$targetBlock isn't interactable, or the held item conflicts"
                    )
                }
                val data = mutableMapOf<String, Any>(
                    "interacted" to true,
                    "target" to targetBlock,
                    "result" to "accepted",
                )
                log.info("interact: clicked {} at {} (dispatch={})", targetBlock, target, outcome.dispatch)
                val opened = outcome.openedScreen
                if (opened != null) {
                    data["opened_screen"] = opened
                    HttpBridge.partial(
                        data,
                        "Interacted with $targetBlock at ($x, $y, $z) — opened a $opened screen and closed it; " +
                            "use a dedicated route (e.g. /chest/store, /furnace/inspect) if that was the goal",
                    )
                } else {
                    HttpBridge.ok(data, "Interacted with $targetBlock at ($x, $y, $z)")
                }
            }
        }
    }
}
