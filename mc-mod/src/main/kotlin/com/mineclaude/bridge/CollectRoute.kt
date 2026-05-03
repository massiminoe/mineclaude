package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.ItemEntity
import net.minecraft.registry.Registries
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory
import kotlin.math.floor
import kotlin.math.sqrt

/**
 * `POST /collect {radius?}` — walk to and pick up dropped item entities
 * within `radius` of the player.
 *
 * Reactive re-aim loop with structurally-valid goto targets and stall
 * detection: every [POLL_MS] we re-scan, pick the closest on-ground
 * untried item, choose a standable goto target by inspecting the actual
 * support blocks under the item BB (not just `entity.blockPos`, which
 * for items resting on a block edge points at the adjacent air column).
 *
 * Per-target failure paths:
 *   - no solid block within the item's BB footprint → mark unreachable
 *     `no-standable-support` and try the next-closest item.
 *   - player position frozen for [STUCK_POLLS] consecutive polls while a
 *     goto is active → mark unreachable `baritone-stalled` (Baritone
 *     accepted the goal but its pathfinder gave up silently — the same
 *     pattern GotoRoute uses to detect stalls).
 *
 * Pickup is detected via inventory delta, not world-entity disappearance,
 * so creeper-grabs / chunk-unloads don't spuriously inflate the count.
 *
 * Exits when:
 *   - all on-ground items in radius have been tried,
 *   - [EMPTY_SCAN_EXIT_THRESHOLD] consecutive scans find no items at all,
 *   - the [OVERALL_BUDGET_MS] wall-clock budget is exhausted.
 *
 * Returns `{collected: N, unreachable: [{name, pos, dist, reason}, …]}`
 * plus a message string that mirrors the unreachable summary so it
 * surfaces through the agent's text-only `_check` path.
 */
object CollectRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.collect")!!

    private const val OVERALL_BUDGET_MS = 18_000L
    private const val POLL_MS = 250L
    private const val FINAL_SETTLE_MS = 600L
    private const val EMPTY_SCAN_EXIT_THRESHOLD = 3
    private const val STUCK_POLLS = 4   // ~1 s of frozen position with goto active
    private const val ITEM_BB_HALF = 0.125  // ItemEntity is 0.25×0.25; BB half-width

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/collect") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val radius = (body["radius"] as? Number)?.toDouble() ?: 3.0

        val callId = "%04x".format((System.nanoTime() ushr 8).toInt() and 0xFFFF)
        log.info("collect[{}]: BEGIN radius={}", callId, radius)
        val result = runCollect(callId, radius)
        log.info("collect[{}]: END collected={} unreachable={}",
            callId, result.collected, result.unreachable.size)
        return HttpBridge.ok(
            mapOf(
                "collected" to result.collected,
                "unreachable" to result.unreachable.map { it.toMap() },
            ),
            buildMessage(result),
        )
    }

    private data class ItemSnapshot(
        val id: Int,
        val x: Double, val y: Double, val z: Double,
        val name: String, val dist: Double,
        val vy: Double, val onGround: Boolean, val age: Int,
        val blockPos: BlockPos,
    )

    private data class ScanResult(
        val px: Double, val py: Double, val pz: Double,
        val items: List<ItemSnapshot>,
    )

    private data class UnreachableItem(
        val name: String,
        val pos: Triple<Double, Double, Double>,
        val dist: Double,
        val reason: String,
    ) {
        fun toMap() = mapOf(
            "name" to name,
            "pos" to listOf(pos.first, pos.second, pos.third),
            "dist" to dist,
            "reason" to reason,
        )
    }

    private data class CollectResult(
        val collected: Int,
        val unreachable: List<UnreachableItem>,
    )

    private fun fmt(v: Double) = "%.2f".format(v)

    private fun buildMessage(r: CollectResult): String {
        val head = if (r.collected > 0) "Collected ${r.collected} item(s)" else "No items to collect"
        if (r.unreachable.isEmpty()) return head
        val tail = r.unreachable.joinToString("; ") { u ->
            val (x, y, z) = u.pos
            "${u.name} at (${x.toInt()},${y.toInt()},${z.toInt()}) [${u.reason}]"
        }
        return "$head; ${r.unreachable.size} unreachable: $tail"
    }

    private fun runCollect(callId: String, radius: Double): CollectResult {
        val startInv = snapshotInventory()
        val heldSlotAtStart = snapshotHeldSlot()
        val deadline = System.currentTimeMillis() + OVERALL_BUDGET_MS

        var lastGoto: BlockPos? = null
        var lastTargetId: Int? = null
        var lastPosRounded: Triple<Double, Double, Double>? = null
        var stallPolls = 0
        var consecutiveEmptyScans = 0
        var anyGotoIssued = false
        val tried = HashSet<Int>()
        val unreachable = ArrayList<UnreachableItem>()
        var iter = 0

        while (System.currentTimeMillis() < deadline) {
            val scan = scanItems(radius)
            val unTried = scan.items.filter { it.id !in tried }
            val onGround = unTried.filter { it.onGround }
            val falling = unTried.filter { !it.onGround }
            log.info(
                "collect[{}]: poll={} player=({},{},{}) scan_n={} untried={} on_ground={} falling={} tried={}",
                callId, iter, fmt(scan.px), fmt(scan.py), fmt(scan.pz),
                scan.items.size, unTried.size, onGround.size, falling.size, tried.size,
            )
            if (unTried.isNotEmpty()) {
                for ((idx, item) in unTried.sortedBy { it.dist }.withIndex().take(3)) {
                    log.info(
                        "collect[{}]:   cand[{}] {}#{} pos=({},{},{}) d={} on_ground={} age={}",
                        callId, idx, item.name, item.id,
                        fmt(item.x), fmt(item.y), fmt(item.z), fmt(item.dist),
                        item.onGround, item.age,
                    )
                }
            }

            if (unTried.isEmpty()) {
                if (scan.items.isEmpty()) {
                    consecutiveEmptyScans++
                    if (consecutiveEmptyScans == 1 && iter == 0) {
                        widerScanLog(callId, radius)
                    }
                    if (consecutiveEmptyScans >= EMPTY_SCAN_EXIT_THRESHOLD) {
                        log.info("collect[{}]: exit reason=empty-scans-x{}", callId, consecutiveEmptyScans)
                        break
                    }
                } else {
                    // All remaining items have been tried and marked unreachable.
                    log.info("collect[{}]: exit reason=all-untried-exhausted", callId)
                    break
                }
                Thread.sleep(POLL_MS)
                iter++
                continue
            }
            consecutiveEmptyScans = 0

            if (onGround.isEmpty()) {
                // Only falling items left untried — wait for them to land. Don't
                // issue a new goto, don't count toward exit.
                log.info("collect[{}]: poll={} all-falling — waiting", callId, iter)
                Thread.sleep(POLL_MS)
                iter++
                continue
            }

            val target = onGround.minBy { it.dist }
            val gotoTarget = chooseGotoTarget(target)

            if (gotoTarget == null) {
                log.info("collect[{}]: poll={} target {}#{} no standable support → mark unreachable",
                    callId, iter, target.name, target.id)
                tried += target.id
                unreachable += UnreachableItem(
                    name = target.name,
                    pos = Triple(target.x, target.y, target.z),
                    dist = target.dist,
                    reason = "no-standable-support",
                )
                lastGoto = null; lastTargetId = null; lastPosRounded = null; stallPolls = 0
                continue
            }

            if (gotoTarget != lastGoto || target.id != lastTargetId) {
                val cmd = "#goto ${gotoTarget.x} ${gotoTarget.y} ${gotoTarget.z}"
                log.info("collect[{}]: poll={} retarget {}#{} goto=\"{}\" d={} item_pos=({},{},{})",
                    callId, iter, target.name, target.id, cmd, fmt(target.dist),
                    fmt(target.x), fmt(target.y), fmt(target.z))
                sendBaritoneCommand(cmd)
                lastGoto = gotoTarget
                lastTargetId = target.id
                lastPosRounded = null
                stallPolls = 0
                anyGotoIssued = true
            } else {
                // Same target as previous poll — track player progress for stall
                // detection. Round to 0.1 so micro-jitter doesn't reset the counter.
                val rounded = Triple(round1(scan.px), round1(scan.py), round1(scan.pz))
                if (rounded == lastPosRounded) {
                    stallPolls++
                    if (stallPolls >= STUCK_POLLS) {
                        log.warn("collect[{}]: poll={} target {}#{} stalled at goto={} for {} polls → mark unreachable",
                            callId, iter, target.name, target.id, gotoTarget, stallPolls)
                        try { sendBaritoneCommand("#stop") } catch (_: Throwable) {}
                        tried += target.id
                        unreachable += UnreachableItem(
                            name = target.name,
                            pos = Triple(target.x, target.y, target.z),
                            dist = target.dist,
                            reason = "baritone-stalled",
                        )
                        lastGoto = null; lastTargetId = null; lastPosRounded = null; stallPolls = 0
                        continue
                    }
                } else {
                    stallPolls = 0
                    lastPosRounded = rounded
                }
            }

            Thread.sleep(POLL_MS)
            iter++
        }

        if (anyGotoIssued) {
            try { sendBaritoneCommand("#stop") } catch (t: Throwable) {
                log.warn("collect[{}]: failed to #stop on exit: {}", callId, t.message)
            }
        }
        Thread.sleep(FINAL_SETTLE_MS)

        restoreHeldSlot(heldSlotAtStart)

        val endInv = snapshotInventory()
        val collected = inventoryDelta(startInv, endInv)
        val diffStr = (endInv.keys + startInv.keys).distinct().mapNotNull { name ->
            val d = (endInv[name] ?: 0) - (startInv[name] ?: 0)
            if (d != 0) "$name${if (d > 0) "+" else ""}$d" else null
        }.joinToString(", ")
        log.info("collect[{}]: inventory delta {{{}}} → collected={}", callId, diffStr, collected)
        return CollectResult(
            collected = collected,
            unreachable = unreachable,
        )
    }

    /**
     * Find a standable goto target near [item]. `entity.blockPos` is often
     * a non-navigable position — for items sitting on a block edge it floors
     * to the adjacent air column, and for items on top of pillars (e.g. log
     * tops) the column directly above is head-blocked. We instead enumerate
     * the 3×3 grid of standing-feet positions within pickup reach of the
     * item's xz at the item's y level, validate each for support+feet+head
     * clearance, and pick the closest valid one.
     *
     * Returns null if no fully-standable position is within pickup reach
     * — caller treats this as `no-standable-support`.
     */
    private fun chooseGotoTarget(item: ItemSnapshot): BlockPos? {
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            val world = MinecraftClient.getInstance().world ?: return@submitAndWait null
            val itemY = floor(item.y).toInt()
            val itemBlockX = floor(item.x).toInt()
            val itemBlockZ = floor(item.z).toInt()

            // Candidate standing-feet positions: 3×3 around the item's column,
            // tried at y=item.y first (player BB overlaps item BB) and y=item.y-1
            // as fallback (player BB top reaches item — magnetism range).
            val candidates = mutableListOf<BlockPos>()
            for (dy in listOf(0, -1)) for (dx in -1..1) for (dz in -1..1) {
                candidates += BlockPos(itemBlockX + dx, itemY + dy, itemBlockZ + dz)
            }

            val ranked = candidates.sortedBy { p ->
                val cx = p.x + 0.5; val cz = p.z + 0.5
                val dx = cx - item.x; val dz = cz - item.z
                // Prefer same y first, then any y, then horizontal proximity.
                val dyPenalty = if (p.y == itemY) 0.0 else 0.5
                dx * dx + dz * dz + dyPenalty
            }

            for (standingFeet in ranked) {
                val support = standingFeet.down()
                val supportState = world.getBlockState(support)
                if (!supportState.isSolidBlock(world, support)) continue
                val feetState = world.getBlockState(standingFeet)
                if (feetState.isSolidBlock(world, standingFeet)) continue
                val headPos = standingFeet.up()
                val headState = world.getBlockState(headPos)
                if (headState.isSolidBlock(world, headPos)) continue
                return@submitAndWait standingFeet
            }
            null
        }
    }

    private fun widerScanLog(callId: String, radius: Double) {
        val wide = scanItems(radius * 4).items
        if (wide.isNotEmpty()) {
            val preview = wide.sortedBy { it.dist }.take(5).joinToString(", ") {
                "${it.name}@(${fmt(it.x)},${fmt(it.y)},${fmt(it.z)}) d=${fmt(it.dist)}"
            }
            log.info("collect[{}]: wider scan r={} found {} item(s) outside collect radius — top 5: {}",
                callId, radius * 4, wide.size, preview)
        }
    }

    private fun round1(v: Double): Double = Math.round(v * 10.0) / 10.0

    /**
     * Tick-thread scan: enumerate `world.entities`, filter to ItemEntity,
     * compute foot-distance from the player. Single submission so the
     * snapshot is consistent across all items.
     */
    private fun scanItems(radius: Double): ScanResult {
        return TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val world = mc.world ?: return@submitAndWait ScanResult(0.0, 0.0, 0.0, emptyList())
            val player = mc.player ?: return@submitAndWait ScanResult(0.0, 0.0, 0.0, emptyList())
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
                val name = Registries.ITEM.getId(entity.stack.item).path
                out.add(ItemSnapshot(
                    id = entity.id,
                    x = entity.x, y = entity.y, z = entity.z,
                    name = name, dist = sqrt(distSq),
                    vy = entity.velocity.y,
                    onGround = entity.isOnGround,
                    age = entity.itemAge,
                    blockPos = entity.blockPos,
                ))
            }
            ScanResult(px, py, pz, out)
        }
    }

    private fun snapshotInventory(): Map<String, Int> {
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait emptyMap<String, Int>()
            val inv = player.inventory
            val out = HashMap<String, Int>()
            for (i in 0 until 36) {
                val stack = inv.getStack(i)
                if (stack.isEmpty) continue
                val name = Registries.ITEM.getId(stack.item).path
                out[name] = (out[name] ?: 0) + stack.count
            }
            out
        }
    }

    private fun inventoryDelta(start: Map<String, Int>, end: Map<String, Int>): Int {
        var positive = 0
        for (name in end.keys + start.keys) {
            val delta = (end[name] ?: 0) - (start[name] ?: 0)
            if (delta > 0) positive += delta
        }
        return positive
    }

    private fun snapshotHeldSlot(): Int? = TickThread.submitAndWait(timeoutMs = 1_000) {
        MinecraftClient.getInstance().player?.inventory?.selectedSlot
    }

    private fun restoreHeldSlot(slot: Int?) {
        if (slot == null) return
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait
            if (player.inventory.selectedSlot != slot) {
                log.info("collect: restoring held slot {} → {} after collect", player.inventory.selectedSlot, slot)
                player.inventory.selectedSlot = slot
                player.networkHandler.sendPacket(
                    net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket(slot)
                )
            }
        }
    }
}
