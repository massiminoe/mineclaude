package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /place` — place a block at world coordinates via real player
 * actions only. The native impl uses
 * [`ClientPlayerInteractionManager.interactBlock`] with a synthetic
 * [BlockHitResult] aimed at an adjacent solid neighbour, which is exactly
 * the path MC's own `doItemUse()` takes when the player right-clicks.
 *
 * # Body-in-cell recovery
 *
 * Vanilla rejects placements that would put a solid block where any entity
 * is, returning a no-op to the client; the verify step then sees "still
 * air" and would surface a misleading error. Preflight detects this and
 * either steps the player one block laterally (cardinal neighbour with
 * solid floor + head clearance) or jump-places (presses jump and fires
 * the click while the player's feet are above the target — i.e., a
 * pillar-up). Step-off is preferred because it leaves the player at the
 * same y; jump-place is the fallback when no lateral exit exists.
 *
 * Three response shapes:
 *   - confirmed (`status:"success"`, `verified:true`) — getBlockState
 *     after the click reports the placed block.
 *   - tolerant (`status:"partial"`, `verified:false`) — getBlockState
 *     errored; placement may have succeeded but we couldn't confirm it.
 *   - error (`status:"error"`) — block already in cell (non-replaceable),
 *     not in inventory, no adjacent solid, click had no effect, or body
 *     intersection with no step-off and no pillar-up clearance.
 */
object PlaceRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.place")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/place") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val block = (body["block"] as? String).orEmpty()
        val x = (body["x"] as? Number)?.toInt() ?: 0
        val y = (body["y"] as? Number)?.toInt() ?: 0
        val z = (body["z"] as? Number)?.toInt() ?: 0
        if (block.isEmpty()) {
            return HttpBridge.err("Missing 'block' parameter", status = 400)
        }

        val target = BlockPos(x, y, z)

        // Step 1: tick-thread preflight — close screen, check target cell,
        // find item, find adjacent. Bundled into one tick submission so the
        // whole pre-condition snapshot is consistent. Also handles body-
        // in-cell: step-off teleports the player and falls through to
        // Ready; jump-place returns NeedsPillarUp for the multi-tick path.
        val preflight = TickThread.submitAndWait(timeoutMs = 2_000) {
            preflightOnTick(target, block)
        }
        when (val outcome = preflight) {
            is PreflightResult.Error -> return HttpBridge.err(outcome.message)
            is PreflightResult.NeedNavigation -> {
                val nav = Navigation.navigateNear(target)
                if (nav is Navigation.Result.Failed) {
                    return HttpBridge.err(
                        "Couldn't reach (${target.x}, ${target.y}, ${target.z}) to place $block: ${nav.reason}",
                    )
                }
                return doPlace(target, block, outcome.hotbarSlot)
            }
            is PreflightResult.NeedsStepOff -> {
                if (!walkOffTarget(target, outcome.destFeet)) {
                    return HttpBridge.err(
                        "Couldn't step off (${target.x},${target.y},${target.z}): " +
                            "still intersecting after walk attempt",
                    )
                }
                return doPlace(target, block, outcome.hotbarSlot)
            }
            is PreflightResult.NeedsPillarUp -> {
                return doPillarUpPlace(target, block, outcome.hotbarSlot)
            }
            is PreflightResult.Ready -> {
                return doPlace(target, block, outcome.hotbarSlot)
            }
        }
    }

    private sealed interface PreflightResult {
        data class Error(val message: String) : PreflightResult
        data class Ready(val hotbarSlot: Int) : PreflightResult
        data class NeedNavigation(val hotbarSlot: Int) : PreflightResult
        data class NeedsStepOff(val hotbarSlot: Int, val destFeet: BlockPos) : PreflightResult
        data class NeedsPillarUp(val hotbarSlot: Int) : PreflightResult
    }

    private fun preflightOnTick(target: BlockPos, block: String): PreflightResult {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return PreflightResult.Error("no player — not connected to a world")
        val world = mc.world ?: return PreflightResult.Error("no world")

        WorldHelpers.ensureNoScreenOpen(player)

        // Reject only if the cell holds a non-replaceable block — vanilla
        // silently overwrites grass overlays, flowers, snow layer, water.
        if (!WorldHelpers.isReplaceable(target)) {
            val current = WorldHelpers.blockIdAt(target)
            return PreflightResult.Error("Block already at ${target.x},${target.y},${target.z}: $current")
        }

        // Body-in-cell check: server-side BlockItem.canPlace rejects placements
        // that overlap an entity, returning a no-op the verify step can't
        // distinguish from line-of-sight failures. Decide a recovery — actual
        // movement happens off-tick after preflight returns. `bodyRecovery`
        // captures which path to take; null means no intersection.
        val bodyRecovery: BodyRecovery? = if (WorldHelpers.playerOccupiesCell(player, target)) {
            val step = WorldHelpers.findStepOffCell(player, target)
            when {
                step != null -> BodyRecovery.StepOff(step)
                canPillarUp(target) -> BodyRecovery.PillarUp
                else -> return PreflightResult.Error(
                    "Player intersects (${target.x},${target.y},${target.z}) with no lateral " +
                        "step-off and no pillar-up clearance; break a neighbour or move first",
                )
            }
        } else null

        // Need item on the hotbar so right-click uses the right stack.
        val found = InventoryHelpers.findItem(player, block)
            ?: return PreflightResult.Error("No $block in inventory")

        val hotbarSlot = if (found.inHotbar) {
            found.piSlot
        } else {
            // Move from main inv into a hotbar staging slot.
            val screenErr = InventoryHelpers.ensurePlayerScreenOpen(player)
            if (screenErr != null) return PreflightResult.Error(screenErr)
            val staging = InventoryHelpers.pickHotbarStagingSlot(player)
            InventoryHelpers.click(
                player, found.pshSlot, staging, net.minecraft.screen.slot.SlotActionType.SWAP,
            )
            staging
        }

        // Select on the hotbar so the player's held item matches the request.
        selectHotbar(player, hotbarSlot)

        // Body-in-cell paths skip the reach check — the click happens after
        // movement (step-off walks 1 block, pillar-up jumps), and each path
        // owns its own positional precondition.
        when (bodyRecovery) {
            is BodyRecovery.StepOff -> return PreflightResult.NeedsStepOff(hotbarSlot, bodyRecovery.destFeet)
            BodyRecovery.PillarUp -> return PreflightResult.NeedsPillarUp(hotbarSlot)
            null -> Unit
        }

        // Reach check is informational at this layer — the actual click
        // also requires reach, but Navigation handles the slow path.
        val inReach = WorldHelpers.isBlockWithinReach(player, target)
        return if (inReach) PreflightResult.Ready(hotbarSlot)
        else PreflightResult.NeedNavigation(hotbarSlot)
    }

    private sealed interface BodyRecovery {
        data class StepOff(val destFeet: BlockPos) : BodyRecovery
        data object PillarUp : BodyRecovery
    }

    /**
     * Pillar-up viability: floor below [target] is non-replaceable (will
     * support the player), and there's two blocks of head clearance at
     * [target.y]+1 and [target.y]+2 (player is 1.8 tall, jump apex puts
     * head at ~target.y+3). When this is false the body-in-cell handler
     * gives up rather than holding jump against a ceiling.
     */
    private fun canPillarUp(target: BlockPos): Boolean {
        val world = MinecraftClient.getInstance().world ?: return false
        val below = world.getBlockState(target.down())
        if (below.isReplaceable) return false
        val head1 = world.getBlockState(target.up())
        val head2 = world.getBlockState(target.up(2))
        return head1.isReplaceable && head2.isReplaceable
    }

    private fun doPlace(target: BlockPos, block: String, hotbarSlot: Int): BridgeResponse {
        val placeResult = TickThread.submitAndWait(timeoutMs = 3_000) {
            placeOnTick(target, hotbarSlot)
        }
        when (placeResult) {
            is PlaceTickResult.Error -> return HttpBridge.err(placeResult.message)
            is PlaceTickResult.Clicked -> Unit
        }

        // Settle a frame, then verify via getBlockState. Three outcomes:
        //   - getBlockState raises  → tolerant success (verified=false)
        //   - non-air                → confirmed success (verified=true)
        //   - still air              → click did nothing → error
        Thread.sleep(150)
        val verify = TickThread.submitAndWait(timeoutMs = 1_000) {
            try {
                WorldHelpers.blockIdAt(target)
            } catch (t: Throwable) {
                "__verify_error__:${t.message}"
            }
        }
        if (verify.startsWith("__verify_error__:")) {
            log.info("place: placed {} at {} (real, verify errored)", block, target)
            return HttpBridge.partial(
                mapOf(
                    "placed" to true,
                    "method" to "real",
                    "verified" to false,
                    "verify_error" to verify.removePrefix("__verify_error__:"),
                ),
                "Placed $block at ${target.x}, ${target.y}, ${target.z} (unverified)",
            )
        }
        if (verify != "air" && "air" !in verify) {
            log.info("place: placed {} at {} (real)", block, target)
            return HttpBridge.ok(
                mapOf("placed" to true, "method" to "real", "verified" to true),
                "Placed $block at ${target.x}, ${target.y}, ${target.z}",
            )
        }
        log.warn("place: interactBlock did not place {} at {} (still air)", block, target)
        return HttpBridge.err(
            "press_use did not place block (target still air); is a GUI open?"
        )
    }

    /**
     * Walk one block off the target cell using real movement keys. Aims at
     * [destFeet] and holds forward until the player's bounding box no
     * longer intersects [target] (or the deadline fires). Releases keys
     * in `finally` so a stalled walk doesn't leave forward stuck.
     *
     * Walking speed is ~4.3 b/s on ground, so a 1-block step takes ~230
     * ms. The 1.2 s deadline gives headroom for the player to overcome
     * static friction at the start of the move.
     */
    private fun walkOffTarget(target: BlockPos, destFeet: BlockPos): Boolean {
        val mc = MinecraftClient.getInstance()
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = mc.player ?: return@submitAndWait Unit
            // Face the destination cell so forward-key motion heads that way.
            WorldHelpers.lookAtPosition(
                player,
                destFeet.x + 0.5, destFeet.y + 0.5, destFeet.z + 0.5,
            )
            mc.options.forwardKey.setPressed(true)
            Unit
        }
        try {
            val deadline = System.currentTimeMillis() + 1_200L
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(50)
                val cleared = TickThread.submitAndWait(timeoutMs = 500) {
                    val p = mc.player ?: return@submitAndWait null
                    !WorldHelpers.playerOccupiesCell(p, target)
                }
                if (cleared == true) {
                    log.info("place: walked off ({},{},{}) → feet ({},{},{})",
                        target.x, target.y, target.z, destFeet.x, destFeet.y, destFeet.z)
                    return true
                }
            }
            log.warn("place: walk-off ({},{},{}) didn't clear before deadline",
                target.x, target.y, target.z)
            return false
        } finally {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                mc.options.forwardKey.setPressed(false)
                Unit
            }
        }
    }

    /**
     * Press jump until the player has cleared the target cell vertically,
     * then dispatch the normal place (which finds the floor below as
     * adjacent and clicks UP face — i.e., a pillar-up). Releases jump in
     * `finally` so a thrown failure doesn't leave it held.
     *
     * Apex condition: `player.y >= target.y + 1.0` (feet above target's
     * top face). Vanilla jump puts feet ~1.25 above the starting cell,
     * so 1.0 is the conservative threshold and arrives ~5 ticks in.
     */
    private fun doPillarUpPlace(target: BlockPos, block: String, hotbarSlot: Int): BridgeResponse {
        val mc = MinecraftClient.getInstance()
        TickThread.submitAndWait(timeoutMs = 1_000) {
            mc.options.jumpKey.setPressed(true)
            Unit
        }
        try {
            val deadline = System.currentTimeMillis() + 800L
            var aboveTarget = false
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(50)
                val cleared = TickThread.submitAndWait(timeoutMs = 500) {
                    val p = mc.player ?: return@submitAndWait null
                    p.y >= target.y + 1.0
                }
                if (cleared == true) { aboveTarget = true; break }
            }
            if (!aboveTarget) {
                log.warn("place: pillar-up never cleared target.y at ({},{},{})", target.x, target.y, target.z)
                return HttpBridge.err(
                    "Couldn't pillar-up to (${target.x},${target.y},${target.z}): jump didn't clear the target cell — ceiling may be too low",
                )
            }
            return doPlace(target, block, hotbarSlot)
        } finally {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                mc.options.jumpKey.setPressed(false)
                Unit
            }
        }
    }

    private sealed interface PlaceTickResult {
        data class Error(val message: String) : PlaceTickResult
        data object Clicked : PlaceTickResult
    }

    private fun placeOnTick(target: BlockPos, hotbarSlot: Int): PlaceTickResult {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return PlaceTickResult.Error("no player")
        val mgr = mc.interactionManager ?: return PlaceTickResult.Error("no interaction manager")

        val adjacent = WorldHelpers.findAdjacentSolidBlock(target)
            ?: return PlaceTickResult.Error("No adjacent solid block to place against")

        // Aim at the adjacent block (the one we're clicking against). MC's
        // own placement code keys off the eye-ray hit, but interactBlock
        // will accept any in-reach BlockHitResult we hand it directly.
        WorldHelpers.lookAtBlock(player, adjacent.pos)

        // Re-affirm hotbar selection on the tick we click — guards against
        // the rare case where another tick handler swapped us between
        // preflight and now.
        if (player.inventory.getSelectedSlot() != hotbarSlot) {
            selectHotbar(player, hotbarSlot)
        }

        // Hit position: centre of the clicked face. Side is the face of the
        // adjacent block that points at the target cell — passed as
        // `adjacent.face` (e.g. UP for floor placement).
        val face = adjacent.face
        val hitPos = net.minecraft.util.math.Vec3d(
            adjacent.pos.x + 0.5 + face.offsetX * 0.5,
            adjacent.pos.y + 0.5 + face.offsetY * 0.5,
            adjacent.pos.z + 0.5 + face.offsetZ * 0.5,
        )
        val hit = BlockHitResult(hitPos, face, adjacent.pos, /*insideBlock=*/ false)

        val result = mgr.interactBlock(player, Hand.MAIN_HAND, hit)
        // ActionResult.isAccepted indicates the server accepted the use.
        // We don't gate on it — the verify step is authoritative.
        @Suppress("UNUSED_VARIABLE")
        val _accepted = result
        player.swingHand(Hand.MAIN_HAND)
        return PlaceTickResult.Clicked
    }

    private fun selectHotbar(player: ClientPlayerEntity, slot: Int) {
        player.inventory.setSelectedSlot(slot)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
    }
}
