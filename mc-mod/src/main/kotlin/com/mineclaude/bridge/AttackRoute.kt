package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.Entity
import net.minecraft.entity.LivingEntity
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * `POST /attack` — fight an entity until it's dead, despawned, out of reach,
 * or the call is cancelled. Each iteration: tick-thread re-resolves the
 * target by id (or name), checks alive + reach, navigates if needed via
 * Baritone, then swings on the tick thread via
 * [`ClientPlayerInteractionManager.attackEntity`].
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
 * thread (so `Thread.sleep` between swings unblocks immediately). The
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

    /** Hard cap on a single fight — pathological loops shouldn't exceed this. */
    private const val SESSION_TIMEOUT_MS = 30_000L

    /**
     * Consecutive nav failures before we give up on closing on the target.
     * 1 retry is enough to absorb a one-off "Baritone stalled because the
     * mob jumped" without letting a fleeing enemy soak the full timeout.
     */
    private const val MAX_NAV_FAILURES = 2

    /** Melee reach (vanilla 3-block + 0.5 forgiveness, mirrors prior one-shot impl). */
    private const val MELEE_REACH = 3.5

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
        var navFailures = 0
        var everEngaged = false  // saw the entity in-world at least once?

        while (true) {
            if (session.cancelled.get()) return summary("cancelled", swings, entityId)
            if (System.currentTimeMillis() >= deadline) return summary("timeout", swings, entityId)

            // submitAndWait throws on tick-thread timeout; the dispatcher
            // catches the throw and returns a 500. That's the right outcome
            // for an unresponsive client tick — the session slot is cleared
            // in the outer finally before the throw propagates.
            val state = TickThread.submitAndWait(timeoutMs = 2_000) { lookupOnTick(entityId) }

            when (state) {
                is LookupState.NoPlayer -> return summary("error", swings, entityId, detail = "no player")
                is LookupState.NotFound -> {
                    return if (everEngaged) summary("despawned", swings, entityId)
                    else summary("not_found", swings, entityId)
                }
                is LookupState.Dead -> {
                    everEngaged = true
                    return summary("killed", swings, entityId)
                }
                is LookupState.OutOfReach -> {
                    everEngaged = true
                    if (session.cancelled.get()) return summary("cancelled", swings, entityId)
                    val nav = Navigation.navigateNear(
                        BlockPos.ofFloored(state.x, state.y, state.z),
                        reach = 2.5,
                    )
                    if (nav is Navigation.Result.Failed) {
                        navFailures += 1
                        if (navFailures >= MAX_NAV_FAILURES) {
                            return summary("out_of_reach", swings, entityId, detail = nav.reason)
                        }
                        // Re-resolve and try again — entity may have moved
                        // back into reach during the partial nav attempt.
                    } else {
                        navFailures = 0
                    }
                    // Loop and re-resolve (no inter-swing sleep yet).
                }
                is LookupState.Ready -> {
                    everEngaged = true
                    navFailures = 0
                    val swung = TickThread.submitAndWait(timeoutMs = 2_000) { swingOnTick(entityId) }
                    if (swung) swings += 1

                    try {
                        Thread.sleep(SWING_INTERVAL_MS)
                    } catch (_: InterruptedException) {
                        // Sleep interrupted — almost certainly a cancel.
                        // Re-check and exit cleanly.
                        if (session.cancelled.get()) return summary("cancelled", swings, entityId)
                        // Spurious interrupt: keep going. (Restore the flag
                        // is unnecessary here — we re-loop and will re-check.)
                    }
                }
            }
        }
    }

    private sealed interface LookupState {
        data object NoPlayer : LookupState
        data object NotFound : LookupState
        data object Dead : LookupState
        data class OutOfReach(val x: Double, val y: Double, val z: Double) : LookupState
        data class Ready(val id: Int) : LookupState
    }

    private fun lookupOnTick(entityId: String): LookupState {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return LookupState.NoPlayer
        val world = mc.world ?: return LookupState.NoPlayer
        WorldHelpers.ensureNoScreenOpen(player)

        val target = matchEntity(world.entities, entityId) ?: return LookupState.NotFound
        if (!target.isAlive || target.isRemoved) return LookupState.Dead
        if (target is LivingEntity && target.health <= 0f) return LookupState.Dead

        val dist = WorldHelpers.playerDistance(player, target.x, target.y, target.z)
        return if (dist > MELEE_REACH) LookupState.OutOfReach(target.x, target.y, target.z)
        else LookupState.Ready(target.id)
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

    private fun swingOnTick(entityId: String): Boolean {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return false
        val world = mc.world ?: return false
        val mgr = mc.interactionManager ?: return false
        val target = matchEntity(world.entities, entityId) ?: return false
        WorldHelpers.lookAtPosition(player, target.x, target.y, target.z)
        mgr.attackEntity(player, target)
        player.swingHand(Hand.MAIN_HAND)
        return true
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
