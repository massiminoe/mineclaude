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
 * Three response shapes:
 *   - confirmed (`status:"success"`, `verified:true`) — getBlockState
 *     after the click reports the placed block.
 *   - tolerant (`status:"partial"`, `verified:false`) — getBlockState
 *     errored; placement may have succeeded but we couldn't confirm it.
 *   - error (`status:"error"`) — block already in cell (non-replaceable),
 *     not in inventory, no adjacent solid, click had no effect.
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
        // whole pre-condition snapshot is consistent.
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
            is PreflightResult.Ready -> {
                return doPlace(target, block, outcome.hotbarSlot)
            }
        }
    }

    private sealed interface PreflightResult {
        data class Error(val message: String) : PreflightResult
        data class Ready(val hotbarSlot: Int) : PreflightResult
        data class NeedNavigation(val hotbarSlot: Int) : PreflightResult
    }

    private fun preflightOnTick(target: BlockPos, block: String): PreflightResult {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return PreflightResult.Error("no player — not connected to a world")
        val world = mc.world ?: return PreflightResult.Error("no world")

        WorldHelpers.ensureNoScreenOpen(player)

        // Reject only if the cell holds a non-replaceable block — vanilla
        // silently overwrites grass overlays, flowers, snow layer, water.
        val current = WorldHelpers.blockIdAt(target)
        if (!WorldHelpers.isReplaceable(current)) {
            return PreflightResult.Error("Block already at ${target.x},${target.y},${target.z}: $current")
        }

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

        // Reach check is informational at this layer — the actual click
        // also requires reach, but Navigation handles the slow path.
        val inReach = WorldHelpers.isBlockWithinReach(player, target)
        return if (inReach) PreflightResult.Ready(hotbarSlot)
        else PreflightResult.NeedNavigation(hotbarSlot)
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
