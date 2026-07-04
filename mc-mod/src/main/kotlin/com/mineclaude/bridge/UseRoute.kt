package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.Leashable
import net.minecraft.entity.decoration.LeashKnotEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.registry.tag.BlockTags
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.hit.HitResult
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory

/**
 * `POST /use {item?, look_at_x/y/z?, hold_ms?}` — the unified right-click.
 *
 * This is the one honest primitive behind every "use an item" interaction:
 * eating, throwing, placing a torch on a wall, lighting flint & steel, filling
 * or pouring a bucket, opening a door. It mirrors what vanilla
 * `MinecraftClient.doItemUse()` does for a player pressing the use key:
 *
 *   1. (optional) equip [item] to the mainhand.
 *   2. (optional) aim the eye at `look_at` and **raycast for real** from the
 *      new rotation — the face/hit come from where you're actually looking,
 *      not a synthesized dominant-axis guess.
 *   3. If the ray hit a block, try `interactBlock`. If the block accepts it
 *      (door opens, torch/flint&steel/BlockItem places), we're done.
 *   4. Otherwise (PASS/FAIL, or no `look_at` at all) fall through to
 *      `interactItem` — `stack.use()`. This is what makes buckets work: an
 *      empty bucket PASSes on the block behind the water, then its own
 *      `BucketItem.use()` raycast (which DOES see fluids) fills it.
 *
 * Why this collapses the old surface: `/use_item` was step 4 with no aim;
 * `/interact` was step 3 with a *synthetic* face. Both lost the one degree of
 * freedom — where you look — that vanilla uses to make fluids, faces, and
 * item-use all fall out of a single action. They're now thin shims over
 * [performUse]. `/place` stays separate (its auto-anchor is a building
 * convenience, not a raw right-click).
 *
 * # Deviation from vanilla
 * Vanilla stops on `interactBlock` returning FAIL (won't fall through to item
 * use). We treat any not-accepted block result as "fall through", which is
 * simpler and harmless for every item we care about (a failed block-place's
 * `BlockItem.use()` is a no-op). We rely only on `ActionResult.isAccepted`.
 *
 * # Leash tie-off: the one click ActionResult can't see
 * Tying leashed mobs to a fence is invisible to client prediction:
 * `LeadItem.useOnBlock` on a fence returns PASS **unconditionally on the
 * client** (the attach logic is `!world.isClient`-gated), so a tie-off that
 * worked server-side reads as "nothing happened" here — a false negative
 * that made the agent retry a leash that was already tied. Since no
 * ActionResult can ever be right, we verify against world state instead
 * (the `/bucket/empty` pattern): before a fence click we snapshot which
 * mobs are leashed to the player, and if the dispatch comes back
 * not-accepted we poll the client-synced leash state — any of those mobs
 * now held by a [LeashKnotEntity] at the clicked pos means the tie-off
 * happened. Reported as `used: true` + `tied` + `leash_knot`.
 */
object UseRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.use")!!

    /** ms to wait before verifying mainhand holds the requested item. */
    private const val POST_EQUIP_SETTLE_MS = 120L

    /** Settle window so a screen-opening interaction lands before we check. */
    private const val POST_CLICK_SETTLE_MS = 150L

    /**
     * How long to poll for the server round-trip that proves a leash
     * tie-off (knot spawn + attach packets) after a client-PASS fence click.
     */
    private const val TIE_OFF_VERIFY_MS = 600L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/use") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String)?.takeIf { it.isNotEmpty() }
        val holdMs = (body["hold_ms"] as? Number)?.toLong()
        if (holdMs != null && holdMs < 0) {
            return HttpBridge.err("hold_ms must be >= 0", status = 400)
        }

        val anyLookKey = body.containsKey("look_at_x") || body.containsKey("look_at_y") ||
            body.containsKey("look_at_z")
        val lookAt = parseLookAt(body)
        if (anyLookKey && lookAt == null) {
            return HttpBridge.err("look_at requires all three of look_at_x, look_at_y, look_at_z", status = 400)
        }
        if (item == null && lookAt == null && !body.containsKey("item")) {
            return HttpBridge.err("nothing to do — pass `item` (to equip) and/or `look_at_*` (to aim)", status = 400)
        }

        return performUse(item, lookAt, holdMs).toResponse()
    }

    private fun parseLookAt(body: Map<String, Any?>): Vec3d? {
        val x = (body["look_at_x"] as? Number)?.toDouble()
        val y = (body["look_at_y"] as? Number)?.toDouble()
        val z = (body["look_at_z"] as? Number)?.toDouble()
        return if (x != null && y != null && z != null) Vec3d(x, y, z) else null
    }

    // -- public surface used by the /use_item and /interact shims --------------

    sealed interface Outcome {
        data class Err(val message: String, val status: Int = 200) : Outcome
        data class Ok(
            /** "block" (a block consumed the click) or "item" (fell through to use). */
            val dispatch: String,
            /** Did anything actually happen? Block-accept, item-accept, or a verified tie-off. */
            val accepted: Boolean,
            val item: String,
            val aimed: Vec3d?,
            val hit: HitInfo?,
            val holdMs: Long,
            val openedScreen: String?,
            val invDelta: Map<String, Int>,
            /** Mob ids verified re-leashed to a knot at the clicked fence (world-state check). */
            val tied: List<Int> = emptyList(),
        ) : Outcome
    }

    data class HitInfo(val block: String, val pos: BlockPos, val face: String)

    /**
     * Equip → (navigate) → aim → raycast → interactBlock-then-interactItem.
     * The single code path behind /use, /use_item, and /interact.
     */
    fun performUse(item: String?, lookAt: Vec3d?, holdMsOverride: Long?): Outcome {
        if (item != null) {
            val equipErr = ensureMainhandHolds(item)
            if (equipErr != null) return Outcome.Err(equipErr)
        }

        // Empty hand is legitimate for block interactions (open a door with
        // nothing held). Only the item-use fall-through no-ops on empty hand.
        val heldName = currentHeldName() ?: "empty_hand"

        // Navigate within reach if we're aiming at something out of range.
        if (lookAt != null) {
            val targetBlock = BlockPos.ofFloored(lookAt.x, lookAt.y, lookAt.z)
            val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
                val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
                WorldHelpers.isBlockWithinReach(p, targetBlock)
            }
            if (!inReach) {
                val nav = Navigation.navigateNear(targetBlock)
                if (nav is Navigation.Result.Failed) {
                    return Outcome.Err(
                        "couldn't reach (${targetBlock.x}, ${targetBlock.y}, ${targetBlock.z}) " +
                            "to use $heldName: ${nav.reason}"
                    )
                }
            }
        }

        val before = snapshotCounts()
        val holdMs = holdMsOverride ?: detectHoldMs()

        val dispatch = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Dispatch.Error("no player — not connected to a world")
            val world = mc.world ?: return@submitAndWait Dispatch.Error("no world")
            val mgr = mc.interactionManager ?: return@submitAndWait Dispatch.Error("no interaction manager")
            WorldHelpers.ensureNoScreenOpen(player)

            // Aim and raycast for real — the hit (and its face) come from the
            // actual look ray, exactly like a player's crosshair.
            var hit: BlockHitResult? = null
            if (lookAt != null) {
                WorldHelpers.lookAtPosition(player, lookAt.x, lookAt.y, lookAt.z)
                val hr = player.raycast(WorldHelpers.BLOCK_REACH, 1.0f, /*includeFluids=*/ false)
                if (hr.type == HitResult.Type.BLOCK) hit = hr as BlockHitResult
            }

            // Leash tie-off pre-state: clicking a fence ties any mobs leashed
            // to the player, but the client-side ActionResult is always PASS
            // for that path — snapshot who's leashed now (same tick as the
            // click) so the post-click verification can prove the tie-off.
            val preLeashed: List<Int> = if (hit != null &&
                world.getBlockState(hit.blockPos).isIn(BlockTags.FENCES)
            ) {
                world.entities.mapNotNull { e ->
                    if (e is Leashable && e.leashHolder?.id == player.id) e.id else null
                }
            } else emptyList()

            // Block-first dispatch (vanilla order).
            if (hit != null) {
                val ar = mgr.interactBlock(player, Hand.MAIN_HAND, hit)
                if (ar.isAccepted) {
                    player.swingHand(Hand.MAIN_HAND)
                    return@submitAndWait Dispatch.Block(hit)
                }
                // not accepted (PASS/FAIL) → fall through to item use
            }

            // Item use (stack.use()). Press the use key only if we're going to
            // hold — otherwise a lingering press would re-fire next tick.
            val ar = mgr.interactItem(player, Hand.MAIN_HAND)
            val accepted = ar.isAccepted
            val holdNow = accepted && holdMs > 0
            if (holdNow) mc.options.useKey.setPressed(true)
            player.swingHand(Hand.MAIN_HAND)
            Dispatch.Item(hit, accepted, holdNow, preLeashed)
        }

        if (dispatch is Dispatch.Error) return Outcome.Err(dispatch.message)

        // Sustain a held use (food / charging bow) across ticks.
        if (dispatch is Dispatch.Item && dispatch.holdNow) {
            try {
                var remaining = holdMs
                while (remaining > 0) {
                    val chunk = minOf(remaining, 100L)
                    Thread.sleep(chunk)
                    CameraDirector.noteFunctionalAim()
                    remaining -= chunk
                }
            } finally {
                TickThread.submitAndWait(timeoutMs = 1_000) {
                    MinecraftClient.getInstance().options.useKey.setPressed(false)
                    Unit
                }
            }
        }

        // Close any screen the interaction opened (chest/furnace via a stray
        // /use) so it can't jam subsequent world actions — same self-heal as
        // the old /interact.
        Thread.sleep(POST_CLICK_SETTLE_MS)
        val openedScreen = closeAnyScreen()

        // Leash tie-off ground truth: a fence click that came back
        // not-accepted may still have tied the player's leashed mobs (the
        // client ActionResult is blind to it — see the class kdoc). Poll the
        // client-synced leash state briefly: any pre-click mob now held by a
        // knot at the clicked pos proves the tie-off.
        var tied: List<Int> = emptyList()
        if (dispatch is Dispatch.Item && !dispatch.accepted &&
            dispatch.preLeashed.isNotEmpty() && dispatch.hit != null
        ) {
            val fencePos = dispatch.hit.blockPos
            val deadline = System.currentTimeMillis() + TIE_OFF_VERIFY_MS
            while (true) {
                tied = checkTieOff(fencePos, dispatch.preLeashed)
                if (tied.isNotEmpty() || System.currentTimeMillis() >= deadline) break
                Thread.sleep(100)
            }
        }

        val after = snapshotCounts()
        val invDelta = computeDelta(before, after)

        return when (dispatch) {
            is Dispatch.Block -> Outcome.Ok(
                dispatch = "block", accepted = true, item = heldName, aimed = lookAt,
                hit = hitInfo(dispatch.hit), holdMs = 0, openedScreen = openedScreen, invDelta = invDelta,
            )
            is Dispatch.Item -> Outcome.Ok(
                dispatch = "item", accepted = dispatch.accepted || tied.isNotEmpty(),
                item = heldName, aimed = lookAt,
                hit = dispatch.hit?.let { hitInfo(it) },
                holdMs = if (dispatch.holdNow) holdMs else 0,
                openedScreen = openedScreen, invDelta = invDelta, tied = tied,
            )
            is Dispatch.Error -> Outcome.Err(dispatch.message) // unreachable; satisfies exhaustiveness
        }
    }

    /**
     * Tick-thread read of the subset of [preLeashed] mobs whose leash holder
     * is now a [LeashKnotEntity] attached at [fencePos]. Keying on the mobs
     * (not mere knot existence) keeps the second-mob case honest — the knot
     * may have pre-existed from an earlier tie-off.
     */
    private fun checkTieOff(fencePos: BlockPos, preLeashed: List<Int>): List<Int> =
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val world = MinecraftClient.getInstance().world ?: return@submitAndWait emptyList()
            preLeashed.filter { id ->
                val holder = (world.getEntityById(id) as? Leashable)?.leashHolder
                holder is LeashKnotEntity && holder.attachedBlockPos == fencePos
            }
        }

    private sealed interface Dispatch {
        data class Error(val message: String) : Dispatch
        data class Block(val hit: BlockHitResult) : Dispatch
        data class Item(
            val hit: BlockHitResult?,
            val accepted: Boolean,
            val holdNow: Boolean,
            /** Ids of mobs leashed to the player pre-click, when the hit was a fence. */
            val preLeashed: List<Int> = emptyList(),
        ) : Dispatch
    }

    private fun hitInfo(hit: BlockHitResult): HitInfo =
        HitInfo(WorldHelpers.blockIdAt(hit.blockPos), hit.blockPos, hit.side.asString())

    private fun Outcome.toResponse(): BridgeResponse = when (this) {
        is Outcome.Err -> HttpBridge.err(message, status)
        is Outcome.Ok -> {
            val data = mutableMapOf<String, Any>(
                "used" to accepted,
                "dispatch" to dispatch,
                "item" to item,
                "hold_ms" to holdMs,
            )
            aimed?.let { data["aimed"] = listOf(it.x, it.y, it.z) }
            hit?.let {
                data["hit"] = mapOf(
                    "block" to it.block, "x" to it.pos.x, "y" to it.pos.y, "z" to it.pos.z, "face" to it.face,
                )
            }
            if (invDelta.isNotEmpty()) data["inventory_delta"] = invDelta
            if (tied.isNotEmpty()) {
                data["tied"] = tied
                hit?.let { data["leash_knot"] = mapOf("x" to it.pos.x, "y" to it.pos.y, "z" to it.pos.z) }
            }
            val deltaStr = if (invDelta.isNotEmpty()) {
                " (" + invDelta.entries.joinToString(", ") { "${if (it.value > 0) "+" else ""}${it.value} ${it.key}" } + ")"
            } else ""
            when {
                openedScreen != null -> {
                    data["opened_screen"] = openedScreen
                    HttpBridge.partial(
                        data,
                        "Used $item — opened a $openedScreen screen and closed it; use a dedicated route " +
                            "(/chest/*, /furnace/*, /craft) if that was the goal",
                    )
                }
                tied.isNotEmpty() -> HttpBridge.ok(
                    data,
                    "Tied ${tied.size} leashed mob(s) (ids ${tied.joinToString(", ")}) to the " +
                        "${hit?.block ?: "fence"} at (${hit?.pos?.x}, ${hit?.pos?.y}, ${hit?.pos?.z})$deltaStr",
                )
                dispatch == "block" -> HttpBridge.ok(data, "Used $item on ${hit?.block ?: "block"}$deltaStr")
                accepted -> HttpBridge.ok(data, "Used $item$deltaStr")
                else -> HttpBridge.ok(
                    data,
                    "Used $item but nothing happened — aim missed, or $item isn't usable here",
                )
            }
        }
    }

    // -- equip / hold helpers (shared; previously private to UseItemRoute) -----

    private fun currentHeldName(): String? = TickThread.submitAndWait(timeoutMs = 1_000) {
        val p = MinecraftClient.getInstance().player ?: return@submitAndWait null
        val s = p.mainHandStack
        if (s.isEmpty) null else Registries.ITEM.getId(s.item).path
    }

    /**
     * Read [getMaxUseTime] off the held stack → hold duration in ms.
     *   0 → instant (snowball, ender pearl, loaded crossbow)
     *   ≤100 ticks → consumable: (ticks+2)*50 (food 32, kelp 16, potion 32)
     *   >100 ticks → chargeable: capped at 1500ms (bow/crossbow/shield)
     */
    private fun detectHoldMs(): Long {
        return TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait 0L
            val stack = player.mainHandStack
            if (stack.isEmpty) return@submitAndWait 0L
            val ticks = stack.getMaxUseTime(player)
            when {
                ticks <= 0 -> 0L
                ticks <= 100 -> (ticks + 2L) * 50L
                else -> 1_500L
            }
        }
    }

    /**
     * Guarantee [item] is held in mainhand after this returns null. Locate →
     * stage to hotbar if needed → select → settle → verify. Returns an error
     * message on failure.
     */
    fun ensureMainhandHolds(item: String): String? {
        val target = item.removePrefix("minecraft:")

        val alreadyHeld = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            mainhandHolds(player, target)
        }
        if (alreadyHeld) return null

        val located = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait null
            InventoryHelpers.findItem(player, item)
        } ?: return "No $item in inventory"

        val hotbarSlot: Int = if (located.inHotbar) {
            located.piSlot
        } else {
            TickThread.submitAndWait(timeoutMs = 2_000) {
                val player = MinecraftClient.getInstance().player ?: return@submitAndWait null
                val refreshed = InventoryHelpers.findItem(player, item) ?: return@submitAndWait null
                if (refreshed.inHotbar) return@submitAndWait refreshed.piSlot
                InventoryHelpers.ensurePlayerScreenOpen(player)?.let { return@submitAndWait null }
                val staging = InventoryHelpers.pickHotbarStagingSlot(player)
                InventoryHelpers.click(player, refreshed.pshSlot, staging, SlotActionType.SWAP)
                staging
            } ?: return "$item is in inventory but couldn't be moved to the hotbar"
        }

        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            selectHotbar(player, hotbarSlot)
            Unit
        }

        Thread.sleep(POST_EQUIP_SETTLE_MS)

        val verified = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            mainhandHolds(player, target)
        }
        if (!verified) return "equip did not take effect — mainhand is not $item after select"
        return null
    }

    private fun mainhandHolds(player: ClientPlayerEntity, target: String): Boolean {
        val stack = player.mainHandStack
        if (stack.isEmpty) return false
        return Registries.ITEM.getId(stack.item).path == target
    }

    private fun selectHotbar(player: ClientPlayerEntity, slot: Int) {
        player.inventory.setSelectedSlot(slot)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
    }

    // -- inventory diff + screen close (shared with /use_on_entity) ------------

    internal fun snapshotCounts(): Map<String, Int> = TickThread.submitAndWait(timeoutMs = 1_000) {
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

    internal fun computeDelta(start: Map<String, Int>, end: Map<String, Int>): Map<String, Int> {
        val out = LinkedHashMap<String, Int>()
        for (name in (start.keys + end.keys)) {
            val d = (end[name] ?: 0) - (start[name] ?: 0)
            if (d != 0) out[name] = d
        }
        return out
    }

    internal fun closeAnyScreen(): String? = TickThread.submitAndWait(timeoutMs = 1_000) {
        val mc = MinecraftClient.getInstance()
        val screen = mc.currentScreen ?: return@submitAndWait null
        val name = screen.javaClass.simpleName
        log.warn("use: closing unintended screen ({}) — use a dedicated route for screen-bearing blocks", name)
        mc.player?.closeHandledScreen()
        mc.setScreen(null)
        name
    }
}
