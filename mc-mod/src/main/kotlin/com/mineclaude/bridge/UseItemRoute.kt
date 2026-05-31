package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.Hand
import org.slf4j.LoggerFactory

/**
 * `POST /use_item {item, hold_ms?}` — right-click in air with [item] held
 * in the mainhand. The "right-click in air" pathway covers everything that
 * isn't a block interaction: eating food, drinking potions, throwing
 * snowballs / eggs / ender pearls, charging bows, casting fishing rods,
 * lighting fireworks.
 *
 * # Two phases
 *
 * 1. Equip [item] to mainhand. Locate → stage to hotbar if needed → select
 *    slot → verify mainhand holds it after a 120 ms settle. Same path as
 *    `/equip` "hand", inlined here so `/equip`'s BridgeResponse-shaped error
 *    handling doesn't leak in.
 * 2. Fire `interactionManager.interactItem(player, MAIN_HAND)` directly on
 *    the tick thread. `interactItem` ignores crosshair targets entirely
 *    (calls `stack.use(world, player, hand)` directly), so we never
 *    accidentally trigger interactBlock on whatever the player happens to
 *    be aimed at. No need to look up at the sky first.
 *
 * # Auto-detected hold duration
 *
 * The right hold time depends on the item, not the caller's intent. We
 * read it from the held stack via `getMaxUseTime(player)` after equipping:
 *
 *   - 0 ticks → instant-use (snowball, ender pearl, fishing rod, loaded
 *     crossbow). hold_ms = 0; interactItem fires once and we're done.
 *   - small (≤100 ticks) → consumable (food 32, dried kelp 16, honey 40,
 *     potion 32). hold_ms = (ticks+2) * 50, exact duration plus slack so
 *     MC's tick loop sees useKey pressed across the whole consume window.
 *   - large (>100 ticks) → chargeable (bow returns 72000, crossbow ~25,
 *     trident-riptide ~10, shield 72000, spyglass 1200). hold_ms capped
 *     at 1500ms — enough for max bow draw (20 ticks) and crossbow load,
 *     after which release fires/loads the action.
 *
 * Crossbow's two-phase load+fire flow Just Works: an unloaded crossbow
 * reports a small maxUseTime (charge ticks), so first call holds and
 * loads. A loaded crossbow reports 0, so the next call fires instantly.
 *
 * # Override
 *
 * Body may pass an explicit `hold_ms` to bypass auto-detect — useful for
 * an under-drawn bow shot or longer "eat until satiated" loop. Negative
 * values rejected; 0 is valid (forces instant fire).
 *
 * # Why we don't pre-aim
 *
 * Calling [interactionManager.interactItem] directly bypasses MC's
 * crosshair check (that's what `doItemUse` in MinecraftClient layers on
 * top). Once the use is initiated, the player's crosshair target is
 * irrelevant — the use animation continues until isUsingItem becomes
 * false. So we don't need to reposition the head, which would otherwise
 * leak side-effects into the agent's view.
 */
object UseItemRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.use_item")!!

    /** ms to wait before verifying mainhand holds the requested item. */
    private const val POST_EQUIP_SETTLE_MS = 120L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/use_item") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty()
        val explicitHoldMs = (body["hold_ms"] as? Number)?.toLong()
        if (item.isEmpty()) {
            return HttpBridge.err("Missing 'item' parameter", status = 400)
        }
        if (explicitHoldMs != null && explicitHoldMs < 0) {
            return HttpBridge.err("hold_ms must be >= 0", status = 400)
        }

        val equipErr = ensureMainhandHolds(item)
        if (equipErr != null) return HttpBridge.err(equipErr)

        // Auto-detect post-equip — caller's job is to name the item, ours is
        // to figure out how long it needs to be held. Override only when the
        // caller cares (under-drawn bow, eat-to-satiation loop).
        val holdMs = explicitHoldMs ?: detectHoldMs()

        return doUse(item, holdMs)
    }

    /**
     * Read [getMaxUseTime] off the now-held stack and translate to a hold
     * duration in milliseconds. See class kdoc for the rule table.
     *
     * Tick-thread submission because `getMaxUseTime` reads the stack +
     * player state, which MC requires accessed from the client thread.
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
     * Guarantee [item] is held in mainhand after this returns null. Mirrors
     * [EquipRoute.handleHand]'s flow — locate, stage to hotbar if needed,
     * select, settle, verify. Returns the error message on failure.
     */
    private fun ensureMainhandHolds(item: String): String? {
        val target = item.removePrefix("minecraft:")

        // Fast path: already held.
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
        if (!verified) {
            return "equip did not take effect — mainhand is not $item after select"
        }
        return null
    }

    /**
     * Fire interactItem and (if hold_ms>0) hold useKey for [holdMs] to
     * sustain the use action across ticks. Always release useKey in
     * `finally` so a thrown sleep doesn't leave it stuck pressed forever.
     */
    private fun doUse(item: String, holdMs: Long): BridgeResponse {
        val initiated = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait UseResult.Error("no player — not connected to a world")
            val mgr = mc.interactionManager ?: return@submitAndWait UseResult.Error("no interaction manager")

            // Defensive: a lingering screen would intercept input.
            WorldHelpers.ensureNoScreenOpen(player)

            val result = mgr.interactItem(player, Hand.MAIN_HAND)
            if (!result.isAccepted) {
                return@submitAndWait UseResult.Error(
                    "use_item rejected (ActionResult=${result.javaClass.simpleName}) — item may not be usable here " +
                        "(e.g. food requires hunger, ender pearl requires open sky)"
                )
            }
            // Only press useKey if we're going to hold — otherwise the press
            // would persist past this request and the next tick MC would
            // fire interactItem again on whatever's in hand.
            if (holdMs > 0) {
                mc.options.useKey.setPressed(true)
            }
            player.swingHand(Hand.MAIN_HAND)
            UseResult.Ok(result.javaClass.simpleName)
        }
        when (initiated) {
            is UseResult.Error -> return HttpBridge.err(initiated.message)
            is UseResult.Ok -> Unit
        }

        if (holdMs > 0) {
            try {
                Thread.sleep(holdMs)
            } finally {
                TickThread.submitAndWait(timeoutMs = 1_000) {
                    MinecraftClient.getInstance().options.useKey.setPressed(false)
                    Unit
                }
            }
        }

        log.info("use_item: used {} (hold_ms={}, result={})", item, holdMs, initiated.resultName)
        return HttpBridge.ok(
            mapOf("used" to true, "item" to item, "hold_ms" to holdMs, "result" to initiated.resultName),
            "Used $item",
        )
    }

    private sealed interface UseResult {
        data class Ok(val resultName: String) : UseResult
        data class Error(val message: String) : UseResult
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
}
