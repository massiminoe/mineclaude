package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.registry.Registries
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Direction
import net.minecraft.util.math.Vec3d
import kotlin.math.atan2
import kotlin.math.sqrt

/**
 * Shared helpers for world-mutation primitives (/break, /place, /attack).
 *
 * Mirrors the legacy [`bridge.player_control`] surface — look_at, reach
 * checks, adjacent-solid lookup, replaceable-block table — so the native
 * impls of /break and /place behave identically to the Minescript-backed
 * ones the agent has been training against.
 *
 * All public helpers must be called on the tick thread (via [TickThread]).
 * They touch `player.pos`, `world.getBlockState`, etc., which MC requires
 * to be accessed from the client thread.
 */
internal object WorldHelpers {
    /** Standing eye height in blocks — matches MC's PlayerEntity.getStandingEyeHeight(). */
    private const val EYE_HEIGHT = 1.62

    /** MC's default block-interaction reach. */
    const val BLOCK_REACH = 4.5
    const val NAV_REACH = 3.5

    /**
     * Aim the player's head at the centre of [pos]. Does the same yaw/pitch
     * math as the legacy `player_look_at` fallback path — ClientPlayerEntity
     * exposes [setYaw]/[setPitch] which the server picks up on the next
     * movement packet.
     */
    fun lookAtBlock(player: ClientPlayerEntity, pos: BlockPos) {
        lookAtPosition(player, pos.x + 0.5, pos.y + 0.5, pos.z + 0.5)
    }

    fun lookAtPosition(player: ClientPlayerEntity, x: Double, y: Double, z: Double) {
        val px = player.x
        val py = player.y + EYE_HEIGHT
        val pz = player.z
        val dx = x - px
        val dy = y - py
        val dz = z - pz
        val distXz = sqrt(dx * dx + dz * dz)
        val yaw = (-Math.toDegrees(atan2(dx, dz))).toFloat()
        val pitch = (-Math.toDegrees(atan2(dy, distXz))).toFloat()
        player.yaw = yaw
        player.pitch = pitch
    }

    /**
     * Eye-to-block-centre distance check. Mirrors MC's own block-interact
     * range (eye, not foot). The 1.62 vertical offset matters for upward
     * targets (canonical case: tree-top logs).
     */
    fun isBlockWithinReach(player: ClientPlayerEntity, pos: BlockPos, reach: Double = BLOCK_REACH): Boolean {
        return eyeToBlockDistance(player, pos) <= reach
    }

    /** Eye-to-block-centre distance. Same math as [isBlockWithinReach]. */
    fun eyeToBlockDistance(player: ClientPlayerEntity, pos: BlockPos): Double {
        val ex = player.x
        val ey = player.y + EYE_HEIGHT
        val ez = player.z
        val cx = pos.x + 0.5
        val cy = pos.y + 0.5
        val cz = pos.z + 0.5
        return sqrt((cx - ex).let { it * it } + (cy - ey).let { it * it } + (cz - ez).let { it * it })
    }

    /** Signed Δy from player eye to block centre — positive means target is above. */
    fun eyeToBlockDy(player: ClientPlayerEntity, pos: BlockPos): Double {
        return (pos.y + 0.5) - (player.y + EYE_HEIGHT)
    }

    /** Foot-based distance — used for entities (item drops sit at foot height). */
    fun playerDistance(player: ClientPlayerEntity, x: Double, y: Double, z: Double): Double {
        val dx = x - player.x
        val dy = y - player.y
        val dz = z - player.z
        return sqrt(dx * dx + dy * dy + dz * dz)
    }

    /**
     * The face nearest the player on [pos] — picked by dominant axis on the
     * player→block vector. Used to pass a Direction to attackBlock /
     * updateBlockBreakingProgress without doing a full raycast: we already
     * aim at the block via [lookAtBlock], so any face the eye-ray actually
     * hits is good enough; the dominant-axis pick matches it in the common
     * case and is harmless otherwise (server does its own validation).
     */
    fun playerFacingSide(player: ClientPlayerEntity, pos: BlockPos): Direction {
        val dx = (pos.x + 0.5) - player.x
        val dy = (pos.y + 0.5) - (player.y + EYE_HEIGHT)
        val dz = (pos.z + 0.5) - player.z
        val ax = kotlin.math.abs(dx)
        val ay = kotlin.math.abs(dy)
        val az = kotlin.math.abs(dz)
        return when {
            ay >= ax && ay >= az -> if (dy < 0) Direction.UP else Direction.DOWN
            ax >= az -> if (dx < 0) Direction.EAST else Direction.WEST
            else -> if (dz < 0) Direction.SOUTH else Direction.NORTH
        }
    }

    /**
     * Return a non-air solid neighbour we can click against to place a
     * block in [target]. Prefers the block below (top face — matches a
     * player tossing dirt onto the ground). Returns null when [target] is
     * floating in air (no neighbours), in which case /place errors out.
     */
    data class Adjacent(val pos: BlockPos, val face: Direction)

