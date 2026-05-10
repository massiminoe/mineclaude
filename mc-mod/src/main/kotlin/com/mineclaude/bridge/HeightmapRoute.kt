package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import org.slf4j.LoggerFactory
import kotlin.math.floor

/**
 * `GET /heightmap?x0=&z0=&w=&h=&near_y=` — return a w×h grid of standable
 * y values for the rectangle `[x0, x0+w) × [z0, z0+h)`.
 *
 * Response shape:
 * ```
 * {
 *   "x0": 340, "z0": 42, "w": 9, "h": 26, "near_y": 95,
 *   "ys":    [[94, 95, ...],  …],   // h rows × w cols, int or null
 *   "floor": [["dirt", "stone", ...], …]  // matching floor block ids, or null
 * }
 * ```
 *
 * Single tick-thread submission scans every cell and returns. Capped at
 * MAX_AREA cells to bound worst-case tick budget — past that we make the
 * caller paginate, since freezing the client tick is bad for any concurrent
 * Baritone work (the `/standable_y` per-cell loop ate a 4-minute window in
 * a 20×20×7×7 nested scan that the agent shipped before this endpoint
 * existed; one-shot bulk is the fix).
 */
object HeightmapRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.heightmap")!!

    /** 32 × 32 cells. ~1 ms/cell ≈ 1 s — within tick-task budget. */
    private const val MAX_AREA = 32 * 32

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/heightmap") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val x0 = params["x0"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'x0'", status = 400)
        val z0 = params["z0"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'z0'", status = 400)
        val w = params["w"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'w'", status = 400)
        val h = params["h"]?.toIntOrNull()
            ?: return HttpBridge.err("Missing or invalid 'h'", status = 400)
        val nearYParam = params["near_y"]?.toIntOrNull()

        if (w <= 0 || h <= 0) {
            return HttpBridge.err("'w' and 'h' must be positive (got w=$w, h=$h)", status = 400)
        }
        if (w * h > MAX_AREA) {
            return HttpBridge.err(
                "Region too large: w*h=${w * h} > $MAX_AREA. Tile into smaller calls.",
                status = 400,
            )
        }

        val result = TickThread.submitAndWait(timeoutMs = 5_000) {
            val mc = MinecraftClient.getInstance()
            mc.world ?: return@submitAndWait Result.NoWorld
            val nearY = nearYParam ?: mc.player?.let { floor(it.pos.y).toInt() }
                ?: return@submitAndWait Result.NoPlayer

            val ys = Array(h) { arrayOfNulls<Int>(w) }
            val floor = Array(h) { arrayOfNulls<String>(w) }
            var found = 0
            for (dz in 0 until h) {
                for (dx in 0 until w) {
                    val cell = Heightmap.findStandable(x0 + dx, z0 + dz, nearY)
                    if (cell != null) {
                        ys[dz][dx] = cell.y
                        floor[dz][dx] = cell.floorBlock
                        found += 1
                    }
                }
            }
            Result.Found(ys, floor, nearY, found)
        }

        return when (result) {
            is Result.NoWorld -> HttpBridge.err("no world")
            is Result.NoPlayer -> HttpBridge.err("no player and no near_y supplied")
            is Result.Found -> {
                log.info(
                    "heightmap: ({},{}) {}x{} near={} → {}/{} cells standable",
                    x0, z0, w, h, result.nearY, result.found, w * h,
                )
                // Convert Array<Array<…?>> → List<List<…?>> for JSON.
                val ysList = result.ys.map { it.toList() }
                val floorList = result.floor.map { it.toList() }
                HttpBridge.ok(
                    mapOf(
                        "x0" to x0, "z0" to z0, "w" to w, "h" to h,
                        "near_y" to result.nearY,
                        "ys" to ysList,
                        "floor" to floorList,
                    ),
                    "Scanned ${w * h} cells, ${result.found} standable",
                )
            }
        }
    }

    private sealed interface Result {
        data object NoWorld : Result
        data object NoPlayer : Result
        data class Found(
            val ys: Array<Array<Int?>>,
            val floor: Array<Array<String?>>,
            val nearY: Int,
            val found: Int,
        ) : Result
    }
}
