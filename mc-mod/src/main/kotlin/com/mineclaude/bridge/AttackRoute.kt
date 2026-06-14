package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.Entity
import net.minecraft.entity.LivingEntity
import net.minecraft.network.packet.c2s.play.UpdateSelectedSlotC2SPacket
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import org.slf4j.LoggerFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.floor

/**
 * `POST /attack` — fight an entity until it's dead, despawned, out of reach,
 * or the call is cancelled.
 *
 * # Pathfinder + combat module (the "multitask" model)
 *
 * A competent melee is two subsystems running at once: a *pathfinder* that
 * owns movement (and is smart about terrain — jumps, gaps, corners, walls)
 * and a *combat module* that owns aim + attack timing. We split it the same
 * way instead of fusing a dumb "walk straight at it" mover into the swing
 * loop (which got stuck on every wall, fence, gap, and pit).
 *
 * Movement is Baritone's job at *every* range. The trick the old code missed
 * is to track the *moving* target continuously: we re-issue `#goto <the
 * target's live coords>` whenever it drifts ([GOAL_REFRESH_DELTA]) or the
 * goal goes stale ([GOAL_REFRESH_MS]) — a dynamic follow-goal emulated over
 * the chat-command surface (Baritone has no `#follow <entity-id>`). Baritone
 * repaths around obstacles on its own ticks; we never block on "arrival".
 *
 * Concurrently, the worker loop (~[TICK_POLL_MS]) is the combat module:
 *  1. Re-resolve the target; bail on dead / despawn / not-found.
 *  2. Within [MELEE_REACH] → halt Baritone (so it doesn't shove past the
 *     mob) and swing on the [SWING_INTERVAL_MS] full-damage cadence.
 *  3. Out of reach → keep Baritone tracking the live position; if we can't
 *     get any closer for [STUCK_MS] (faster mob / walled off), give up with
 *     `out_of_reach`.
 *  [RESUME_REACH] adds hysteresis so a mob hovering at the edge of reach
 *  doesn't thrash Baritone start/stop every tick.
 *
 * We do NOT override the head rotation while Baritone is steering — and we
 * don't need to: `interactionManager.attackEntity(player, target)` hits the
 * explicit entity, not a crosshair raycast. We only aim at the swing moment
 * (when Baritone is halted) for a faithful recording + server-side leniency.
 *
 * Held-slot guard: Baritone swaps the hotbar to a throwaway block while
 * pathing, which would gut melee damage. We re-hold the agent's weapon slot
 * immediately before each swing and restore it in the outer `finally`.
 *
 * Looping (rather than one-shot) keeps a single high-level "kill this thing"
 * call cheap from the agent's perspective: one HTTP request per fight, not
 * one per swing. The reflex layer relies on this — the damage_taken
 * retaliate path issues one `/attack` and the bridge fights to a
 * conclusion. A new reflex event cancels the in-flight loop via
 * [`/attack/stop`] (driven from `_pre_interrupt_stop_bridge` on the agent
 * side).
 *
 * Cancellation: any new `/attack` POST or explicit `POST /attack/stop`
 * flips the active session's `cancelled` flag and interrupts its worker
 * thread (so `Thread.sleep` between ticks unblocks immediately). The
 * worker re-checks the flag at every loop boundary and after sleep
 * interruption, then returns a `cancelled` summary.
 *
 * Response shape: `{attacked, swings, reason, method}` where reason is
 * one of `killed | despawned | out_of_reach | cancelled | timeout |
 * not_found`. Status is `success` for any outcome where we engaged
 * (landed ≥1 swing) or the target died; `error` only if we couldn't even
 * find/reach the entity to start.
 */
object AttackRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.attack")!!

    /**
     * ms between swings. Sword full-damage cooldown is 1.6 atk/sec ≈ 625 ms;
     * spamming faster halves damage. Keeping the bridge at full-damage cadence
     * minimizes total fight time despite the latency.
     */
    private const val SWING_INTERVAL_MS = 625L

    /**
     * Per-tick cadence of the combat loop. Baritone drives movement on its
     * own client ticks; this loop only re-resolves the target, refreshes the
     * goal, and swings when in reach — so ~100 ms is plenty and keeps the
     * tick-thread submission rate modest.
     */
    private const val TICK_POLL_MS = 100L

    /** Hard cap on a single fight — pathological loops shouldn't exceed this. */
    private const val SESSION_TIMEOUT_MS = 30_000L

    /** Melee reach (vanilla 3-block + 0.5 forgiveness, mirrors prior one-shot impl). */
    private const val MELEE_REACH = 3.5

    /**
     * Resume pathing once the target drifts past this (> [MELEE_REACH]).
     * The gap between the two is hysteresis: a mob jittering right at the
     * edge of reach won't make us spam Baritone #goto/#stop every tick.
     */
    private const val RESUME_REACH = 4.5

    /**
     * Re-issue `#goto` when the target's floored position moves at least this
     * far from the goal we last sent. Tracks a moving mob without recomputing
     * Baritone's path every single tick.
     */
    private const val GOAL_REFRESH_DELTA = 2.0

    /** Backstop: refresh the goal at least this often while chasing. */
    private const val GOAL_REFRESH_MS = 1_000L

    /**
     * Give up with `out_of_reach` if we go this long without getting any
     * closer while out of melee range — a mob faster than us, or one Baritone
     * can't path to (walled off, treed up). Replaces the old per-nav retry
     * counter with a progress-based stall check on the continuous chase.
     */
    private const val STUCK_MS = 6_000L

    /** Min distance improvement (blocks) that counts as "making progress". */
    private const val STUCK_IMPROVE_EPS = 0.5

    private data class Session(val thread: Thread, val cancelled: AtomicBoolean)

    /** At most one in-flight attack session at a time. */
    private val current = AtomicReference<Session?>(null)

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/attack") { ex -> handle(ex) }
        bridge.addRoute("POST", "/attack/stop") { _ -> handleStop() }
    }

    private fun handleStop(): BridgeResponse {
        val cancelled = cancelCurrent()
        return HttpBridge.ok(
            mapOf("cancelled" to cancelled),
            if (cancelled) "Attack cancelled" else "No attack in progress",
        )
    }

    /** Returns true if a session was cancelled. */
    private fun cancelCurrent(): Boolean {
        val s = current.getAndSet(null) ?: return false
        s.cancelled.set(true)
        // Interrupt unblocks the inter-swing Thread.sleep so the worker
        // exits within ~ms of cancel rather than the next swing tick.
        s.thread.interrupt()
        return true
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val entityId = (body["entity_id"] as? String).orEmpty()
        if (entityId.isEmpty()) {
            return HttpBridge.err("Missing 'entity_id' parameter", status = 400)
        }

        // Newest call wins. Cancel any prior loop before claiming the slot.
        cancelCurrent()
        val session = Session(Thread.currentThread(), AtomicBoolean(false))
        current.set(session)

        // Defensive: clear any stale interrupt flag on this thread (HttpServer
        // reuses worker threads, and a prior cancel could have left the bit
        // set without an intervening sleep to consume it).
        Thread.interrupted()

        try {
            return runLoop(entityId, session)
        } finally {
            // Only clear the slot if we still own it; another /attack may
            // have already replaced us.
            current.compareAndSet(session, null)
            // Eat any lingering interrupt before returning to the pool.
            Thread.interrupted()
        }
    }

    private fun runLoop(entityId: String, session: Session): BridgeResponse {
        val deadline = System.currentTimeMillis() + SESSION_TIMEOUT_MS
        var swings = 0
        var everEngaged = false  // saw the entity in-world at least once?
        var lastSwingMs = 0L     // 0 ⇒ first in-reach tick swings immediately

        // Movement state: Baritone is the mover, driven by a refreshed #goto
        // that tracks the target's live position (a dynamic follow-goal).
        var pathing = false
        var haveGoal = false
        var goalX = 0; var goalY = 0; var goalZ = 0
        var lastGoalMs = 0L

        // Progress-based stall check: bail if we can't get any closer.
        var bestDist = Double.MAX_VALUE
        var lastImproveMs = System.currentTimeMillis()

        // Baritone leaks the held hotbar slot while pathing; snapshot the
        // agent's weapon slot so we can re-hold it before each swing (full
        // melee damage) and restore it on exit.
        val weaponSlot = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.inventory?.selectedSlot ?: 0
        }

        try {
            while (true) {
                if (session.cancelled.get()) return summary("cancelled", swings, entityId)
                val nowMs = System.currentTimeMillis()
                if (nowMs >= deadline) return summary("timeout", swings, entityId)

                // submitAndWait throws on tick-thread timeout; the dispatcher
                // returns a 500 and the outer finally clears the slot + stops
                // Baritone + restores the held weapon.
                when (val t = TickThread.submitAndWait(timeoutMs = 2_000) { resolveTargetOnTick(entityId) }) {
                    is Target.NoPlayer -> return summary("error", swings, entityId, detail = "no player")
                    is Target.NotFound -> {
                        return if (everEngaged) summary("despawned", swings, entityId)
                        else summary("not_found", swings, entityId)
                    }
                    is Target.Dead -> {
                        everEngaged = true
                        return summary("killed", swings, entityId)
                    }
                    is Target.Alive -> {
                        everEngaged = true
                        if (t.dist < bestDist - STUCK_IMPROVE_EPS) {
                            bestDist = t.dist
                            lastImproveMs = nowMs
                        }

                        when {
                            t.dist <= MELEE_REACH -> {
                                // In reach: halt Baritone so it doesn't shove
                                // past the mob, then swing on the cadence.
                                if (pathing) { stopBaritone(); pathing = false }
                                lastImproveMs = nowMs  // engaged ⇒ progressing
                                if (nowMs - lastSwingMs >= SWING_INTERVAL_MS) {
                                    val swung = TickThread.submitAndWait(timeoutMs = 2_000) {
                                        swingOnTick(entityId, weaponSlot)
                                    }
                                    if (swung) { swings += 1; lastSwingMs = nowMs }
                                }
                            }
                            t.dist <= RESUME_REACH && !pathing -> {
                                // Hysteresis band while halted — hold, don't
                                // thrash Baritone for a mob hovering at reach.
                            }
                            else -> {
                                // Out of reach: keep Baritone tracking the live
                                // target position (refresh on drift / staleness).
                                val gx = floor(t.x).toInt()
                                val gy = floor(t.y).toInt()
                                val gz = floor(t.z).toInt()
                                val moved = !haveGoal || run {
                                    val dx = (gx - goalX).toDouble()
                                    val dy = (gy - goalY).toDouble()
                                    val dz = (gz - goalZ).toDouble()
                                    dx * dx + dy * dy + dz * dz > GOAL_REFRESH_DELTA * GOAL_REFRESH_DELTA
                                }
                                if (!pathing || moved || nowMs - lastGoalMs > GOAL_REFRESH_MS) {
                                    sendBaritone("#goto $gx $gy $gz")
                                    goalX = gx; goalY = gy; goalZ = gz
                                    haveGoal = true; lastGoalMs = nowMs; pathing = true
                                }
                                if (nowMs - lastImproveMs > STUCK_MS) {
                                    return summary(
                                        "out_of_reach", swings, entityId,
                                        detail = "no progress for ${STUCK_MS / 1000}s — " +
                                            "target faster than us or unreachable (walled off / treed up)",
                                    )
                                }
                            }
                        }

                        try {
                            Thread.sleep(TICK_POLL_MS)
                        } catch (_: InterruptedException) {
                            if (session.cancelled.get()) return summary("cancelled", swings, entityId)
                            // Spurious interrupt: re-loop and re-check.
                        }
                    }
                }
            }
        } finally {
            // Stop Baritone (it keeps pathing otherwise) and give the agent
            // back the weapon slot Baritone may have swapped away mid-path.
            if (pathing) stopBaritone()
            restoreSlot(weaponSlot)
        }
    }

    /** Live target read on the tick thread. */
    private sealed interface Target {
        data object NoPlayer : Target
        data object NotFound : Target
        data object Dead : Target
        data class Alive(val x: Double, val y: Double, val z: Double, val dist: Double) : Target
    }

    private fun resolveTargetOnTick(entityId: String): Target {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return Target.NoPlayer
        val world = mc.world ?: return Target.NoPlayer
        WorldHelpers.ensureNoScreenOpen(player)

        val target = matchEntity(world.entities, entityId) ?: return Target.NotFound
        if (!target.isAlive || target.isRemoved) return Target.Dead
        if (target is LivingEntity && target.health <= 0f) return Target.Dead

        val dist = WorldHelpers.playerDistance(player, target.x, target.y, target.z)
        return Target.Alive(target.x, target.y, target.z, dist)
    }

    /**
     * Swing at the target on the tick thread. Re-holds [weaponSlot] first —
     * Baritone may have swapped the hotbar to a throwaway block while pathing,
     * which would land the hit bare-handed. Aims at the body centre (we only
     * touch rotation here, when Baritone is halted, so we don't fight its
     * steering). Returns true if a swing was issued.
     */
    private fun swingOnTick(entityId: String, weaponSlot: Int): Boolean {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return false
        val world = mc.world ?: return false
        val mgr = mc.interactionManager ?: return false
        val target = matchEntity(world.entities, entityId) ?: return false
        if (weaponSlot in 0..8 && player.inventory.selectedSlot != weaponSlot) {
            player.inventory.selectedSlot = weaponSlot
            player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(weaponSlot))
        }
        val centre = target.boundingBox.center
        WorldHelpers.lookAtPosition(player, centre.x, centre.y, centre.z)
        mgr.attackEntity(player, target)
        player.swingHand(Hand.MAIN_HAND)
        return true
    }

    /** Send a Baritone `#…` chat command (intercepted client-side before send). */
    private fun sendBaritone(cmd: String) {
        TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.networkHandler?.sendChatMessage(cmd)
            Unit
        }
    }

    private fun stopBaritone() = sendBaritone("#stop")

    /** Re-hold [slot] after Baritone may have swapped it; tick-paced. */
    private fun restoreSlot(slot: Int) {
        try {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                val player = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
                if (slot in 0..8 && player.inventory.selectedSlot != slot) {
                    player.inventory.selectedSlot = slot
                    player.networkHandler.sendPacket(UpdateSelectedSlotC2SPacket(slot))
                }
                Unit
            }
        } catch (t: Throwable) {
            log.warn("attack: failed to restore held slot {}: {}", slot, t.message)
        }
    }

    private fun matchEntity(entities: Iterable<Entity>, query: String): Entity? {
        // Numeric id path — used by the damage_taken reflex to retaliate
        // against the exact attacker. Skip the name/type loop entirely so
        // a stringified id can't accidentally substring-match a mob name.
        query.toIntOrNull()?.let { id ->
            for (entity in entities) {
                if (entity is net.minecraft.client.network.ClientPlayerEntity) continue
                if (entity.id == id) return entity
            }
            return null
        }
        val q = query.lowercase()
        for (entity in entities) {
            if (entity is net.minecraft.client.network.ClientPlayerEntity) continue
            val name = entity.name.string.lowercase().removePrefix("minecraft:")
            val type = Registries.ENTITY_TYPE.getId(entity.type).path.lowercase()
            if (q == name || q == type) return entity
            if (q in name || q in type) return entity
        }
        return null
    }

    private fun summary(
        reason: String,
        swings: Int,
        entityId: String,
        detail: String? = null,
    ): BridgeResponse {
        val data = mapOf(
            "attacked" to (swings > 0),
            "swings" to swings,
            "reason" to reason,
            "method" to "real",
        )
        val msg = when (reason) {
            "killed" -> "Killed $entityId in $swings swings"
            "despawned" -> "Target $entityId despawned after $swings swings"
            "out_of_reach" -> buildString {
                append("Couldn't keep up with $entityId after $swings swings")
                if (detail != null) append(": $detail")
            }
            "cancelled" -> "Attack cancelled after $swings swings"
            "timeout" -> "Attack timed out after $swings swings (${SESSION_TIMEOUT_MS / 1000}s)"
            "not_found" -> "Entity $entityId not found"
            "error" -> "Attack errored: ${detail ?: "unknown"}"
            else -> reason
        }
        // Contract: the ask is "kill this entity". Success only when killed
        // or when we were preempted on purpose (cancelled). Everything else
        // — despawn mid-fight, ran out of reach, timed out, never found —
        // is an error the agent should react to. The `data` payload tells
        // the agent how much progress was made.
        val ok = reason == "killed" || reason == "cancelled"
        return if (ok) HttpBridge.ok(data, msg) else BridgeResponse("error", msg, data, 200)
    }
}
