package com.mineclaude.bridge

import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.Entity
import net.minecraft.entity.ItemEntity
import net.minecraft.entity.LivingEntity
import net.minecraft.util.math.Box
import net.minecraft.util.math.MathHelper
import org.slf4j.LoggerFactory
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.sqrt

/**
 * Idle "look at the nearest entity" cosmetic camera — pure stream charm.
 *
 * When the bot is doing nothing — no functional aim in flight, not walking,
 * no GUI open, alive — it slowly slews its head to face the nearest
 * interesting entity. The instant any real work touches rotation it yields
 * and stays out of the way.
 *
 * Safety model — the director NEVER fights meaningful work:
 *   - every functional aim (break / place / attack / interact / aimed
 *     screenshot) stamps [noteFunctionalAim]; the director stays dormant for
 *     [HOLD_MS] afterwards.
 *   - it does nothing while the body is moving (Baritone owns yaw then),
 *     while a screen is open, or while dead.
 *
 * It only ever nudges yaw/pitch by a small per-tick step, so MC's render
 * lerp (prevYaw -> yaw across frames) turns it into smooth motion for free.
 *
 * Runs on the client tick thread (its own END_CLIENT_TICK hook), the same
 * thread every other rotation write uses, so it races nothing. Disable with
 * env MINECLAUDE_IDLE_CAMERA=0.
 */
object CameraDirector {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.camera")!!

    /** Standing eye height — matches WorldHelpers.EYE_HEIGHT. */
    private const val EYE_HEIGHT = 1.62

    /** How long after any functional aim the director stays dormant (ms). */
    private const val HOLD_MS = 2_000L

    /** Search radius for the nearest entity (blocks). */
    private const val SCAN_RADIUS = 16.0

    /** Body displacement per tick (blocks) above which we count as "moving". */
    private const val MOVE_THRESHOLD = 0.02

    /** Fraction of the remaining angle to close each tick (ease-out feel). */
    private const val EASE = 0.35f

    /** Hard cap on per-tick rotation (deg) — "fast, but not instant". */
    private const val MAX_STEP_DEG = 25f

    /** Stop nudging once within this many degrees of target (anti-jitter). */
    private const val DEADZONE_DEG = 1.0f

    @Volatile private var enabled = true

    /** Wall-clock of the last real rotation set by a route. */
    @Volatile private var lastFunctionalAimMs = 0L

    // Body-movement tracking via position delta between ticks.
    private var hasLastPos = false
    private var lastX = 0.0
    private var lastZ = 0.0

    /**
     * Stamp from any route that sets rotation for real. Keeps the idle
     * camera dormant for [HOLD_MS] so it can't pan away mid-action (or
     * spoil an aimed screenshot during its settle window).
     */
    fun noteFunctionalAim() {
        lastFunctionalAimMs = System.currentTimeMillis()
    }

    fun register() {
        if (System.getenv("MINECLAUDE_IDLE_CAMERA") == "0") {
            enabled = false
            log.info("CameraDirector: disabled via MINECLAUDE_IDLE_CAMERA=0")
            return
        }
        ClientTickEvents.END_CLIENT_TICK.register(
            ClientTickEvents.EndTick { client -> tick(client.player) }
        )
        log.info("CameraDirector: idle look-at-nearest-entity active")
    }

    private fun tick(player: ClientPlayerEntity?) {
        if (!enabled) return
        if (player == null) {
            hasLastPos = false
            return
        }

        // Track body movement every tick, even when we bail early below, so
        // the first idle tick after walking has a fresh baseline.
        val moved = if (hasLastPos) {
            val dx = player.x - lastX
            val dz = player.z - lastZ
            sqrt(dx * dx + dz * dz)
        } else {
            0.0
        }
        lastX = player.x
        lastZ = player.z
        hasLastPos = true

        val mc = MinecraftClient.getInstance()

        // --- gates: never intrude on meaningful work ---
        if (mc.currentScreen != null) return                                    // GUI open
        if (player.isDead || player.health <= 0f) return                        // dead
        if (moved > MOVE_THRESHOLD) return                                       // walking (Baritone owns yaw)
        if (System.currentTimeMillis() - lastFunctionalAimMs < HOLD_MS) return  // recent real aim

        val target = nearestInteresting(player) ?: return

        // Target angles — same atan2 math as WorldHelpers.lookAtPosition, but
        // we compute them here and slew toward them instead of snapping.
        val dx = target.x - player.x
        val dy = target.eyeY - (player.y + EYE_HEIGHT)
        val dz = target.z - player.z
        val distXz = sqrt(dx * dx + dz * dz)
        val targetYaw = (-Math.toDegrees(atan2(dx, dz))).toFloat()
        val targetPitch = (-Math.toDegrees(atan2(dy, distXz))).toFloat()

        player.yaw = slew(player.yaw, targetYaw)
        player.pitch = MathHelper.clamp(slew(player.pitch, targetPitch), -90f, 90f)
    }

    /** Ease [current] toward [target] along the shortest arc, capped per tick. */
    private fun slew(current: Float, target: Float): Float {
        val delta = MathHelper.wrapDegrees(target - current)
        if (abs(delta) < DEADZONE_DEG) return current
        val step = (delta * EASE).coerceIn(-MAX_STEP_DEG, MAX_STEP_DEG)
        return current + step
    }

    /**
     * Nearest interesting entity within [SCAN_RADIUS]. Self is excluded by
     * [net.minecraft.world.World.getOtherEntities]; the whitelist keeps it to
     * mobs / animals / other players ([LivingEntity]) and dropped items
     * ([ItemEntity]), so XP orbs, arrows and the like are ignored.
     */
    private fun nearestInteresting(player: ClientPlayerEntity): Entity? {
        val world = MinecraftClient.getInstance().world ?: return null
        val box = Box(
            player.x - SCAN_RADIUS, player.y - SCAN_RADIUS, player.z - SCAN_RADIUS,
            player.x + SCAN_RADIUS, player.y + SCAN_RADIUS, player.z + SCAN_RADIUS,
        )
        var best: Entity? = null
        var bestSq = Double.MAX_VALUE
        for (entity in world.getOtherEntities(player, box)) {
            if (entity !is LivingEntity && entity !is ItemEntity) continue
            val sq = player.squaredDistanceTo(entity)
            if (sq < bestSq) {
                bestSq = sq
                best = entity
            }
        }
        return best
    }
}
