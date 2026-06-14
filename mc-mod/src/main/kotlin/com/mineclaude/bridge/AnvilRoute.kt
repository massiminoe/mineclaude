package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import org.slf4j.LoggerFactory

/**
 * `POST /anvil/combine {left, right, [x,y,z]}`
 *
 * Generic anvil combine: places `left` in the first input slot and `right`
 * in the second, lets the AnvilScreenHandler compute the result server-side,
 * then shift-clicks the output out. One path covers the three slot-mechanic
 * anvil uses:
 *   - repair with material   (e.g. iron_pickaxe + iron_ingot)
 *   - repair by combining     (two damaged tools of the same kind)
 *   - apply an enchanted_book (gear + enchanted_book)
 *
 * Renaming is NOT here — it rides a separate RenameItemC2SPacket, not slot
 * mechanics. One unit of each input is placed; for a multi-unit material
 * repair, call again. Taking the result costs XP levels server-side; if the
 * bot can't afford it the take is rejected, nothing is consumed, and the
 * route says so. Open/insert/extract mechanics live in StationMenu.
 */
object AnvilRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.anvil")!!

    private const val LEFT = 0
    private const val RIGHT = 1
    private const val OUTPUT = 2
    private val ANVIL_IDS = setOf("anvil", "chipped_anvil", "damaged_anvil")

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/anvil/combine") { ex -> handleCombine(ex) }
    }

    private fun handleCombine(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val left = (body["left"] as? String).orEmpty().removePrefix("minecraft:")
        val right = (body["right"] as? String).orEmpty().removePrefix("minecraft:")
        if (left.isEmpty() || right.isEmpty()) {
            return HttpBridge.err("Missing 'left' or 'right' item", status = 400)
        }

        val pos = StationMenu.parsePos(
            (body["x"] as? Number)?.toInt(),
            (body["y"] as? Number)?.toInt(),
            (body["z"] as? Number)?.toInt(),
        ) ?: TickThread.submitAndWait(timeoutMs = 1_000) {
            StationMenu.findNearestBlock(ANVIL_IDS, radius = 16)
        } ?: return HttpBridge.err("No anvil nearby. Place one first.")

        StationMenu.ensureInReach(pos, "anvil")?.let { return it }

        var err: String? = null
        var result = StationMenu.emptySlot()
        var cost = 0
        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (pl, lerr) = StationMenu.insertExact(handler, left, 1, LEFT)
                if (pl < 1) { err = lerr ?: "couldn't place $left"; return@withOpenedBlock }
                val (pr, rerr) = StationMenu.insertExact(handler, right, 1, RIGHT)
                if (pr < 1) { err = rerr ?: "couldn't place $right"; return@withOpenedBlock }

                // Let AnvilScreenHandler.updateResult run server-side.
                Thread.sleep(MenuClicker.TICK_MS * 2)
                result = StationMenu.snapshot(handler, OUTPUT)
                if (result["item"] == null) {
                    err = "Anvil produced no result — $left + $right can't be combined " +
                        "(incompatible items, or the combine is too expensive)."
                    return@withOpenedBlock
                }

                val xpBefore = StationMenu.playerLevel()
                StationMenu.shiftMoveIfNonEmpty(handler, OUTPUT)
                Thread.sleep(MenuClicker.TICK_MS * 2)
                cost = (xpBefore - StationMenu.playerLevel()).coerceAtLeast(0)

                // Output still populated → the take was rejected (not enough XP).
                if (StationMenu.snapshot(handler, OUTPUT)["item"] != null) {
                    err = "Result couldn't be taken — not enough experience (have level " +
                        "$xpBefore). Nothing was consumed."
                }
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }
        err?.let { return HttpBridge.err(it) }

        log.info("anvil/combine: {} + {} -> {} (xp -{}) at {}", left, right, result["item"], cost, pos)
        return HttpBridge.ok(
            mapOf(
                "combined" to true,
                "result" to result,
                "xp_levels_spent" to cost,
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "method" to "real",
            ),
            "Combined $left + $right -> ${result["count"]} ${result["item"]} (xp -$cost)",
        )
    }
}
