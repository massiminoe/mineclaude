package com.mineclaude.bridge

import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.player.PlayerInventory
import net.minecraft.registry.Registries

/**
 * Inventory helpers shared by /equip and /discard.
 *
 * Phase 2 scope: hotbar-only operations (slots 0..8 of the main inventory).
 * Items in the broader inventory require the legacy `/item replace` path
 * — which we don't replicate here. When an item exists in inventory but
 * not in the hotbar, callers return a clear error ("not in hotbar") and
 * leave routing on the legacy bridge.
 */
internal object InventoryHelpers {
    /** Slots 0..8 are the hotbar. The hotbar lives at the start of `main`. */
    const val HOTBAR_SIZE = PlayerInventory.HOTBAR_SIZE

    /**
     * Return the hotbar slot (0..8) containing an item whose registry path
     * matches [itemName], or null if not in the hotbar. Strips the leading
     * `minecraft:` namespace before comparison so callers can pass either
     * form. Picks the lowest matching slot for determinism.
     */
    fun findInHotbar(player: ClientPlayerEntity, itemName: String): Int? {
        val target = itemName.removePrefix("minecraft:")
        val inv = player.inventory
        for (i in 0 until HOTBAR_SIZE) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            val path = Registries.ITEM.getId(stack.item).path
            if (path == target) return i
        }
        return null
    }

    /**
     * Return true if [itemName] exists anywhere in the player's main
     * inventory (not including armor/offhand). Used for crafting better
     * error messages: "X not in hotbar" vs "X not in inventory at all".
     */
    fun existsInInventory(player: ClientPlayerEntity, itemName: String): Boolean {
        val target = itemName.removePrefix("minecraft:")
        val inv = player.inventory
        for (i in 0 until inv.size()) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            val path = Registries.ITEM.getId(stack.item).path
            if (path == target) return true
        }
        return false
    }
}
