package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /blocks` — batch single-cell inspection.
 *
 * Body: `{"coords": [[x,y,z], ...]}`. Reads every cell inside ONE
 * `TickThread.submitAndWait` and returns `{blocks: [{x,y,z,block,replaceable},
 * ...]}` in input order. This is the batch sibling of `GET /block`: identical
 * per-cell shape, but it collapses N HTTP round-trips + N tick submissions
 * into one. Use it instead of looping `GET /block` over a coordinate list
 * (build-footprint preflight, re-checking a set of known ore coords, etc).
 *
 * Capped at [MAX_COORDS] per call to bound the response size and the
 * tick-thread budget — same spirit as the heightmap's 1024-cell cap.
 */
object BlocksRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.blocks")!!
    private const val MAX_COORDS = 4096

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/blocks") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val raw = ex.jsonBody()["coords"] as? List<*>
            ?: return HttpBridge.err(
                "Missing or invalid 'coords' (expected list of [x, y, z])",
                status = 400,
            )
        if (raw.size > MAX_COORDS) {
            return HttpBridge.err("Too many coords: ${raw.size} > $MAX_COORDS", status = 400)
        }

        val coords = ArrayList<BlockPos>(raw.size)
        for ((i, c) in raw.withIndex()) {
            val triple = c as? List<*>
                ?: return HttpBridge.err("coords[$i] is not a [x, y, z] list", status = 400)
            if (triple.size != 3) {
                return HttpBridge.err("coords[$i] must have exactly 3 elements", status = 400)
            }
            // gson decodes JSON numbers as Double; accept any Number and floor to int.
            val x = (triple[0] as? Number)?.toInt()
                ?: return HttpBridge.err("coords[$i] x is not a number", status = 400)
            val y = (triple[1] as? Number)?.toInt()
                ?: return HttpBridge.err("coords[$i] y is not a number", status = 400)
            val z = (triple[2] as? Number)?.toInt()
                ?: return HttpBridge.err("coords[$i] z is not a number", status = 400)
            coords.add(BlockPos(x, y, z))
        }

        val blocks = TickThread.submitAndWait(timeoutMs = 5_000) {
            val world = MinecraftClient.getInstance().world
                ?: return@submitAndWait null
            coords.map { pos ->
                // blockIdAt mirrors /block's id semantics exactly (minecraft: stripped).
                mapOf(
                    "x" to pos.x,
                    "y" to pos.y,
                    "z" to pos.z,
                    "block" to WorldHelpers.blockIdAt(pos),
                    "replaceable" to world.getBlockState(pos).isReplaceable,
                )
            }
        } ?: return HttpBridge.err("no world")

        log.debug("blocks: inspected {} cells", blocks.size)
        return HttpBridge.ok(mapOf("blocks" to blocks), "Inspected ${blocks.size} cells")
    }
}
