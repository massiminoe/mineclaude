package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory
import kotlin.math.floor

/**
 * `GET /standable_y?x=&z=&near_y=` — answer "what y can the player stand
 * at, at horizontal cell (x, z), closest to a reference y?"
 *
 * The y returned is the cell the player's feet would occupy:
 *   - replaceable at y      (feet)
 *   - replaceable at y + 1  (head clearance)
 *   - non-replaceable at y-1 (floor)
 *
 * Search expands outward from `near_y` (defaults to the player's current
 * feet y) up to ±MAX_RANGE blocks. Picking by proximity to near_y is what
 * makes the primitive useful underground/indoors: a query at (x, z) from
 * inside a cave returns the cave floor near you, not the world surface
 * 40 blocks above.
 *
 * "Replaceable" delegates to vanilla `BlockState.isReplaceable()` —
 * data-driven across MC versions, no enumeration.
 */
object StandableYRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.standable_y")!!

    private const val MAX_RANGE = 64

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/standable_y") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val x = params["x"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'x'", status = 400)
        val z = params["z"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'z'", status = 400)
        val nearYParam = params["near_y"]?.toIntOrNull()

        val result = TickThread.submitAndWait(timeoutMs = 1_000) {
            val mc = MinecraftClient.getInstance()
            val world = mc.world ?: return@submitAndWait Result.NoWorld
            val nearY = nearYParam ?: mc.player?.let { floor(it.pos.y).toInt() }
                ?: return@submitAndWait Result.NoPlayer
            findStandableY(x, z, nearY)?.let { (y, floorBlock) ->
                Result.Found(y, floorBlock, nearY)
            } ?: Result.NotFound(nearY)
        }
        return when (result) {
            is Result.NoWorld -> HttpBridge.err("no world")
            is Result.NoPlayer -> HttpBridge.err("no player and no near_y supplied")
            is Result.NotFound -> HttpBridge.err(
                "No standable y at ($x, $z) within ±$MAX_RANGE of y=${result.nearY}",
            )
            is Result.Found -> {
                log.info("standable_y: ({}, {}) near={} → y={} (floor={})",
                    x, z, result.nearY, result.y, result.floorBlock)
                HttpBridge.ok(
                    mapOf(
                        "y" to result.y,
                        "floor_block" to result.floorBlock,
                        "near_y" to result.nearY,
                    ),
                    "Standable at ($x, ${result.y}, $z) on ${result.floorBlock}",
                )
            }
        }
    }

    private sealed interface Result {
        data object NoWorld : Result
        data object NoPlayer : Result
        data class NotFound(val nearY: Int) : Result
        data class Found(val y: Int, val floorBlock: String, val nearY: Int) : Result
    }

    /** Returns (y, floor block id) of the standable cell nearest to nearY, or null. */
    private fun findStandableY(x: Int, z: Int, nearY: Int): Pair<Int, String>? {
        // Expand outward from nearY: 0, +1, -1, +2, -2, …
        for (dy in 0..MAX_RANGE) {
            checkAt(x, nearY + dy, z)?.let { return (nearY + dy) to it }
            if (dy != 0) {
                checkAt(x, nearY - dy, z)?.let { return (nearY - dy) to it }
            }
        }
        return null
    }

    /** If (x,y,z) is a standable cell, return the floor block id; else null. */
    private fun checkAt(x: Int, y: Int, z: Int): String? {
        val world = MinecraftClient.getInstance().world ?: return null
        val feet = BlockPos(x, y, z)
        val head = BlockPos(x, y + 1, z)
        val floor = BlockPos(x, y - 1, z)
        if (!world.getBlockState(feet).isReplaceable) return null
        if (!world.getBlockState(head).isReplaceable) return null
        val floorState = world.getBlockState(floor)
        if (floorState.isReplaceable) return null
        return WorldHelpers.blockIdAt(floor)
    }
}
