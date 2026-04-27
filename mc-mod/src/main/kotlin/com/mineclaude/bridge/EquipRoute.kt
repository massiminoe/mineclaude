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

/**
 * `POST /equip` — select a hotbar item, swap it to the offhand, or move
 * it into an armor slot.
 *
 * Phase 2b extends Phase 2's hotbar-only impl to cover armor and
 * non-hotbar items. The `interactionManager.clickSlot` path makes this
 * straightforward: the PlayerScreenHandler is always present as the
 * player's `currentScreenHandler` when no other GUI is open, so we click
 * directly into PSH slots without touching any UI state.
 *
 * Slot semantics (mirrors legacy):
 *   - "hand" / "mainhand" — select hotbar slot (move from inv if needed)
 *   - "offhand"           — select hotbar slot, then SWAP_ITEM_WITH_OFFHAND
 *   - "head"|"chest"|"legs"|"feet" — pickup-then-place into armor slot;
 *     verifies via `getEquippedStack` so a server reject (e.g. wrong
 *     item type for the slot) surfaces as a clear error.
 */
object EquipRoute {
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
        return TickThread.submitAndWait(timeoutMs = 3_000) { equipOnTick(item, slot) }
    }

    private fun equipOnTick(item: String, slot: String): BridgeResponse {
        val player = MinecraftClient.getInstance().player
            ?: return HttpBridge.err("no player — not connected to a world")

        val normalized = slot.lowercase()
        return when (normalized) {
            "hand", "mainhand" -> equipToHand(player, item, slot)
            "offhand"          -> equipToOffhand(player, item)
            in InventoryHelpers.ARMOR_PSH_SLOTS -> equipToArmor(player, item, normalized)
            else               -> HttpBridge.err("Unknown equip slot: $slot")
        }
    }

    private fun equipToHand(player: ClientPlayerEntity, item: String, slot: String): BridgeResponse {
        val hotbarSlot = ensureItemInHotbar(player, item)
            ?: return notInInventory(player, item)
        selectHotbar(player, hotbarSlot)
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to $slot",
        )
    }

    private fun equipToOffhand(player: ClientPlayerEntity, item: String): BridgeResponse {
        val hotbarSlot = ensureItemInHotbar(player, item)
            ?: return notInInventory(player, item)
        selectHotbar(player, hotbarSlot)
        // Swap-hands packet — server moves the held stack into the offhand.
        player.networkHandler.sendPacket(
            PlayerActionC2SPacket(
                PlayerActionC2SPacket.Action.SWAP_ITEM_WITH_OFFHAND,
                BlockPos.ORIGIN,
                Direction.DOWN,
            )
        )
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
    private fun equipToArmor(player: ClientPlayerEntity, item: String, armorSlot: String): BridgeResponse {
        InventoryHelpers.ensurePlayerScreenOpen(player)?.let {
            return HttpBridge.err(it)
        }
        val target = item.removePrefix("minecraft:")
        val armorPsh = InventoryHelpers.ARMOR_PSH_SLOTS[armorSlot]!!
        val equipmentSlot = ARMOR_EQUIPMENT_SLOTS[armorSlot]!!

        // If the requested item is *already* equipped, treat as success —
        // matches legacy idempotency.
        val current = player.getEquippedStack(equipmentSlot)
        if (!current.isEmpty && Registries.ITEM.getId(current.item).path == target) {
            return HttpBridge.ok(
                mapOf("equipped" to true, "method" to "real", "noop" to true),
                "Equipped $item to $armorSlot",
            )
        }

        val found = InventoryHelpers.findItem(player, item)
            ?: return HttpBridge.err("No $item in inventory")

        InventoryHelpers.click(player, found.pshSlot, 0, SlotActionType.PICKUP)
        InventoryHelpers.click(player, armorPsh, 0, SlotActionType.PICKUP)

        val handler = player.currentScreenHandler
        if (!handler.cursorStack.isEmpty) {
            // Server didn't accept the item on this armor slot. Put it back.
            InventoryHelpers.click(player, found.pshSlot, 0, SlotActionType.PICKUP)
            return HttpBridge.err(
                "armor equip rejected: $item is not valid for $armorSlot"
            )
        }

        // Verify the stack actually landed in the armor slot.
        val after = player.getEquippedStack(equipmentSlot)
        if (after.isEmpty || Registries.ITEM.getId(after.item).path != target) {
            return HttpBridge.err(
                "armor equip did not stick (expected $target on $armorSlot, got ${if (after.isEmpty) "<empty>" else Registries.ITEM.getId(after.item).path})"
            )
        }
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to $armorSlot",
        )
    }

    /**
     * Make sure [item] is in the hotbar after this call; return the
     * hotbar slot it's in, or null if the item doesn't exist anywhere
     * we can reach. If the item is in the main inventory we SWAP it
     * into a staging hotbar slot (preferring an empty one).
     */
    private fun ensureItemInHotbar(player: ClientPlayerEntity, item: String): Int? {
        val found = InventoryHelpers.findItem(player, item) ?: return null
        if (found.inHotbar) return found.piSlot

        InventoryHelpers.ensurePlayerScreenOpen(player)?.let {
            // Can't swap if the player has another screen up. Fail soft
            // back to "no hotbar slot" so the caller emits a clean error.
            return null
        }
        val staging = InventoryHelpers.pickHotbarStagingSlot(player)
        // SWAP: clickSlot(syncId, sourcePSH, hotbarButton, SWAP, player)
        // moves source ↔ hotbar[hotbarButton] (button is the PI hotbar
        // index 0..8, NOT a PSH slot). We don't reverse it here — for
        // /equip the agent intentionally wants the item on the action
        // bar afterward.
        InventoryHelpers.click(player, found.pshSlot, staging, SlotActionType.SWAP)
        return staging
    }

    private fun selectHotbar(player: ClientPlayerEntity, slot: Int) {
        player.inventory.setSelectedSlot(slot)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
    }

    private fun notInInventory(player: ClientPlayerEntity, item: String): BridgeResponse {
        return HttpBridge.err(
            if (InventoryHelpers.existsInInventory(player, item))
                // Should not happen now that we move into the hotbar — but
                // covers e.g. cursor-not-empty preconditions failing.
                "$item is in inventory but couldn't be moved to the hotbar"
            else
                "No $item in inventory"
        )
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
