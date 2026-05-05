package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.registry.Registries
import net.minecraft.util.math.BlockPos
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * `/nearby/blocks` and `/nearby/entities` — port of the Minescript-backed
 * neighborhood scans.
 *
 * Reads run on the tick thread because `world.getBlockState` and
 * `world.getEntities` are not safe off-thread. Both endpoints support
 * tight microcaching (50 ms) keyed by query params so identical reads
 * within a tick share one tick-thread submission.
 *
 * Shape contract: `docs/superpowers/specs/2026-04-27-native-mod-phase1-response-shapes.md`.
 */
object NearbyRoutes {
    /** Hard cap matching the legacy bridge's `radius = min(radius, 32)`. */
    private const val MAX_RADIUS = 32

    /**
     * Cache key for blocks reads. Because handlers commonly differ only in
     * `radius` and `types`, we key on those plus the player's chunk coord
     * to invalidate on player movement that could shift the result.
     */
    private data class BlocksCacheKey(
        val radius: Int,
        val types: Set<String>,
        val cx: Int,
        val cy: Int,
        val cz: Int,
    )

    private data class EntitiesCacheKey(val radius: Int, val cx: Int, val cy: Int, val cz: Int)

    // Single-slot caches keyed by the request shape: when the key changes
    // (radius/types/player chunk), the slot is replaced rather than grown.
    // Keeps memory bounded and matches the legacy single-snapshot model.
    @Volatile private var blocksCacheKey: BlocksCacheKey? = null
    private val blocksCache = TickCache(ttlMs = 50) { readBlocksOnTick(blocksCacheKey!!) }

