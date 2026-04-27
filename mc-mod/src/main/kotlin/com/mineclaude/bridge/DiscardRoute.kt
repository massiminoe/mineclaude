package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.screen.slot.SlotActionType

/**
 * `POST /discard` — drop items from anywhere in the player's main
 * inventory.
 *
 * Phase 2b extends Phase 2's hotbar-only impl: when the requested item
 * lives in the main inventory (PSH slots 9..35) we SWAP it into a
 * staging hotbar slot, drop, then SWAP it back. The cycle is lossless
 * for the staging slot's prior contents — the original hotbar layout
 * is restored regardless of whether the staging slot was empty or held
 * a different item — and the originally-selected hotbar slot is also
 * restored at the end.
 */
object DiscardRoute {
    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/discard") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty()
        val count = (body["count"] as? Number)?.toInt() ?: 1
        if (item.isEmpty()) {
            return HttpBridge.err("Missing 'item' parameter", status = 400)
        }
        if (count <= 0) {
            return HttpBridge.err("count must be >= 1", status = 400)
        }
        return TickThread.submitAndWait(timeoutMs = 3_000) { dropOnTick(item, count) }
    }

    private fun dropOnTick(item: String, count: Int): BridgeResponse {
        val player = MinecraftClient.getInstance().player
            ?: return HttpBridge.err("no player — not connected to a world")

        val found = InventoryHelpers.findItem(player, item)
            ?: return HttpBridge.err("No $item in inventory")

        return if (found.inHotbar) {
            dropFromHotbar(player, item, count, hotbarSlot = found.piSlot)
        } else {
            dropFromMainInventory(player, item, count, sourcePsh = found.pshSlot)
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
            mapOf("discarded" to dropped, "method" to "real"),
            // Match legacy: top-line uses requested count; data carries
            // the actual drop count.
            "Discarded $count $item",
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
            mapOf("discarded" to dropped, "method" to "real"),
            "Discarded $count $item",
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
