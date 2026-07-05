package com.mineclaude.bridge

import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.Entity
import net.minecraft.entity.ItemEntity
import net.minecraft.entity.LivingEntity
import net.minecraft.entity.mob.EndermanEntity
import net.minecraft.util.math.Box
import net.minecraft.util.math.MathHelper
import org.slf4j.LoggerFactory
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.sqrt

/**
 * Cosmetic camera director — pure stream charm. Two behaviours, both only
 * about how the POV reads on screen, never about what the bot physically does:
 *
 *   - IDLE (env MINECLAUDE_IDLE_CAMERA): when the bot is standing still it
 *     slowly slews its head to face the nearest interesting entity.
 *   - TRAVEL (env MINECLAUDE_TRAVEL_CAMERA): while the body is walking it
 *     faces the direction of motion (from the per-tick position delta) with
 *     the pitch eased to a level horizon, so the camera looks where it's
 *     going instead of at the ground / backwards.
 *
 * Why TRAVEL is cosmetic, not a steering input: Baritone paths with freeLook
 * on, so movement is decoupled from head yaw — it walks the path regardless
 * of where the head points, and only grabs yaw itself when it has to mine /
 * interact en route (which stamps [noteFunctionalAim] and makes us yield).
 * Writing yaw during a plain walk therefore changes only what's rendered.
 *
 * Safety model — the director NEVER fights meaningful work:
 *   - every functional aim (break / place / attack / interact / aimed
 *     screenshot) stamps [noteFunctionalAim]; the director stays dormant for
 *     [HOLD_MS] afterwards.
 *   - it does nothing while a screen is open or while dead.
 *
 * It only ever nudges yaw/pitch by a small per-tick step, so MC's render
 * lerp (prevYaw -> yaw across frames) turns it into smooth motion for free.
 *
 * Runs on the client tick thread (its own END_CLIENT_TICK hook), the same
 * thread every other rotation write uses, so it races nothing. Disable
 * either behaviour with MINECLAUDE_IDLE_CAMERA=0 / MINECLAUDE_TRAVEL_CAMERA=0.
 */
object CameraDirector {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.camera")!!

    /** Standing eye height — matches WorldHelpers.EYE_HEIGHT. */
    private const val EYE_HEIGHT = 1.62

    /** How long after any functional aim the director stays dormant (ms). */
    private const val HOLD_MS = 2_000L

    /** Search radius for the nearest entity (blocks). */
    private const val SCAN_RADIUS = 6.0

    /** Body displacement per tick (blocks) above which we count as "moving". */
    private const val MOVE_THRESHOLD = 0.02

    /** Fraction of the remaining angle to close each tick (ease-out feel). */
    private const val EASE = 0.35f

    /** Hard cap on per-tick rotation (deg) — "fast, but not instant". */
    private const val MAX_STEP_DEG = 25f

    /** Stop nudging once within this many degrees of target (anti-jitter). */
    private const val DEADZONE_DEG = 1.0f

    @Volatile private var idleEnabled = true
    @Volatile private var travelEnabled = true

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
        idleEnabled = System.getenv("MINECLAUDE_IDLE_CAMERA") != "0"
        travelEnabled = System.getenv("MINECLAUDE_TRAVEL_CAMERA") != "0"
        if (!idleEnabled && !travelEnabled) {
            log.info("CameraDirector: disabled via MINECLAUDE_IDLE_CAMERA=0 + MINECLAUDE_TRAVEL_CAMERA=0")
            return
        }
        ClientTickEvents.END_CLIENT_TICK.register(
            ClientTickEvents.EndTick { client -> tick(client.player) }
        )
        log.info("CameraDirector: active (idle=$idleEnabled travel=$travelEnabled)")
    }

    private fun tick(player: ClientPlayerEntity?) {
        if (player == null) {
            hasLastPos = false
            return
        }

        // Track body movement every tick, even when we bail early below, so
        // the first idle tick after walking has a fresh baseline. Keep the
        // signed delta, not just its magnitude — TRAVEL faces along it.
        var moveDx = 0.0
        var moveDz = 0.0
        val moved = if (hasLastPos) {
            moveDx = player.x - lastX
            moveDz = player.z - lastZ
            sqrt(moveDx * moveDx + moveDz * moveDz)
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
        if (System.currentTimeMillis() - lastFunctionalAimMs < HOLD_MS) return  // recent real aim

        if (moved > MOVE_THRESHOLD) {
            // TRAVEL: face the direction of motion, pitch level. Cosmetic —
            // Baritone's freeLook walk ignores head yaw (see class doc).
            if (!travelEnabled) return
            val targetYaw = (-Math.toDegrees(atan2(moveDx, moveDz))).toFloat()
            player.yaw = slew(player.yaw, targetYaw)
            player.pitch = slew(player.pitch, 0f)  // ease to a level horizon
            return
        }

        // IDLE: slew toward the nearest interesting entity.
        if (!idleEnabled) return
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
     *
     * Endermen are excluded: making eye contact with one is exactly what
     * aggravates it, so the cosmetic idle pan must never look at them.
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
            if (entity is EndermanEntity) continue  // eye contact aggros them
            val sq = player.squaredDistanceTo(entity)
            if (sq < bestSq) {
                bestSq = sq
                best = entity
            }
        }
        return best
    }
}