    @Volatile private var entitiesCacheKey: EntitiesCacheKey? = null
    private val entitiesCache = TickCache(ttlMs = 50) { readEntitiesOnTick(entitiesCacheKey!!) }

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/nearby/blocks") { ex -> handleBlocks(ex) }
        bridge.addRoute("GET", "/nearby/entities") { ex -> handleEntities(ex) }
    }

    private fun handleBlocks(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val radius = (params["r"]?.toIntOrNull() ?: 8).coerceAtMost(MAX_RADIUS)
        val typesRaw = params["types"].orEmpty()
        val types = typesRaw.split(',').map { it.trim() }.filter { it.isNotEmpty() }.toSet()

        val key = blocksKeyFor(radius, types) ?: return HttpBridge.ok(
            mapOf("blocks" to emptyList<Map<String, Any>>()),
            "Found 0 blocks (no player)",
        )
        if (blocksCacheKey != key) {
            blocksCacheKey = key
            blocksCache.invalidate()
        }
        val blocks = blocksCache.get()
        return HttpBridge.ok(mapOf("blocks" to blocks), "Found ${blocks.size} blocks")
    }

    private fun handleEntities(ex: HttpExchange): BridgeResponse {
        val params = ex.queryParams()
        val radius = (params["r"]?.toIntOrNull() ?: 32).coerceAtMost(MAX_RADIUS * 4)

        val key = entitiesKeyFor(radius) ?: return HttpBridge.ok(
            mapOf("entities" to emptyList<Map<String, Any>>()),
            "Found 0 entities (no player)",
        )
        if (entitiesCacheKey != key) {
            entitiesCacheKey = key
            entitiesCache.invalidate()
        }
        val entities = entitiesCache.get()
        return HttpBridge.ok(mapOf("entities" to entities), "Found ${entities.size} entities")
    }

    private fun blocksKeyFor(radius: Int, types: Set<String>): BlocksCacheKey? {
        val player = MinecraftClient.getInstance().player ?: return null
        val pos = player.blockPos
        return BlocksCacheKey(radius, types, pos.x, pos.y, pos.z)
    }

    private fun entitiesKeyFor(radius: Int): EntitiesCacheKey? {
        val player = MinecraftClient.getInstance().player ?: return null
        val pos = player.blockPos
        return EntitiesCacheKey(radius, pos.x, pos.y, pos.z)
    }

    /**
     * Tick-thread block scan.
     *
     * Iterates an integer cube around the player's BlockPos but computes
     * distance from the player's float position (block-center coords). This
     * matches the legacy WorldCache.query_blocks path the agent normally
     * sees — see `bridge/world_cache.py:262` — so a parity diff against an
     * agent session shows real world drift, not coord-rounding artefacts.
     */
    private fun readBlocksOnTick(key: BlocksCacheKey): List<Map<String, Any>> {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return emptyList()
        val player = mc.player ?: return emptyList()
        val anchor = player.blockPos
        val (ax, ay, az) = Triple(anchor.x, anchor.y, anchor.z)
        val (fx, fy, fz) = Triple(player.x, player.y, player.z)
        val r = key.radius
        val rSqF = (r * r).toDouble()
        val typeFilter = key.types.takeIf { it.isNotEmpty() }

        val out = ArrayList<Map<String, Any>>(64)
        val mut = BlockPos.Mutable()
        for (dx in -r..r) {
            for (dy in -r..r) {
                for (dz in -r..r) {
                    val bx = ax + dx
                    val by = ay + dy
                    val bz = az + dz
                    val ddx = bx - fx
                    val ddy = by - fy
                    val ddz = bz - fz
                    val distSqF = ddx * ddx + ddy * ddy + ddz * ddz
                    if (distSqF > rSqF) continue
                    mut.set(bx, by, bz)
                    val state = world.getBlockState(mut)
                    if (state.isAir) continue
                    val name = Registries.BLOCK.getId(state.block).path
                    if (typeFilter != null && name !in typeFilter) continue
                    out.add(
                        mapOf(
                            "name" to name,
                            "x" to bx,
                            "y" to by,
                            "z" to bz,
                            "distance" to roundDistance(sqrt(distSqF)),
                        )
                    )
                }
            }
        }
        out.sortBy { (it["distance"] as Double) }
        return out
    }

    /** Tick-thread entity scan, includes the local player at distance 0. */
    private fun readEntitiesOnTick(key: EntitiesCacheKey): List<Map<String, Any>> {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return emptyList()
        val player = mc.player ?: return emptyList()
        val origin = player.pos
        val r = key.radius.toDouble()
        val rSq = r * r

        val out = ArrayList<Map<String, Any>>()
        for (entity in world.entities) {
            val dx = entity.x - origin.x
            val dy = entity.y - origin.y
            val dz = entity.z - origin.z
            val distSq = dx * dx + dy * dy + dz * dz
            if (distSq > rSq) continue
            val type = Registries.ENTITY_TYPE.getId(entity.type).path
            val name = entity.name.string.ifEmpty { type }
            val health = if (entity is net.minecraft.entity.LivingEntity) entity.health.toDouble() else 0.0
            out.add(
                mapOf(
                    "id" to entity.id,
                    "name" to name,
                    "type" to type,
                    "x" to entity.x,
                    "y" to entity.y,
                    "z" to entity.z,
                    "distance" to roundDistance(sqrt(distSq)),
                    "health" to health,
                )
            )
        }
        // Match the legacy WorldCache.query_entities path agents normally see.
        out.sortBy { (it["distance"] as Double) }
        return out
    }

    /** Match the legacy `round(d, 1)` semantics so parity diffs stay clean. */
    private fun roundDistance(d: Double): Double = (d * 10.0).roundToInt() / 10.0
}

/** Parse `?key=val&key2=val2` from an HttpExchange URI. */
internal fun HttpExchange.queryParams(): Map<String, String> {
    val raw = requestURI.rawQuery ?: return emptyMap()
    val out = mutableMapOf<String, String>()
    for (pair in raw.split('&')) {
        if (pair.isEmpty()) continue
        val eq = pair.indexOf('=')
        if (eq < 0) {
            out[urlDecode(pair)] = ""
        } else {
            out[urlDecode(pair.substring(0, eq))] = urlDecode(pair.substring(eq + 1))
        }
    }
    return out
}

private fun urlDecode(s: String): String =
    java.net.URLDecoder.decode(s, java.nio.charset.StandardCharsets.UTF_8)
