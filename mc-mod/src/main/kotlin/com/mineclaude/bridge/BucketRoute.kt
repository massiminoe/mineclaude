package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.block.FluidFillable
import net.minecraft.client.MinecraftClient
import net.minecraft.fluid.Fluid
import net.minecraft.fluid.Fluids
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.hit.HitResult
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Direction
import net.minecraft.util.math.Vec3d
import net.minecraft.world.World
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

        return doEmpty(pos, bucket, fluid)
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

    private sealed interface PourOutcome {
        data class Fail(val message: String) : PourOutcome
        /** No candidate sightline hit any block in reach. */
        data object Blocked : PourOutcome
        /** A sightline exists, but vanilla would pour into [landsAt], not the target. */
        data class Diverts(val landsAt: BlockPos) : PourOutcome
        /** A clean aim was found and the pour was fired. */
        data object Poured : PourOutcome
    }

    private fun doEmpty(pos: BlockPos, bucket: String, fluid: String): BridgeResponse {
        UseRoute.ensureMainhandHolds(bucket)?.let { return HttpBridge.err(it) }

        navigateIfNeeded(pos)?.let { return it }

        val fluidObj = if (fluid == "water") Fluids.WATER else Fluids.LAVA

        // A filled BucketItem ignores any hit we'd hand interactBlock — it runs
        // its OWN eye-raycast (FluidHandling.NONE) inside use(). So we can't
        // inject a placement; we can only aim the head and let vanilla resolve.
        // To stop the fluid landing somewhere we didn't ask for (the rim of a
        // pit, a near lip), we PREVIEW: for each candidate aim, raycast exactly
        // as vanilla will and compute the cell it would place into. We only fire
        // when a candidate resolves to the target — same tick, same rotation, so
        // the commit raycast matches the preview. If none does, we DON'T pour
        // (no silent mess); we report where the open sightline would have gone.
        val before = snapshotCounts()
        val outcome = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait PourOutcome.Fail("no player")
            val world = mc.world ?: return@submitAndWait PourOutcome.Fail("no world")
            val mgr = mc.interactionManager ?: return@submitAndWait PourOutcome.Fail("no interaction manager")
            WorldHelpers.ensureNoScreenOpen(player)

            var diverted: BlockPos? = null
            for (aim in pourCandidates(world, pos)) {
                WorldHelpers.lookAtPosition(player, aim.x, aim.y, aim.z)
                val hr = player.raycast(WorldHelpers.BLOCK_REACH, 1.0f, /*includeFluids=*/ false)
                if (hr.type != HitResult.Type.BLOCK) continue
                val landsAt = predictPlacement(world, hr as BlockHitResult, fluidObj)
                if (landsAt == pos) {
                    // Commit on this rotation — vanilla's use() raycast re-runs
                    // from here and lands the same cell.
                    mgr.interactItem(player, Hand.MAIN_HAND)
                    player.swingHand(Hand.MAIN_HAND)
                    return@submitAndWait PourOutcome.Poured
                }
                if (diverted == null) diverted = landsAt
            }
            if (diverted != null) PourOutcome.Diverts(diverted) else PourOutcome.Blocked
        }

        when (outcome) {
            is PourOutcome.Fail -> return HttpBridge.err(outcome.message)
            is PourOutcome.Blocked -> return HttpBridge.err(
                "nothing in reach to pour against at (${pos.x}, ${pos.y}, ${pos.z}) — a bucket needs a " +
                    "solid face beside the cell; move closer or place a block to spill from",
            )
            is PourOutcome.Diverts -> {
                val d = outcome.landsAt
                return HttpBridge.err(
                    "couldn't line up a pour into (${pos.x}, ${pos.y}, ${pos.z}) from here — the open " +
                        "sightline lands at (${d.x}, ${d.y}, ${d.z}) instead (a lip or wall deflects " +
                        "it). Break that edge, stand on a different side, or pick a more open cell.",
                )
            }
            is PourOutcome.Poured -> Unit
        }

        Thread.sleep(POST_USE_SETTLE_MS)
        val delta = computeDelta(before, snapshotCounts())
        val emptied = (delta[bucket] ?: 0) < 0

        // Verify a real source of our fluid now sits in the target — the
        // authoritative truth-in-return, not an echo of the request.
        val landed = TickThread.submitAndWait(timeoutMs = 1_000) {
            val w = MinecraftClient.getInstance().world ?: return@submitAndWait false
            val fs = w.getFluidState(pos)
            !fs.isEmpty && fs.fluid == fluidObj && fs.isStill
        }

        val data = mutableMapOf<String, Any>(
            "emptied" to emptied,
            "fluid" to fluid,
            "requested" to listOf(pos.x, pos.y, pos.z),
            "placed_at" to listOf(pos.x, pos.y, pos.z),
            "verified" to landed,
        )
        if (delta.isNotEmpty()) data["inventory_delta"] = delta
        return when {
            emptied && landed -> {
                log.info("bucket: poured {} into {} (verified source)", fluid, pos)
                HttpBridge.ok(data, "Poured $fluid at ${pos.x}, ${pos.y}, ${pos.z}")
            }
            emptied -> HttpBridge.partial(
                data,
                "Emptied $bucket aimed at (${pos.x}, ${pos.y}, ${pos.z}) but couldn't confirm a $fluid " +
                    "source there — it may have flowed off or the cell was obstructed",
            )
            else -> HttpBridge.err(
                "bucket didn't empty — the pour didn't take (still holding $bucket); cell may be " +
                    "obstructed or out of reach",
            )
        }
    }

    /**
     * Aim points to try for pouring into [target], in preference order. Each
     * adjacent solid yields an aim at the face it shares with [target] (so a
     * NONE raycast hitting that face spills the fluid into [target]); we add the
     * cell centre + floor point last for open-ground pours. The caller previews
     * each with a real raycast and keeps the first that resolves to [target] —
     * so a pit's FAR wall (clean downward line) wins over the near lip the bot
     * stands on (which the preview rejects). DOWN is first: "pour on the ground"
     * is the common case and the floor is the natural anchor.
     */
    private fun pourCandidates(world: World, target: BlockPos): List<Vec3d> {
        val out = ArrayList<Vec3d>()
        for (d in Direction.values()) {
            val neighbour = target.offset(d)
            if (world.getBlockState(neighbour).isReplaceable) continue // air/fluid/grass — not an anchor
            val faceTowardTarget = d.opposite
            out.add(
                Vec3d(
                    neighbour.x + 0.5 + faceTowardTarget.offsetX * 0.5,
                    neighbour.y + 0.5 + faceTowardTarget.offsetY * 0.5,
                    neighbour.z + 0.5 + faceTowardTarget.offsetZ * 0.5,
                ),
            )
        }
        out.add(Vec3d(target.x + 0.5, target.y + 0.5, target.z + 0.5))
        out.add(Vec3d(target.x + 0.5, target.y + 0.05, target.z + 0.5))
        return out
    }

    /**
     * The cell vanilla [net.minecraft.item.BucketItem.use] would place its fluid
     * into for a given block [hit], mirroring `FluidModificationItem.placeFluid`:
     * the hit block itself if it can hold the fluid (replaceable, or a
     * waterloggable [FluidFillable]), otherwise the neighbour on the hit face.
     */
    private fun predictPlacement(world: World, hit: BlockHitResult, fluid: Fluid): BlockPos {
        val p = hit.blockPos
        val state = world.getBlockState(p)
        val block = state.block
        val canHoldAtHit = state.isReplaceable ||
            (block is FluidFillable && block.canFillWithFluid(null, world, p, state, fluid))
        return if (canHoldAtHit) p else p.offset(hit.side)
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
