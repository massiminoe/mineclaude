package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `GET /block?x=&y=&z=` — single-cell inspection.
 *
 * Returns `{block: <id>, replaceable: <bool>}`. The `replaceable` flag is
 * vanilla `BlockState.isReplaceable()` — same predicate `placeBlock`'s
 * preflight uses, so callers can decide "would placing here succeed?"
 * without firing a placement and parsing the error.
 *
 * Cheap, tick-thread wrapped (one `world.getBlockState` call). Intended
 * use is preflight before a build operation: scan the cells you plan to
 * fill, verify they're replaceable.
 */
object BlockRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.block")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/block") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val x = params["x"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'x'", status = 400)
        val y = params["y"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'y'", status = 400)
        val z = params["z"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'z'", status = 400)

        val target = BlockPos(x, y, z)
        val result = TickThread.submitAndWait(timeoutMs = 1_000) {
            val world = MinecraftClient.getInstance().world
                ?: return@submitAndWait null
            val state = world.getBlockState(target)
            val id = WorldHelpers.blockIdAt(target)
            id to state.isReplaceable
        } ?: return HttpBridge.err("no world")

        val (block, replaceable) = result
        log.debug("block: ({}, {}, {}) → {} (replaceable={})", x, y, z, block, replaceable)
        return HttpBridge.ok(
            mapOf("block" to block, "replaceable" to replaceable),
            "$block at ($x, $y, $z)",
        )
    }
}
