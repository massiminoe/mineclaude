package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.ItemEntity
import org.slf4j.LoggerFactory
import kotlin.math.sqrt

/**
 * `POST /collect {radius?}` — walk to and pick up dropped item entities
 * within `radius` of the player.
 *
 * Mirrors `bridge.player_control.collect_nearby_items` semantics:
 *   - up to 4 iterations,
 *   - 18 s overall wall-clock budget (well under the 90 s HTTP timeout),
 *   - each iteration: scan ItemEntity types, sort by foot distance, target
 *     closest; if within 1.0 reach, settle for auto-pickup (300 ms); else
 *     send `#goto`, poll for 1.5-reach with a 3 s walk budget, then `#stop`.
 *   - re-scan the radius after each iteration; if the count didn't drop
 *     (delta ≤ 0), bail out (no progress).
 *
 * Collection-by-delta is the legacy signal of choice: MC's vanilla pickup
 * loop fires when the player capsule overlaps the item entity, which is
 * easy to detect post-hoc (entity disappeared from `world.entities`) but
 * hard to predict (depends on player walking trajectory).
 *
 * Returns `{"collected": N}` on success — same shape as legacy.
 */
object CollectRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.collect")!!

    private const val MAX_ITERATIONS = 4
    private const val OVERALL_BUDGET_MS = 18_000L
    private const val WALK_BUDGET_MS = 3_000L
    private const val WALK_POLL_MS = 250L
    private const val PICKUP_REACH = 1.0
    private const val WALK_REACH = 1.5
    private const val SETTLE_MS = 300L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/collect") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val radius = (body["radius"] as? Number)?.toDouble() ?: 3.0

        val collected = runCollect(radius)
        val message = if (collected > 0) "Collected $collected item(s)" else "No items to collect"
        return HttpBridge.ok(mapOf("collected" to collected), message)
    }

    /**
     * Item-entity description captured on the tick thread so the HTTP-side
     * loop can sort and decide without re-entering MC state.
     */
    private data class ItemSnapshot(val id: Int, val x: Double, val y: Double, val z: Double, val name: String, val dist: Double)

    private fun runCollect(radius: Double): Int {
        // Initial settle for any in-flight item entities (drops dropped on
        // the same tick as the request landed).
        Thread.sleep(200)

        val deadline = System.currentTimeMillis() + OVERALL_BUDGET_MS
        var collected = 0

        for (iteration in 0 until MAX_ITERATIONS) {
            if (System.currentTimeMillis() >= deadline) {
                log.info("collect: overall time budget exhausted")
                break
            }

            val items = scanItems(radius)
            if (items.isEmpty()) {
                if (iteration == 0) {
                    val wide = scanItems(radius * 4)
                    if (wide.isNotEmpty()) {
                        val preview = wide.sortedBy { it.dist }.take(5).joinToString(", ") {
                            "${it.name}@(${"%.1f".format(it.x)},${"%.1f".format(it.y)},${"%.1f".format(it.z)}) d=${"%.1f".format(it.dist)}"
                        }
                        log.info("collect: wider scan r={} found {} item(s) outside collect radius — top 5: {}",
                            radius * 4, wide.size, preview)
                    } else {
                        log.info("collect: wider scan r={} also empty — no drops spawned or they despawned", radius * 4)
                    }
                }
                break
            }

            val sorted = items.sortedBy { it.dist }
            val target = sorted.first()
            val beforeCount = sorted.size
            log.info("collect: targeting {} at ({},{},{}) dist={} ({} item(s) in range)",
                target.name, "%.1f".format(target.x), "%.1f".format(target.y), "%.1f".format(target.z),
                "%.2f".format(target.dist), beforeCount)

            if (target.dist <= PICKUP_REACH) {
                Thread.sleep(SETTLE_MS)
            } else {
                val walkBudget = (deadline - System.currentTimeMillis()).coerceAtMost(WALK_BUDGET_MS)
                if (walkBudget <= 0) break
                walkToward(target, walkBudget)
            }

            val after = scanItems(radius)
            val delta = beforeCount - after.size
            if (delta <= 0) {
                log.info("collect: no progress (before={}, after={}), stopping", beforeCount, after.size)
                break
            }
            collected += delta
        }

        log.info("collect: picked up {} item(s)", collected)
        return collected
    }

    /**
     * Tick-thread scan: enumerate `world.entities`, filter to ItemEntity,
     * compute foot-distance from the player. Single submission so the
     * snapshot is consistent across all items.
     */
    private fun scanItems(radius: Double): List<ItemSnapshot> {
        return TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val world = mc.world ?: return@submitAndWait emptyList()
            val player = mc.player ?: return@submitAndWait emptyList()
            val px = player.x
            val py = player.y
            val pz = player.z
            val rSq = radius * radius
            val out = ArrayList<ItemSnapshot>()
            for (entity in world.entities) {
                if (entity !is ItemEntity) continue
                val dx = entity.x - px
                val dy = entity.y - py
                val dz = entity.z - pz
                val distSq = dx * dx + dy * dy + dz * dz
                if (distSq > rSq) continue
                val name = entity.stack.item.let { stack ->
                    net.minecraft.registry.Registries.ITEM.getId(stack).path
                }
                out.add(ItemSnapshot(entity.id, entity.x, entity.y, entity.z, name, sqrt(distSq)))
            }
            out
        }
    }

    /**
     * Send `#goto` to the item's coord (truncated to int — Baritone goal),
     * poll player position vs the item's coord every 250 ms until within
     * [WALK_REACH] or `walkBudgetMs` elapses. Always emits `#stop` on exit
     * + small settle so auto-pickup has a chance to fire.
     *
     * Item is targeted by stable id so we can detect mid-walk despawn /
     * pickup-by-other-process and bail early.
     */
    private fun walkToward(target: ItemSnapshot, walkBudgetMs: Long) {
        sendBaritoneCommand("#goto ${target.x.toInt()} ${target.y.toInt()} ${target.z.toInt()}")
        val walkDeadline = System.currentTimeMillis() + walkBudgetMs
        try {
            while (System.currentTimeMillis() < walkDeadline) {
                Thread.sleep(WALK_POLL_MS)
                val arrivedOrGone = TickThread.submitAndWait(timeoutMs = 1_000) {
                    val mc = MinecraftClient.getInstance()
                    val world = mc.world ?: return@submitAndWait true
                    val player = mc.player ?: return@submitAndWait true
                    // Item picked up / despawned mid-walk → done with this target.
                    val live = world.getEntityById(target.id) as? ItemEntity ?: return@submitAndWait true
                    val dx = live.x - player.x
                    val dy = live.y - player.y
                    val dz = live.z - player.z
                    sqrt(dx * dx + dy * dy + dz * dz) <= WALK_REACH
                }
                if (arrivedOrGone) break
            }
        } finally {
            try { sendBaritoneCommand("#stop") } catch (t: Throwable) {
                log.warn("collect: failed to #stop after walk: {}", t.message)
            }
            Thread.sleep(SETTLE_MS)
        }
    }
}
