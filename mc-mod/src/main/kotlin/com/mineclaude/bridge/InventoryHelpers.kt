package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.player.PlayerInventory
import net.minecraft.registry.Registries
import net.minecraft.screen.PlayerScreenHandler
import net.minecraft.screen.ScreenHandler
import net.minecraft.screen.slot.SlotActionType

/**
 * Inventory helpers shared by /equip and /discard.
 *
 * The native bridge bypasses the legacy `/item replace` shuffle entirely:
 * `MinecraftClient.interactionManager.clickSlot` is the supported client
 * path (it ships `ClickSlotC2SPacket` and updates local state in lockstep)
 * and does not depend on the broken `player_inventory_slot_to_hotbar`
 * Minescript API. The PlayerScreenHandler is always the player's
 * `currentScreenHandler` when no other GUI is open, so we can click into
 * armor / main-inventory slots without first opening any UI.
 *
 * # Slot indexing
 *
 * Two coordinate systems are at play and the conversion catches everyone:
 *
 * - **PlayerInventory (PI)** — flat list `inv.getStack(i)` reads/writes:
 *     0..8     hotbar
 *     9..35    main inventory
 *     36..39   armor (feet, legs, chest, head — bottom-up)
 *     40       offhand
 *
 * - **PlayerScreenHandler (PSH)** — the slot indexes you pass to
 *   `interactionManager.clickSlot`:
 *     0        crafting result
 *     1..4     2x2 crafting input
 *     5..8     armor (head, chest, legs, feet — top-down)
 *     9..35    main inventory  (same as PI)
 *     36..44   hotbar          (PI 0..8 + 36)
 *     45       offhand
 *
 * Notice the armor ordering inverts and the hotbar offset is +36. Helpers
 * here always return PSH indices since callers click on PSH; PI is only
 * used when reading raw stack contents via `inv.getStack`.
 */
internal object InventoryHelpers {
    const val HOTBAR_SIZE = PlayerInventory.HOTBAR_SIZE

    /** PSH slot indices for armor by name — what /equip needs to click. */
    val ARMOR_PSH_SLOTS = mapOf(
        "head" to 5,
        "chest" to 6,
        "legs" to 7,
        "feet" to 8,
    )

    /** PSH offset for the hotbar (PI 0..8 → PSH 36..44). */
    private const val PSH_HOTBAR_BASE = 36

    /**
     * Where in the inventory we found a matching stack. PSH slot is what
     * callers click; the `inHotbar` flag distinguishes the fast-path
     * (already on the action bar) from the slow-path (needs a swap).
     */
    data class FoundStack(val pshSlot: Int, val inHotbar: Boolean, val piSlot: Int)

    /**
     * Find a matching item anywhere in the main inventory + hotbar.
     * Hotbar is preferred so /equip and /discard hit the fast path when
     * possible. Armor / offhand / crafting slots are *not* searched —
     * we don't want to accidentally consume equipped armor.
     *
     * Returns null if no match.
     */
    fun findItem(player: ClientPlayerEntity, itemName: String): FoundStack? {
        val target = itemName.removePrefix("minecraft:")
        val inv = player.inventory
        // Hotbar first.
        for (i in 0 until HOTBAR_SIZE) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            if (Registries.ITEM.getId(stack.item).path == target) {
                return FoundStack(pshSlot = PSH_HOTBAR_BASE + i, inHotbar = true, piSlot = i)
            }
        }
        // Main inventory (PI 9..35 maps 1:1 to PSH 9..35).
        for (i in HOTBAR_SIZE until 36) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            if (Registries.ITEM.getId(stack.item).path == target) {
                return FoundStack(pshSlot = i, inHotbar = false, piSlot = i)
            }
        }
        return null
    }

    /**
     * Return true if the item exists anywhere we'd consider — used only
     * for distinguishing "not in inventory at all" from "not where you
     * asked" in error messages.
     */
    fun existsInInventory(player: ClientPlayerEntity, itemName: String): Boolean =
        findItem(player, itemName) != null

    /**
     * Pick a hotbar slot (0..8) to stage a non-hotbar item into. Prefers
     * the first empty slot; falls back to the currently-held slot when
     * everything is occupied. The caller is expected to swap that slot
     * back out of the hotbar after they're done so the player's loadout
     * isn't altered.
     */
    fun pickHotbarStagingSlot(player: ClientPlayerEntity): Int {
        val inv = player.inventory
        for (i in 0 until HOTBAR_SIZE) {
            if (inv.getStack(i).isEmpty) return i
        }
        return inv.getSelectedSlot()
    }

    /**
     * Verify we can issue clickSlot calls right now. Server only honors
     * clicks against the screen handler the player currently has open —
     * if some other screen is up (chest, furnace, etc.) the click would
     * either be rejected or hit the wrong slot index.
     */
    fun ensurePlayerScreenOpen(player: ClientPlayerEntity): String? {
        val handler = player.currentScreenHandler
        if (handler !is PlayerScreenHandler) {
            return "another screen is open (${handler.javaClass.simpleName}) — close it first"
        }
        if (!handler.cursorStack.isEmpty) {
            return "cursor is not empty — refusing to click and risk dropping items"
        }
        return null
    }

    /** Convenience wrapper: clickSlot against the current handler. */
    fun click(player: ClientPlayerEntity, slot: Int, button: Int, action: SlotActionType) {
        val mc = MinecraftClient.getInstance()
        val mgr = mc.interactionManager ?: error("no interaction manager")
        val handler: ScreenHandler = player.currentScreenHandler
        mgr.clickSlot(handler.syncId, slot, button, action, player)
    }
}
