package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory

/**
 * `POST /interact {x, y, z}` — right-click the existing block at the given
 * coordinates. Doors, trapdoors, fence gates, buttons, levers, pressure
 * plates' redstone sources, beds, jukeboxes, note blocks — anything where
 * the agent's intent is "press use on this block" rather than "place a
 * block here" (covered by /place) or "destroy this block" (covered by
 * /break).
 *
 * # Why this isn't a special case of /place
 *
 * /place clicks against an *adjacent* solid neighbour with a synthetic
 * BlockHitResult whose face points at the target air cell — that's the
 * shape of "place a new block". /interact clicks the target block itself
 * with a face pointing at the player — that's the shape of "use this
 * block". MC's `interactBlock` follows the BlockHitResult's `pos` to
 * decide which BlockState to call `onUse` on, so the two click shapes
 * route to entirely different behaviour.
 *
 * # Hand-held items
 *
 * The agent decides what to hold before calling /interact. Vanilla rules
 * apply: holding a block while clicking dirt → the block places (because
 * dirt's `onUse` returns PASS, falling through to item use). Holding a
 * sword while clicking a door → door opens (door consumes the click).
 * /interact doesn't enforce any policy here; if the agent wants a "pure
 * interact with empty hand", they call /equip "" first or hold a
 * non-placeable item.
 *
 * # Screen-opening blocks
 *
 * Chests, furnaces, crafting tables etc. *will* open their screen on a
 * right-click. Those have dedicated routes (`/chest/...`, `/furnace/...`, `/craft`)
 * which manage the screen lifecycle. If /interact is used on one anyway,
 * we close the resulting screen post-click — leaving it open would
 * silently break every subsequent world action (Screens capture input).
 * The response notes whether a screen was opened+closed so the caller can
 * switch to the dedicated route if that's what they wanted.
 */
object InteractRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.interact")!!

    /** Settle window so the screen-opening result lands before we check. */
    private const val POST_CLICK_SETTLE_MS = 150L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/interact") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val x = (body["x"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'x' parameter", status = 400)
        val y = (body["y"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'y' parameter", status = 400)
        val z = (body["z"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'z' parameter", status = 400)
        val target = BlockPos(x, y, z)

        // Reject air up front — interactBlock on air would just fall
        // through to item-use, which is what /use_item is for.
        val targetBlock = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().world ?: return@submitAndWait null
            WorldHelpers.blockIdAt(target)
        } ?: return HttpBridge.err("no world")
        if (targetBlock == "air" || targetBlock == "void_air" || targetBlock == "cave_air") {
            return HttpBridge.err("Nothing to interact with at ($x, $y, $z) — block is $targetBlock")
        }

        val nav = Navigation.navigateNear(target)
        if (nav is Navigation.Result.Failed) {
            return HttpBridge.err(
                "Couldn't reach ($x, $y, $z) to interact with $targetBlock: ${nav.reason}"
            )
        }

        val click = TickThread.submitAndWait(timeoutMs = 2_000) {
            clickOnTick(target)
        }
        when (click) {
            is ClickResult.Error -> return HttpBridge.err(click.message)
            is ClickResult.Ok -> Unit
        }

        // Settle, then close any screen the click opened. Without this a
        // chest-opening interact would jam every subsequent world action.
        Thread.sleep(POST_CLICK_SETTLE_MS)
        val screenClosed = TickThread.submitAndWait(timeoutMs = 1_000) {
            val mc = MinecraftClient.getInstance()
            val screen = mc.currentScreen
            if (screen != null) {
                log.warn(
                    "interact: closing unintended screen ({}) — use a dedicated route for screen-bearing blocks",
                    screen.javaClass.simpleName,
                )
                mc.player?.closeHandledScreen()
                mc.setScreen(null)
                screen.javaClass.simpleName
            } else null
        }

        log.info("interact: clicked {} at {} (result={})", targetBlock, target, click.resultName)
        val data = mutableMapOf<String, Any>(
            "interacted" to true,
            "target" to targetBlock,
            "result" to click.resultName,
        )
        if (screenClosed != null) {
            data["opened_screen"] = screenClosed
            return HttpBridge.partial(
                data,
                "Interacted with $targetBlock at ($x, $y, $z) — opened a $screenClosed screen and closed it; " +
                    "use a dedicated route (e.g. /chest/store, /furnace/inspect) if that was the goal",
            )
        }
        return HttpBridge.ok(data, "Interacted with $targetBlock at ($x, $y, $z)")
    }

    private sealed interface ClickResult {
        data class Ok(val resultName: String) : ClickResult
        data class Error(val message: String) : ClickResult
    }

    private fun clickOnTick(target: BlockPos): ClickResult {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return ClickResult.Error("no player — not connected to a world")
        val mgr = mc.interactionManager ?: return ClickResult.Error("no interaction manager")

        WorldHelpers.ensureNoScreenOpen(player)

        if (!WorldHelpers.isBlockWithinReach(player, target)) {
            return ClickResult.Error(
                "block at (${target.x},${target.y},${target.z}) is out of reach after navigation"
            )
        }

        WorldHelpers.lookAtBlock(player, target)

        // Face we're clicking is the one nearest the player — same dominant-
        // axis pick used by /break. The hit position is the centre of that
        // face, +0.5 along the face normal from the block centre.
        val face = WorldHelpers.playerFacingSide(player, target)
        val hitPos = Vec3d(
            target.x + 0.5 + face.offsetX * 0.5,
            target.y + 0.5 + face.offsetY * 0.5,
            target.z + 0.5 + face.offsetZ * 0.5,
        )
        val hit = BlockHitResult(hitPos, face, target, /*insideBlock=*/ false)

        val result = mgr.interactBlock(player, Hand.MAIN_HAND, hit)
        player.swingHand(Hand.MAIN_HAND)

        // PASS / FAIL means the block did nothing with the click. That's a
        // legitimate "interact attempted but nothing happened" — surface as
        // a soft error so the agent doesn't think it pressed a button that
        // wasn't there.
        if (!result.isAccepted) {
            return ClickResult.Error(
                "interactBlock returned ${result.javaClass.simpleName} — block at " +
                    "(${target.x},${target.y},${target.z}) didn't accept the click " +
                    "(not interactable, or held item conflicts)"
            )
        }
        return ClickResult.Ok(result.javaClass.simpleName)
    }
}
