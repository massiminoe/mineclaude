package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.screen.slot.SlotActionType

/**
 * `POST /discard {slot, count}` — drop items from a specific PI slot.
 *
 * Slot is the PlayerInventory index that `/status` already reports
 * (0..8 hotbar, 9..35 main). Caller is responsible for picking the right
 * slot — for damageable items (multiple pickaxes with different
 * `durability.remaining`) the bridge can't guess which one the agent
 * meant, so we make the agent name it explicitly.
 *
 * Hotbar slot: select + dropSelectedItem. Main inventory: SWAP into a
 * hotbar staging slot, drop, SWAP back. Both restore the originally-held
 * hotbar selection. Armor (PI 36..39) and offhand (PI 40) are not
 * supported here — unequip them via `/equip` first.
 */
object DiscardRoute {
    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/discard") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val slot = (body["slot"] as? Number)?.toInt()
            ?: return HttpBridge.err("Missing 'slot' parameter (PI index 0..35)", status = 400)
        val count = (body["count"] as? Number)?.toInt() ?: 1
        if (slot !in 0..35) {
            return HttpBridge.err(
                "slot $slot out of range — must be 0..8 (hotbar) or 9..35 (main inventory). " +
                    "Armor and offhand are not discardable; unequip first.",
                status = 400,
            )
        }
        if (count <= 0) {
            return HttpBridge.err("count must be >= 1", status = 400)
        }
        return TickThread.submitAndWait(timeoutMs = 3_000) { dropOnTick(slot, count) }
    }

    private fun dropOnTick(slot: Int, count: Int): BridgeResponse {
        val player = MinecraftClient.getInstance().player
            ?: return HttpBridge.err("no player — not connected to a world")

        val stack = player.inventory.getStack(slot)
        if (stack.isEmpty) {
            return HttpBridge.err("Slot $slot is empty")
        }
        val itemName = Registries.ITEM.getId(stack.item).path

        return if (slot < InventoryHelpers.HOTBAR_SIZE) {
            dropFromHotbar(player, itemName, count, hotbarSlot = slot)
        } else {
            // PI 9..35 maps 1:1 to PSH 9..35 for the SWAP.
            dropFromMainInventory(player, itemName, count, sourcePsh = slot)
        }
    }

    /**
     * Fast path: the item is already in the hotbar. Save the player's
     * original selection, switch to the target slot, drop, restore.
     */
    private fun dropFromHotbar(
        player: ClientPlayerEntity,
        item: String,
        count: Int,
        hotbarSlot: Int,
    ): BridgeResponse {
        val originalSelected = player.inventory.getSelectedSlot()
        select(player, hotbarSlot)
        val dropped = dropSelected(player, count)
        if (originalSelected != hotbarSlot) select(player, originalSelected)

        if (dropped == 0) return HttpBridge.err("Failed to drop any $item")
        return HttpBridge.ok(
            mapOf("discarded" to dropped, "item" to item, "method" to "real"),
            "Discarded $dropped $item",
        )
    }

    /**
     * Slow path: stage the main-inventory stack into a hotbar slot,
     * drop, then SWAP it back so the player's hotbar layout survives.
     * The staging slot is preferentially an empty one to avoid bouncing
     * a real item around.
     */
    private fun dropFromMainInventory(
        player: ClientPlayerEntity,
        item: String,
        count: Int,
        sourcePsh: Int,
    ): BridgeResponse {
        InventoryHelpers.ensurePlayerScreenOpen(player)?.let {
            return HttpBridge.err(it)
        }
        val originalSelected = player.inventory.getSelectedSlot()
        val staging = InventoryHelpers.pickHotbarStagingSlot(player)

        // Step 1: SWAP source ↔ hotbar[staging]. Item is now in hotbar.
        InventoryHelpers.click(player, sourcePsh, staging, SlotActionType.SWAP)

        select(player, staging)
        val dropped = dropSelected(player, count)

        // Step 3: SWAP back to restore both slots' contents.
        InventoryHelpers.click(player, sourcePsh, staging, SlotActionType.SWAP)

        if (originalSelected != staging) select(player, originalSelected)

        if (dropped == 0) return HttpBridge.err("Failed to drop any $item")
        return HttpBridge.ok(
            mapOf("discarded" to dropped, "item" to item, "method" to "real"),
            "Discarded $dropped $item",
        )
    }

    /**
     * Drop up to [count] items from the currently-selected hotbar slot
     * one at a time. Stops early if the stack runs out or the client
     * refuses (e.g. cooldown). Returns the actual number dropped.
     */
    private fun dropSelected(player: ClientPlayerEntity, count: Int): Int {
        val hotbarSlot = player.inventory.getSelectedSlot()
        var dropped = 0
        for (i in 0 until count) {
            val stack = player.inventory.getStack(hotbarSlot)
            if (stack.isEmpty) break
            if (player.dropSelectedItem(false)) dropped++ else break
        }
        return dropped
    }

    private fun select(player: ClientPlayerEntity, slot: Int) {
        player.inventory.setSelectedSlot(slot)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
    }
}
