package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket

/**
 * `POST /discard` — drop items from the hotbar.
 *
 * Phase 2 scope: hotbar items only (matches /equip). Non-hotbar items
 * return an error pointing at the Phase 2b inventory-move helper. Until
 * that lands, /discard stays off `NATIVE_ENDPOINTS` so legacy continues
 * to handle the broader inventory case.
 *
 * Drop semantics: select the slot, then call `dropSelectedItem(false)`
 * once per requested unit. The `false` argument means "drop one item from
 * the stack" (vs `true` = drop the entire stack). The client method
 * handles both the local stack-shrink and the
 * PlayerActionC2SPacket(DROP_ITEM, …) packet to the server.
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
        return TickThread.submitAndWait(timeoutMs = 2_000) { dropOnTick(item, count) }
    }

    private fun dropOnTick(item: String, count: Int): BridgeResponse {
        val player = MinecraftClient.getInstance().player
            ?: return HttpBridge.err("no player — not connected to a world")

        val hotbar = InventoryHelpers.findInHotbar(player, item)
            ?: return HttpBridge.err(
                if (InventoryHelpers.existsInInventory(player, item))
                    "$item not in hotbar (Phase 2 native /discard is hotbar-only)"
                else
                    "No $item in inventory"
            )

        // Select first so dropSelectedItem targets the requested stack.
        player.inventory.setSelectedSlot(hotbar)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(hotbar))

        var dropped = 0
        for (i in 0 until count) {
            val stack = player.inventory.getStack(hotbar)
            if (stack.isEmpty) break
            // false = single item, not entire stack. Returns true if a
            // drop event actually fired (stack non-empty + drop allowed).
            if (player.dropSelectedItem(false)) dropped++ else break
        }

        if (dropped == 0) {
            return HttpBridge.err("Failed to drop any $item")
        }
        return HttpBridge.ok(
            mapOf("discarded" to dropped, "method" to "real"),
            // Match legacy: top-line uses requested `count`, structured
            // data carries the actual.
            "Discarded $count $item",
        )
    }
}
