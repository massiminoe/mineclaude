package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.item.ItemStack
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.screen.slot.SlotActionType
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * Picks and equips the right mining tool before /break swings.
 *
 * # Why this exists
 *
 * [BreakRoute] swings whatever is in the mainhand — `interactionManager`
 * mines with the held stack, it never selects a tool. That's correct *if*
 * the right tool is already held, but `placeBlock`/`use` swap the held item
 * (a torch, a block) and leave it held; a following /break then swings
 * bare-handed (slow, and stone/ore drop nothing). The agent equipping a
 * pickaxe doesn't help if it then places a torch before mining.
 *
 * So /break self-selects, the same way Baritone's miner does: ensure a tool
 * that can actually harvest the target is held before the swing loop.
 *
 * # Policy (see [equipBestToolFor])
 *
 *  - **Held item already harvests it → leave it.** This is the conservative
 *    hook: if you `equip` a specific tool (the stone pickaxe to spare your
 *    diamond one) it is kept, because it's already suitable — the selector
 *    never overrides a working held tool. Also covers hand-mineable blocks
 *    (dirt: every item `isSuitableFor`, so no churn).
 *  - **Otherwise pick the BEST available tool.** Highest material tier that
 *    `isSuitableFor` the block (so it yields drops *and* mines fastest),
 *    tie-broken by most durability remaining. Auto-select optimises for the
 *    break succeeding fast, not for sparing premium tools — to be
 *    conservative, equip the cheaper tool yourself (see above).
 *  - **Nothing suitable in inventory → leave the held item.** Honest
 *    bare-handed mine, exactly the pre-existing behaviour.
 *
 * The switch is idempotent and not restored: a mining loop selects the tool
 * once on the first break and every subsequent break no-ops (held is now
 * suitable). Leaving the right tool held is the desired end state, and it
 * mirrors the snapshot-less "best tool stays" behaviour of Baritone.
 */
internal object ToolSelector {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.tool")!!

    /**
     * Material tiers, higher = better (more capable + faster). Parsed from the
     * item id prefix so it's mapping-stable across MC versions (no enum
     * coupling) and degrades gracefully — an unranked material sorts BELOW
     * every known tier, so it's only auto-picked when nothing better exists.
     * Selection takes the MAX rank among suitable tools (best tool wins).
     */
    private val MATERIAL_RANK = listOf(
        "wooden_" to 0, "golden_" to 1, "stone_" to 2,
        "iron_" to 3, "diamond_" to 4, "netherite_" to 5,
    )

    private const val UNRANKED = -1

    private fun materialRank(path: String): Int {
        for ((prefix, rank) in MATERIAL_RANK) if (path.startsWith(prefix)) return rank
        return UNRANKED
    }

    /**
     * Ensure the player holds a tool that can harvest the block at [pos]
     * before /break swings. Returns the item path we switched to, or null if
     * no change was made (held item already suitable, or nothing better
     * exists). MUST be called on the tick thread — touches world/inventory
     * state and issues `clickSlot`.
     */
    fun equipBestToolFor(pos: BlockPos): String? {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return null
        val world = mc.world ?: return null
        val state = world.getBlockState(pos)
        if (state.isAir) return null

        // Already holding something that harvests this block? Leave it — this
        // is both the "agent equipped the right tool" and the "hand-mineable
        // block, anything works" fast path.
        if (player.mainHandStack.isSuitableFor(state)) return null

        // Scan hotbar (PI 0..8) + main inventory (PI 9..35) for suitable tools.
        // Armor/offhand/crafting slots are deliberately not searched.
        val inv = player.inventory
        var bestSlot = -1
        var bestRank = Int.MIN_VALUE
        var bestRemaining = -1
        for (i in 0 until 36) {
            val stack = inv.getStack(i)
            if (stack.isEmpty) continue
            if (!stack.isSuitableFor(state)) continue
            val rank = materialRank(Registries.ITEM.getId(stack.item).path)
            val rem = remainingDurability(stack)
            // Best tier wins; tie-break by most durability remaining.
            val better = rank > bestRank || (rank == bestRank && rem > bestRemaining)
            if (better) {
                bestSlot = i
                bestRank = rank
                bestRemaining = rem
            }
        }
        if (bestSlot < 0) return null  // nothing can harvest it — leave held, honest

        val chosenPath = Registries.ITEM.getId(inv.getStack(bestSlot).item).path

        // Bring the chosen tool onto the action bar (if it's in main inv) and
        // select it. Staging mirrors EquipRoute's SWAP path.
        val hotbarSlot: Int = if (bestSlot < InventoryHelpers.HOTBAR_SIZE) {
            bestSlot
        } else {
            val screenErr = InventoryHelpers.ensurePlayerScreenOpen(player)
            if (screenErr != null) {
                // Can't safely click right now (a GUI is up / cursor busy).
                // Skip the swap rather than risk dropping items — /break falls
                // back to mining with whatever's held.
                log.warn("tool: skipping auto-select of {} — {}", chosenPath, screenErr)
                return null
            }
            val staging = InventoryHelpers.pickHotbarStagingSlot(player)
            // Main-inv PI slot maps 1:1 to its PSH slot (9..35); SWAP's button
            // arg is the destination hotbar index (0..8).
            InventoryHelpers.click(player, bestSlot, staging, SlotActionType.SWAP)
            staging
        }
        selectHotbar(player, hotbarSlot)
        log.info("tool: auto-selected {} for {} at ({}, {}, {})",
            chosenPath, WorldHelpers.blockIdAt(pos), pos.x, pos.y, pos.z)
        return chosenPath
    }

    private fun remainingDurability(stack: ItemStack): Int =
        if (stack.isDamageable) stack.maxDamage - stack.damage else Int.MAX_VALUE

    private fun selectHotbar(player: ClientPlayerEntity, slot: Int) {
        player.inventory.setSelectedSlot(slot)
        player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
    }
}
