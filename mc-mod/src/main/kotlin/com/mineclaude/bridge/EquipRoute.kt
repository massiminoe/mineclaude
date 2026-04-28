package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.EquipmentSlot
import net.minecraft.network.packet.c2s.play.PlayerActionC2SPacket
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Direction
import org.slf4j.LoggerFactory

/**
 * `POST /equip` — select a hotbar item, swap it to the offhand, or move
 * it into an armor slot.
 *
 * The `interactionManager.clickSlot` path makes this straightforward:
 * PlayerScreenHandler is always the player's `currentScreenHandler`
 * when no other GUI is open, so we click directly into PSH slots
 * without touching any UI state.
 *
 * Slot semantics (mirrors legacy):
 *   - "hand" / "mainhand" — select hotbar slot (move from inv if needed)
 *   - "offhand"           — select hotbar slot, then SWAP_ITEM_WITH_OFFHAND
 *   - "head"|"chest"|"legs"|"feet" — pickup-then-place into armor slot;
 *     verifies via `getEquippedStack` so a server reject (e.g. wrong
 *     item type for the slot) surfaces as a clear error.
 *
 * Post-equip verify (read mainHandStack / offHandStack / getEquippedStack
 * after a 2-tick settle and return an error if the item isn't actually
 * held) guards against server-side rejects unrelated to screen handlers
 * — wrong armor type for the slot, etc.
 *
 * # Why there's no longer a syncCloseScreen barrier
 *
 * Until Phase 4, /craft and /smelt ran on the legacy bridge and could
 * leave the *server* in a non-PlayerScreenHandler state for a few ticks
 * after closing client-side. A native /equip arriving in that window
 * fired clickSlot with a syncId the server didn't expect — the click
 * was silently dropped server-side, the local swap applied client-side,
 * and the next break swung bare-handed. The fix at the time was a
 * `closeHandledScreen()` + 80ms wait inside /equip. With /craft and
 * /smelt now native, no endpoint leaves a stale ScreenHandler open
 * across bridges, so the barrier is dead code and was removed.
 */
object EquipRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.equip")!!

    /** ms to wait before the post-equip verify. Must exceed two MC ticks (~100ms). */
    private const val POST_EQUIP_SETTLE_MS = 120L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/equip") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty()
        val slot = (body["slot"] as? String) ?: "hand"
        if (item.isEmpty()) {
            return HttpBridge.err("Missing 'item' parameter", status = 400)
        }
        val normalized = slot.lowercase()
        return when (normalized) {
            "hand", "mainhand" -> handleHand(item, slot)
            "offhand"          -> handleOffhand(item)
            in InventoryHelpers.ARMOR_PSH_SLOTS -> handleArmor(item, normalized)
            else               -> HttpBridge.err("Unknown equip slot: $slot")
        }
    }

    /**
     * "hand" / "mainhand" — guarantee [item] is held in the player's
     * mainhand after this call returns. Slow path moves it into the
     * hotbar via SWAP.
     */
    private fun handleHand(item: String, slot: String): BridgeResponse {
        val target = item.removePrefix("minecraft:")

        // Tick 1 — locate the item.
        val located = TickThread.submitAndWait(timeoutMs = 1_000) { locate(item) }
            ?: return HttpBridge.err("No $item in inventory")

        // Determine the hotbar slot the item will end up in.
        val hotbarSlot: Int = if (located.inHotbar) {
            located.piSlot
        } else {
            val staged = TickThread.submitAndWait(timeoutMs = 2_000) {
                stageIntoHotbar(item)
            } ?: return HttpBridge.err(
                if (TickThread.submitAndWait(timeoutMs = 500) { locate(item) != null })
                    "$item is in inventory but couldn't be moved to the hotbar"
                else
                    "No $item in inventory"
            )
            staged
        }

        // Select on a fresh tick so any pending SWAP packet ships first.
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            selectHotbar(player, hotbarSlot)
            Unit
        }

        // Verify post-equip. Long enough for any server-side resync to
        // land if the click was rejected; short enough not to bloat the
        // request.
        Thread.sleep(POST_EQUIP_SETTLE_MS)
        val verified = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            mainhandHolds(player, target)
        }
        if (!verified) {
            log.warn("equip: post-select mainhand does not hold {} (asked for slot {})", item, hotbarSlot)
            return HttpBridge.err(
                "equip did not take effect — mainhand is not $item after select."
            )
        }
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to $slot",
        )
    }

    private fun handleOffhand(item: String): BridgeResponse {
        val target = item.removePrefix("minecraft:")
        val located = TickThread.submitAndWait(timeoutMs = 1_000) { locate(item) }
            ?: return HttpBridge.err("No $item in inventory")
        val hotbarSlot: Int = if (located.inHotbar) {
            located.piSlot
        } else {
            TickThread.submitAndWait(timeoutMs = 2_000) { stageIntoHotbar(item) }
                ?: return HttpBridge.err("$item is in inventory but couldn't be moved to the hotbar")
        }
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            selectHotbar(player, hotbarSlot)
            // Swap-hands packet — server moves the held stack into the offhand.
            player.networkHandler.sendPacket(
                PlayerActionC2SPacket(
                    PlayerActionC2SPacket.Action.SWAP_ITEM_WITH_OFFHAND,
                    BlockPos.ORIGIN,
                    Direction.DOWN,
                )
            )
            Unit
        }
        Thread.sleep(POST_EQUIP_SETTLE_MS)
        val verified = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            val off = player.offHandStack
            !off.isEmpty && Registries.ITEM.getId(off.item).path == target
        }
        if (!verified) {
            log.warn("equip: post-swap offhand does not hold {} (asked for slot {})", item, hotbarSlot)
            return HttpBridge.err(
                "equip did not take effect — offhand is not $item after swap-hands."
            )
        }
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to offhand",
        )
    }

    /**
     * Move [item] into the named armor slot. Strategy:
     *   1. Find the item (errors if absent).
     *   2. PICKUP from source PSH slot — cursor holds the stack.
     *   3. PICKUP at the armor PSH slot — server validates the item is
     *      the right armor type and either accepts (cursor empties) or
     *      rejects (cursor still holds the stack).
     *   4. If cursor still has it, deposit back at source so we don't
     *      drop items on the ground when the screen handler closes.
     *   5. Verify via `getEquippedStack(slot)`: belt-and-suspenders for
     *      the rare server-quirk where the click "succeeded" client-side
     *      but server state diverged.
     */
    private fun handleArmor(item: String, armorSlot: String): BridgeResponse {
        val target = item.removePrefix("minecraft:")
        val armorPsh = InventoryHelpers.ARMOR_PSH_SLOTS[armorSlot]!!
        val equipmentSlot = ARMOR_EQUIPMENT_SLOTS[armorSlot]!!

        // Idempotency check on tick 1.
        val alreadyEquipped = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait false
            val current = player.getEquippedStack(equipmentSlot)
            !current.isEmpty && Registries.ITEM.getId(current.item).path == target
        }
        if (alreadyEquipped) {
            return HttpBridge.ok(
                mapOf("equipped" to true, "method" to "real", "noop" to true),
                "Equipped $item to $armorSlot",
            )
        }

        val tickResult = TickThread.submitAndWait(timeoutMs = 2_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait "no player"
            val screenErr = InventoryHelpers.ensurePlayerScreenOpen(player)
            if (screenErr != null) return@submitAndWait screenErr
            val found = InventoryHelpers.findItem(player, item)
                ?: return@submitAndWait "No $item in inventory"

            InventoryHelpers.click(player, found.pshSlot, 0, SlotActionType.PICKUP)
            InventoryHelpers.click(player, armorPsh, 0, SlotActionType.PICKUP)

            val handler = player.currentScreenHandler
            if (!handler.cursorStack.isEmpty) {
                InventoryHelpers.click(player, found.pshSlot, 0, SlotActionType.PICKUP)
                return@submitAndWait "armor equip rejected: $item is not valid for $armorSlot"
            }
            ""
        }
        if (tickResult.isNotEmpty()) {
            return HttpBridge.err(tickResult)
        }

        // Verify after settle.
        Thread.sleep(POST_EQUIP_SETTLE_MS)
        val verified = TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait null
            val after = player.getEquippedStack(equipmentSlot)
            if (after.isEmpty) "<empty>"
            else Registries.ITEM.getId(after.item).path
        }
        if (verified != target) {
            return HttpBridge.err(
                "armor equip did not stick (expected $target on $armorSlot, got ${verified ?: "<unknown>"})"
            )
        }
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to $armorSlot",
        )
    }

    /**
     * Tick-thread helper: locate [item] in the player's inventory.
     * Returns null if not present anywhere reachable.
     */
    private fun locate(item: String): InventoryHelpers.FoundStack? {
        val player = MinecraftClient.getInstance().player ?: return null
        return InventoryHelpers.findItem(player, item)
    }

    /**
     * Tick-thread helper: SWAP [item] from main inv into a hotbar staging
     * slot. Returns the staging slot (PI hotbar index) on success, null
     * if the item disappeared or the screen state still rejects the click.
     * Prefers an empty hotbar slot to avoid bouncing real items around.
     */
    private fun stageIntoHotbar(item: String): Int? {
        val player = MinecraftClient.getInstance().player ?: return null
        val refreshed = InventoryHelpers.findItem(player, item) ?: return null
        if (refreshed.inHotbar) return refreshed.piSlot
        InventoryHelpers.ensurePlayerScreenOpen(player)?.let { return null }
        val staging = InventoryHelpers.pickHotbarStagingSlot(player)
        InventoryHelpers.click(player, refreshed.pshSlot, staging, SlotActionType.SWAP)
        return staging
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

    /**
     * Map the legacy slot strings to MC's EquipmentSlot enum. Used for
     * post-equip verification via `player.getEquippedStack(slot)`.
     */
    private val ARMOR_EQUIPMENT_SLOTS = mapOf(
        "head" to EquipmentSlot.HEAD,
        "chest" to EquipmentSlot.CHEST,
        "legs" to EquipmentSlot.LEGS,
        "feet" to EquipmentSlot.FEET,
    )
}
