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
 *
 * # Cross-bridge sync barrier (post-Phase 3 fix)
 *
 * The native bridge runs alongside the legacy Python bridge. When the
 * legacy bridge has just done a /craft (or any container_open dance) and
 * issued a container_close, the *client* sees the new PlayerScreenHandler
 * immediately but the *server* may not have processed the close packet
 * yet. If a native /equip arrives in that window and fires a clickSlot,
 * the syncId of the click won't match the server's still-open
 * CraftingScreenHandler — server silently drops the click. The local
 * SWAP applied client-side; the held-slot select also applied; but the
 * server's view of the player's inventory + held item is stale, so the
 * next break swings bare-handed (observed: stones broke, no cobblestone
 * dropped).
 *
 * Fix:
 *   1. Before the SWAP path, ship `closeHandledScreen()` as an explicit
 *      sync barrier and wait one client tick so client + server agree.
 *   2. After the select, read mainHandStack and verify it actually holds
 *      the requested item. If not, return a clear error rather than
 *      reporting a false success.
 */
object EquipRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.equip")!!

    /** ms to wait between tick submissions for client+server screen-state sync. */
    private const val SCREEN_SYNC_MS = 80L
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
     * hotbar via SWAP after a closeHandledScreen sync barrier.
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
            // Slow path: close any handled screen as a sync barrier, wait
            // one tick, then SWAP. Without this, a recent legacy /craft can
            // leave the server in CraftingScreenHandler — clickSlot fires
            // against the wrong syncId and is silently dropped server-side.
            syncCloseScreen()
            Thread.sleep(SCREEN_SYNC_MS)
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
            log.warn(
                "equip: post-select mainhand does not hold {} (asked for slot {}); " +
                    "likely a screen-handler desync — see Phase 2b sync-barrier comment.",
                item, hotbarSlot,
            )
            return HttpBridge.err(
                "equip did not take effect — mainhand is not $item after select. " +
                    "Likely cause: server's screen handler was out of sync (stale /craft " +
                    "or container). A retry should succeed."
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
            syncCloseScreen()
            Thread.sleep(SCREEN_SYNC_MS)
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
                "equip did not take effect — offhand is not $item after swap-hands. " +
                    "Likely cause: server's screen handler was out of sync. A retry should succeed."
            )
        }
        return HttpBridge.ok(
            mapOf("equipped" to true, "method" to "real"),
            "Equipped $item to offhand",
        )
    }

    /**
     * Move [item] into the named armor slot. Strategy:
     *   1. Sync barrier: closeHandledScreen + 1 tick wait so we don't
     *      click against a stale screen handler.
     *   2. Find the item (errors if absent).
     *   3. PICKUP from source PSH slot — cursor holds the stack.
     *   4. PICKUP at the armor PSH slot — server validates the item is
     *      the right armor type and either accepts (cursor empties) or
     *      rejects (cursor still holds the stack).
     *   5. If cursor still has it, deposit back at source so we don't
     *      drop items on the ground when the screen handler closes.
     *   6. Verify via `getEquippedStack(slot)`: belt-and-suspenders for
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

        // Sync barrier — armor click also needs the right syncId server-side.
        syncCloseScreen()
        Thread.sleep(SCREEN_SYNC_MS)

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
     * Tick-thread helper: ship a CloseHandledScreenC2SPacket so the
     * server resets to PlayerScreenHandler. No-op visually if no GUI is
     * open client-side, but acts as a sync barrier when the *server*
     * still has a stale handler from a recent /craft / /open_container.
     */
    private fun syncCloseScreen() {
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            // closeHandledScreen sends CloseHandledScreenC2SPacket and
            // resets currentScreenHandler. Safe to call even when only
            // PlayerScreenHandler is "open" — server treats it as a
            // standard inventory close.
            player.closeHandledScreen()
            Unit
        }
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
