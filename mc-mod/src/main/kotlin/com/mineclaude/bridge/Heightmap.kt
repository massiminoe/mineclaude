package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.util.math.BlockPos

/**
 * Standable-y resolution shared by /heightmap, /goto, and /place.
 *
 * "Standable" = the player's feet would occupy [y]:
 *   - replaceable at (x, y, z)        — feet
 *   - replaceable at (x, y + 1, z)    — head clearance
 *   - non-replaceable at (x, y - 1, z) — floor
 *
 * Search expands outward from `nearY`: 0, +1, -1, +2, -2 … up to ±MAX_RANGE.
 * Picking by proximity to `nearY` is what makes the predicate useful indoors
 * / underground — a query from inside a cave returns the cave floor near you,
 * not the world surface 40 blocks above.
 *
 * All callers must invoke from the client tick thread (touches `world`).
 */
internal object Heightmap {
    /** Search range above and below `nearY`. */
    const val MAX_RANGE = 64

    data class Cell(val y: Int, val floorBlock: String)

    /** The standable cell at column (x, z) closest to [nearY], or null. */
    fun findStandable(x: Int, z: Int, nearY: Int): Cell? {
        for (dy in 0..MAX_RANGE) {
            checkAt(x, nearY + dy, z)?.let { return Cell(nearY + dy, it) }
            if (dy != 0) {
                checkAt(x, nearY - dy, z)?.let { return Cell(nearY - dy, it) }
            }
        }
        return null
    }

    /** If feet at (x,y,z) is standable, return the floor block id; else null. */
    private fun checkAt(x: Int, y: Int, z: Int): String? {
        val world = MinecraftClient.getInstance().world ?: return null
        val feet = BlockPos(x, y, z)
        val head = BlockPos(x, y + 1, z)
        val floor = BlockPos(x, y - 1, z)
        if (!world.getBlockState(feet).isReplaceable) return null
        if (!world.getBlockState(head).isReplaceable) return null
        if (world.getBlockState(floor).isReplaceable) return null
        return WorldHelpers.blockIdAt(floor)
    }
}
