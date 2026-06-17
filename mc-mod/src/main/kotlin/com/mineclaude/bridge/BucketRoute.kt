package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /bucket/fill {x, y, z}` and `POST /bucket/empty {x, y, z, item?}` —
 * the dedicated bucket surface. Buckets used to ride the unified `/use`
 * path, which forced the agent to hand-compute a `look_at` point AND know
 * which water cell was a fillable *source* (invisible from block ids alone —
 * source and flowing both read as "water"). Both jobs are world geometry the
 * bridge can do honestly, so they live here. The agent only names a cell.
 *
 * # Why two endpoints, not one /use
 * Vanilla [net.minecraft.item.BucketItem.use] raycasts itself: an *empty*
 * bucket raycasts SOURCE_ONLY (sees source fluids), a *filled* bucket raycasts
 * NONE (sees only solids, then pours onto the clicked face). The two need
 * opposite aim strategies:
 *   - fill  → aim at the source block's centre (the SOURCE_ONLY ray lands on it)
 *   - empty → aim at an adjacent solid's face pointing at the target air cell
 *             (the NONE ray hits the solid, the fluid spills into the target)
 * `/use`'s single "aim at look_at, raycast no-fluids, fall through to item"
 * pipeline can't express "aim at the source" for a fill, which is exactly why
 * fills were finicky. Each endpoint owns its own aim + the navigate-into-reach.
 *
 * Truth-in-return: the inventory delta is authoritative. `filled`/`emptied`
 * are true iff the empty↔filled bucket swap actually happened.
 */
object BucketRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.bucket")!!

    /** Settle window for the server to round-trip the fluid pickup/placement. */
    private const val POST_USE_SETTLE_MS = 150L

    /** Filled-bucket item id → the fluid it carries (path form). */
    private val FILLED_BUCKETS = mapOf("water_bucket" to "water", "lava_bucket" to "lava")

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/bucket/fill") { ex -> handleFill(ex) }
        bridge.addRoute("POST", "/bucket/empty") { ex -> handleEmpty(ex) }
    }

    // -- fill -------------------------------------------------------------------

    private fun handleFill(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val pos = parsePos(body) ?: return HttpBridge.err(
            "fill requires integer x, y, z (the source block to fill from)", status = 400,
        )

        // Validate the named cell is a still *source* of a bucketable fluid.
        when (val v = validateSource(pos)) {
            is SourceCheck.Err -> return HttpBridge.err(v.message)
            is SourceCheck.Ok -> return doFill(pos, v.fluid)
        }
    }

    private sealed interface SourceCheck {
        data class Err(val message: String) : SourceCheck
        data class Ok(val fluid: String) : SourceCheck
    }

    private fun validateSource(pos: BlockPos): SourceCheck = TickThread.submitAndWait(timeoutMs = 1_000) {
        val world = MinecraftClient.getInstance().world
            ?: return@submitAndWait SourceCheck.Err("no world")
        val fs = world.getFluidState(pos)
        if (fs.isEmpty) {
            return@submitAndWait SourceCheck.Err(
                "no fluid at (${pos.x}, ${pos.y}, ${pos.z}) to fill from — that cell is " +
                    WorldHelpers.blockIdAt(pos),
            )
        }
        if (!fs.isStill) {
            return@submitAndWait SourceCheck.Err(
                "fluid at (${pos.x}, ${pos.y}, ${pos.z}) is flowing, not a source — a bucket only " +
                    "fills from a source block; aim at the spring/pool cell, not the runoff",
            )
        }
        val fluid = Registries.FLUID.getId(fs.fluid).path
        if (fluid !in FILLED_BUCKETS.values) {
            return@submitAndWait SourceCheck.Err(
                "fluid at (${pos.x}, ${pos.y}, ${pos.z}) is $fluid — only water and lava are bucketable",
            )
        }
        SourceCheck.Ok(fluid)
    }

    private fun doFill(pos: BlockPos, fluid: String): BridgeResponse {
        // Need an EMPTY bucket in hand. ensureMainhandHolds locates + equips it.
        UseRoute.ensureMainhandHolds("bucket")?.let { return HttpBridge.err(it) }

        navigateIfNeeded(pos)?.let { return it }

        val before = snapshotCounts()
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            WorldHelpers.ensureNoScreenOpen(player)
            // Aim the eye at the source centre — BucketItem's SOURCE_ONLY raycast
            // lands on it and fills.
            WorldHelpers.lookAtPosition(player, pos.x + 0.5, pos.y + 0.5, pos.z + 0.5)
            mgr.interactItem(player, Hand.MAIN_HAND)
            player.swingHand(Hand.MAIN_HAND)
            Unit
        }
        Thread.sleep(POST_USE_SETTLE_MS)
        val delta = computeDelta(before, snapshotCounts())

        val filledItem = FILLED_BUCKETS.entries.first { it.value == fluid }.key
        val filled = (delta[filledItem] ?: 0) > 0
        val data = mutableMapOf<String, Any>(
            "filled" to filled,
            "fluid" to fluid,
            "position" to listOf(pos.x, pos.y, pos.z),
        )
        if (delta.isNotEmpty()) data["inventory_delta"] = delta
        return if (filled) {
            log.info("bucket: filled {} from {} at {}", filledItem, fluid, pos)
            HttpBridge.ok(data, "Filled $filledItem from $fluid at ${pos.x}, ${pos.y}, ${pos.z}")
        } else {
            HttpBridge.err(
                "bucket didn't fill — aim missed the source or it's out of reach (no $filledItem gained)",
            )
        }
    }

    // -- empty ------------------------------------------------------------------

    private fun handleEmpty(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val pos = parsePos(body) ?: return HttpBridge.err(
            "empty requires integer x, y, z (the cell to pour the fluid into)", status = 400,
        )
        val itemHint = (body["item"] as? String)?.takeIf { it.isNotEmpty() }
            ?.removePrefix("minecraft:")

        // Decide which filled bucket to pour.
        val bucket = when (val r = resolveFilledBucket(itemHint)) {
            is BucketChoice.Err -> return HttpBridge.err(r.message)
            is BucketChoice.Ok -> r.item
        }
        val fluid = FILLED_BUCKETS.getValue(bucket)

        // Target cell must be free for the fluid to occupy. Vanilla pours into a
        // replaceable cell (air, grass, water it will overwrite); a solid block
        // would just bounce the click.
        val targetErr = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().world ?: return@submitAndWait "no world"
            if (!WorldHelpers.isReplaceable(pos)) {
                "cell (${pos.x}, ${pos.y}, ${pos.z}) is occupied by ${WorldHelpers.blockIdAt(pos)} — " +
                    "pour into an empty cell"
            } else null
        }
        if (targetErr != null) return HttpBridge.err(targetErr)

        // Need a solid neighbour to click against; the fluid spills onto the
        // face pointing at the target — same anchor logic /place uses.
        val anchor = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().world ?: return@submitAndWait null
            WorldHelpers.findAdjacentSolidBlock(pos)
        } ?: return HttpBridge.err(
            "no solid block beside (${pos.x}, ${pos.y}, ${pos.z}) to pour against — fluid needs a " +
                "surface to spill from; place a block first or pick an edge cell",
        )

        return doEmpty(pos, bucket, fluid, anchor)
    }

    private sealed interface BucketChoice {
        data class Err(val message: String) : BucketChoice
        data class Ok(val item: String) : BucketChoice
    }

    /** Pick the filled bucket to use: honour [hint] if given, else the lone one in inventory. */
    private fun resolveFilledBucket(hint: String?): BucketChoice {
        val present = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player
                ?: return@submitAndWait emptySet<String>()
            val have = HashSet<String>()
            val inv = player.inventory
            for (i in 0 until 36) {
                val name = Registries.ITEM.getId(inv.getStack(i).item).path
                if (name in FILLED_BUCKETS.keys) have.add(name)
            }
            have
        }
        if (hint != null) {
            if (hint !in FILLED_BUCKETS.keys) {
                return BucketChoice.Err("$hint isn't a filled bucket — pass water_bucket or lava_bucket")
            }
            if (hint !in present) return BucketChoice.Err("No $hint in inventory")
            return BucketChoice.Ok(hint)
        }
        return when (present.size) {
            0 -> BucketChoice.Err("No filled bucket in inventory (need water_bucket or lava_bucket)")
            1 -> BucketChoice.Ok(present.first())
            else -> BucketChoice.Err(
                "inventory has both water_bucket and lava_bucket — pass item= to say which to pour",
            )
        }
    }

    private fun doEmpty(
        pos: BlockPos,
        bucket: String,
        fluid: String,
        anchor: WorldHelpers.Adjacent,
    ): BridgeResponse {
        UseRoute.ensureMainhandHolds(bucket)?.let { return HttpBridge.err(it) }

        navigateIfNeeded(pos)?.let { return it }

        val before = snapshotCounts()
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            WorldHelpers.ensureNoScreenOpen(player)
            // Aim at the point on the anchor's face that points at the target,
            // so BucketItem's NONE raycast hits the solid and pours into [pos].
            val face = anchor.face
            WorldHelpers.lookAtPosition(
                player,
                anchor.pos.x + 0.5 + face.offsetX * 0.5,
                anchor.pos.y + 0.5 + face.offsetY * 0.5,
                anchor.pos.z + 0.5 + face.offsetZ * 0.5,
            )
            mgr.interactItem(player, Hand.MAIN_HAND)
            player.swingHand(Hand.MAIN_HAND)
            Unit
        }
        Thread.sleep(POST_USE_SETTLE_MS)
        val delta = computeDelta(before, snapshotCounts())

        val emptied = (delta[bucket] ?: 0) < 0
        val data = mutableMapOf<String, Any>(
            "emptied" to emptied,
            "fluid" to fluid,
            "position" to listOf(pos.x, pos.y, pos.z),
        )
        if (delta.isNotEmpty()) data["inventory_delta"] = delta
        return if (emptied) {
            log.info("bucket: poured {} into {}", fluid, pos)
            HttpBridge.ok(data, "Poured $fluid at ${pos.x}, ${pos.y}, ${pos.z}")
        } else {
            HttpBridge.err(
                "bucket didn't empty — the pour didn't take (still holding $bucket); cell may be " +
                    "obstructed or out of reach",
            )
        }
    }

    // -- shared helpers ---------------------------------------------------------

    private fun parsePos(body: Map<String, Any?>): BlockPos? {
        val x = (body["x"] as? Number)?.toInt()
        val y = (body["y"] as? Number)?.toInt()
        val z = (body["z"] as? Number)?.toInt()
        return if (x != null && y != null && z != null) BlockPos(x, y, z) else null
    }

    /** Navigate within reach of [pos] if not already. Returns an error response, or null on success. */
    private fun navigateIfNeeded(pos: BlockPos): BridgeResponse? {
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(p, pos)
        }
        if (inReach) return null
        val nav = Navigation.navigateNear(pos)
        return if (nav is Navigation.Result.Failed) {
            HttpBridge.err("couldn't reach (${pos.x}, ${pos.y}, ${pos.z}): ${nav.reason}")
        } else null
    }

    private fun snapshotCounts(): Map<String, Int> = TickThread.submitAndWait(timeoutMs = 1_000) {
        val player = MinecraftClient.getInstance().player ?: return@submitAndWait emptyMap<String, Int>()
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

    private fun computeDelta(start: Map<String, Int>, end: Map<String, Int>): Map<String, Int> {
        val out = LinkedHashMap<String, Int>()
        for (name in (start.keys + end.keys)) {
            val d = (end[name] ?: 0) - (start[name] ?: 0)
            if (d != 0) out[name] = d
        }
        return out
    }
}