    fun findAdjacentSolidBlock(target: BlockPos): Adjacent? {
        val world = MinecraftClient.getInstance().world ?: return null
        val candidates = listOf(
            BlockPos(target.x, target.y - 1, target.z) to Direction.UP,
            BlockPos(target.x, target.y + 1, target.z) to Direction.DOWN,
            BlockPos(target.x - 1, target.y, target.z) to Direction.EAST,
            BlockPos(target.x + 1, target.y, target.z) to Direction.WEST,
            BlockPos(target.x, target.y, target.z - 1) to Direction.SOUTH,
            BlockPos(target.x, target.y, target.z + 1) to Direction.NORTH,
        )
        for ((p, face) in candidates) {
            val state = world.getBlockState(p)
            if (state.isAir) continue
            return Adjacent(p, face)
        }
        return null
    }

    /** Strip the `minecraft:` namespace + any blockstate suffix. */
    fun normalizeBlockId(id: String): String {
        val noState = id.substringBefore('[')
        return noState.substringAfter(':', noState)
    }

    /** Block id from a BlockPos as the namespace-stripped path string. */
    fun blockIdAt(pos: BlockPos): String {
        val world = MinecraftClient.getInstance().world ?: return "air"
        val state = world.getBlockState(pos)
        return Registries.BLOCK.getId(state.block).path
    }

    /**
     * Vanilla MC silently replaces these blocks when something is placed
     * into their cell — short_grass overlays, flowers, snow layer, water,
     * etc. Pinned to MC 1.21.5; mirrors `bridge.blocks.REPLACEABLE_BLOCKS`
     * so /place behaviour is bit-identical between bridges.
     */
    val REPLACEABLE_BLOCKS: Set<String> = setOf(
        "air", "cave_air", "void_air",
        "short_grass", "tall_grass", "fern", "large_fern",
        "dead_bush", "seagrass", "tall_seagrass",
        "vine", "glow_lichen", "hanging_roots",
        "pink_petals", "leaf_litter", "wildflowers",
        "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
        "red_tulip", "orange_tulip", "white_tulip", "pink_tulip",
        "oxeye_daisy", "cornflower", "lily_of_the_valley", "wither_rose",
        "torchflower", "pitcher_plant",
        "sunflower", "lilac", "rose_bush", "peony",
        "oak_sapling", "spruce_sapling", "birch_sapling", "jungle_sapling",
        "acacia_sapling", "dark_oak_sapling", "mangrove_propagule",
        "cherry_sapling", "azalea", "flowering_azalea",
        "brown_mushroom", "red_mushroom",
        "snow",
        "water", "lava", "bubble_column",
        "fire", "soul_fire", "light",
    )

    fun isReplaceable(id: String?): Boolean {
        if (id.isNullOrEmpty()) return true
        return normalizeBlockId(id) in REPLACEABLE_BLOCKS
    }

    /**
     * Block types /break will NOT auto-clear when they occlude the target.
     * Containers and functional blocks could have game-state consequences
     * (chest contents drop, breaking a bed the player needs). Naturally-
     * placed terrain is fair game. Mirrors the legacy denylist verbatim.
     */
    val OCCLUDER_DENYLIST: Set<String> = setOf(
        "chest", "trapped_chest", "ender_chest", "barrel", "shulker_box",
        "furnace", "blast_furnace", "smoker", "crafting_table", "loom",
        "anvil", "chipped_anvil", "damaged_anvil",
        "bed", "white_bed", "orange_bed", "magenta_bed", "light_blue_bed",
        "yellow_bed", "lime_bed", "pink_bed", "gray_bed", "light_gray_bed",
        "cyan_bed", "purple_bed", "blue_bed", "brown_bed", "green_bed",
        "red_bed", "black_bed",
        "door", "oak_door", "iron_door", "spruce_door", "birch_door",
        "jungle_door", "acacia_door", "dark_oak_door", "mangrove_door",
        "sign", "wall_sign", "hanging_sign",
        "lectern", "bookshelf", "enchanting_table", "beacon",
        "brewing_stand", "cauldron", "hopper", "dispenser", "dropper",
        "observer", "note_block", "jukebox", "conduit",
        "spawner", "end_portal_frame",
    )

    /**
     * Defensively close any lingering screen before a world-interaction
     * primitive. Input is captured by Screens, so any open GUI would
     * silently no-op the action. Self-heals plus logs.
     */
    fun ensureNoScreenOpen(player: ClientPlayerEntity): String? {
        val mc = MinecraftClient.getInstance()
        if (mc.currentScreen == null) return null
        HttpBridge.log.warn(
            "ensureNoScreen: closing lingering {} before world action",
            mc.currentScreen!!.javaClass.simpleName,
        )
        player.closeHandledScreen()
        // Also nuke any orphan Screen the local UI may still be holding.
        mc.setScreen(null)
        return null
    }

    /** Held-position centre of [pos] as a Vec3d. */
    fun blockCentre(pos: BlockPos): Vec3d = Vec3d(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5)
}
