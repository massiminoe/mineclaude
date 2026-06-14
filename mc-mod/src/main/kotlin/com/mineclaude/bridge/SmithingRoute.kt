package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import org.slf4j.LoggerFactory

/**
 * `POST /smithing/upgrade {template, base, addition, [x,y,z]}`
 *
 * Drives a smithing table's three input slots (1.20+ layout):
 *   slot 0 — smithing template
 *   slot 1 — base equipment (the gear)
 *   slot 2 — addition (the material)
 *   slot 3 — output
 *
 * Covers both smithing uses in one path:
 *   - netherite upgrade: netherite_upgrade_smithing_template + diamond gear +
 *     netherite_ingot -> netherite gear
 *   - armor trim: a trim template + armor piece + trim material (cosmetic)
 *
 * The SmithingScreenHandler computes the output server-side once all three
 * inputs are valid; we snapshot it and shift-click it out. No XP cost. One
 * unit of each input is placed. The netherite_upgrade_smithing_template is
 * loot-only (bastions) — it must already be in the bot's inventory; only the
 * netherite_ingot is craftable.
 */
object SmithingRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.smithing")!!

    private const val TEMPLATE = 0
    private const val BASE = 1
    private const val ADDITION = 2
    private const val OUTPUT = 3

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/smithing/upgrade") { ex -> handleUpgrade(ex) }
    }

    private fun handleUpgrade(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val template = (body["template"] as? String).orEmpty().removePrefix("minecraft:")
        val base = (body["base"] as? String).orEmpty().removePrefix("minecraft:")
        val addition = (body["addition"] as? String).orEmpty().removePrefix("minecraft:")
        if (template.isEmpty() || base.isEmpty() || addition.isEmpty()) {
            return HttpBridge.err("Missing 'template', 'base', or 'addition'", status = 400)
        }

        val pos = StationMenu.parsePos(
            (body["x"] as? Number)?.toInt(),
            (body["y"] as? Number)?.toInt(),
            (body["z"] as? Number)?.toInt(),
        ) ?: TickThread.submitAndWait(timeoutMs = 1_000) {
            StationMenu.findNearestBlock(setOf("smithing_table"), radius = 16)
        } ?: return HttpBridge.err("No smithing table nearby. Place one first.")

        StationMenu.ensureInReach(pos, "smithing table")?.let { return it }

        var err: String? = null
        var result = StationMenu.emptySlot()
        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (pt, terr) = StationMenu.insertExact(handler, template, 1, TEMPLATE)
                if (pt < 1) { err = terr ?: "couldn't place $template"; return@withOpenedBlock }
                val (pb, berr) = StationMenu.insertExact(handler, base, 1, BASE)
                if (pb < 1) { err = berr ?: "couldn't place $base"; return@withOpenedBlock }
                val (pa, aerr) = StationMenu.insertExact(handler, addition, 1, ADDITION)
                if (pa < 1) { err = aerr ?: "couldn't place $addition"; return@withOpenedBlock }

                // Let SmithingScreenHandler.updateResult run server-side.
                Thread.sleep(MenuClicker.TICK_MS * 2)
                result = StationMenu.snapshot(handler, OUTPUT)
                if (result["item"] == null) {
                    err = "Smithing produced no result — $template + $base + $addition is not a " +
                        "valid recipe (wrong template, gear, or material)."
                    return@withOpenedBlock
                }
                StationMenu.shiftMoveIfNonEmpty(handler, OUTPUT)
                Thread.sleep(MenuClicker.TICK_MS * 2)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }
        err?.let { return HttpBridge.err(it) }

        log.info("smithing/upgrade: {} + {} + {} -> {} at {}", template, base, addition, result["item"], pos)
        return HttpBridge.ok(
            mapOf(
                "upgraded" to true,
                "result" to result,
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "method" to "real",
            ),
            "Smithed $base + $addition -> ${result["count"]} ${result["item"]}",
        )
    }
}
