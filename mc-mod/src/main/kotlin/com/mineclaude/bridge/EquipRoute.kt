package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.network.packet.c2s.play.PlayerActionC2SPacket
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Direction

/**
 * `POST /equip` — select a hotbar item or swap it to the offhand.
 *
 * Phase 2 scope: hotbar items only. Armor slots return an error so
 * the legacy bridge keeps handling them until Phase 3 ports container
 * interaction. The route is implemented and tested directly against
 * :8081 but is **not yet** in `agent.bridge.NATIVE_ENDPOINTS` — leaving
 * /equip on legacy preserves armor + non-hotbar inventory item support.
 *
 * Native is more direct than legacy: we know `player_inventory_select_slot`
 * is broken on 1.21.5 only because it goes through the (broken)
 * ServerboundPickItemPacket path. Setting `selectedSlot` directly +
 * sending UpdateSelectedSlotC2SPacket is the supported path and works
 * cleanly.
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
        return TickThread.submitAndWait(timeoutMs = 2_000) { equipOnTick(item, slot) }
    }

    private fun equipOnTick(item: String, slot: String): BridgeResponse {
        val player = MinecraftClient.getInstance().player
            ?: return HttpBridge.err("no player — not connected to a world")

        val normalized = slot.lowercase()
        if (normalized in ARMOR_SLOTS) {
            return HttpBridge.err(
                "native /equip does not yet support armor slot '$slot' — leave on legacy bridge"
            )
        }
        if (normalized != "hand" && normalized != "mainhand" && normalized != "offhand") {
            return HttpBridge.err("Unknown equip slot: $slot")
        }

        val hotbar = InventoryHelpers.findInHotbar(player, item)
            ?: return HttpBridge.err(
                if (InventoryHelpers.existsInInventory(player, item))
                    "$item not in hotbar (Phase 2 native /equip is hotbar-only)"
                else
                    "No $item in inventory"
            )

        // Select the hotbar slot. The supported path: mutate the local
        // selectedSlot then ship UpdateSelectedSlotC2SPacket so the server
        // mirrors the client. (Going through key-bind dispatch would just
        // do the same thing one tick later.)
        player.inventory.setSelectedSlot(hotbar)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(hotbar))

        if (normalized == "offhand") {
            // Swap-hands packet does the rest — server moves the held stack
            // into the offhand slot and vice versa, then echoes the result.
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

        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to $slot",
        )
    }

    private val ARMOR_SLOTS = setOf("head", "chest", "legs", "feet")
}
