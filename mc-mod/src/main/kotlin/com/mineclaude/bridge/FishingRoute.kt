package com.mineclaude.bridge

import com.mineclaude.bridge.mixin.FishingBobberAccessor
import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.projectile.FishingBobberEntity
import net.minecraft.util.Hand
import org.slf4j.LoggerFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * `POST /fish {wait_s?}` — cast a fishing rod, wait for a bite, reel in, and
 * report the catch. `POST /fish/stop` cancels an in-flight session.
 *
 * # Why this needs a mixin
 *
 * A right-click with a rod (`interactionManager.interactItem`) is the same
 * gesture for both casting and reeling — [FishingRodItem.use] toggles on
 * whether the player already has an active hook (server-side state we can't
 * read directly). The one signal that matters — "a fish actually bit" — is
 * [FishingBobberEntity.caughtFish], backed by a synced `TrackedData<Boolean>`
 * but exposed with no public getter. Rather than guess from the bobber's
 * velocity (a fragile heuristic — the real flag is synced and just needs
 * unhiding), [FishingBobberAccessor] is an `@Accessor` mixin that reads the
 * private field directly, the same pattern the death-detection mixins in this
 * package already use for other unexposed client state.
 *
 * # Lifecycle
 *   1. Equip a fishing rod. Retract any stray hook left by a previous
 *      crashed/interrupted session first — otherwise the next click would
 *      reel *that* in instead of casting fresh.
 *   2. Cast: one `interactItem`. Confirm a bobber owned by the player
 *      actually spawned (else the rod/click failed outright).
 *   3. Poll (~100ms) the bobber's `caughtFish` flag until it flips true (a
 *      bite), the entity disappears (`lost` — cut short some other way), or
 *      `wait_s` elapses (`no_bite`).
 *   4. On a bite, reel in immediately (the vanilla catch window is only a
 *      couple of seconds) and verify the catch via an inventory-count diff —
 *      the same truth-in-return pattern as the bucket routes. A `no_bite` timeout
 *      also reels in (so we don't leave the line out); a `lost` hook does
 *      NOT re-click (the hook's already gone server-side; clicking again
 *      would cast a fresh one and orphan it for the next call).
 *
 * Response shape: `{caught, reason, position, inventory_delta}`, reason one
 * of `caught | no_bite | lost | cancelled | error`. Status is `success` only
 * for `caught`/`cancelled` (mirrors `/attack`) — `no_bite`/`lost` are
 * feedback the agent should act on (recast elsewhere), not a crash.
 */
object FishingRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.fish")!!

    /** Bite-wait cadence. Fast enough to catch the few-second reel window. */
    private const val POLL_MS = 100L

    /** Let the cast's bobber-spawn packet + entity sync land before we look for it. */
    private const val CAST_SETTLE_MS = 250L

    /** Let the reel's item-pickup packets land before diffing inventory. */
    private const val REEL_SETTLE_MS = 200L

    /** Vanilla's un-Lure'd wait is 5-30s; this default covers the worst case with margin. */
    private const val DEFAULT_WAIT_S = 40.0

    /**
     * Hard cap on `wait_s`. Must leave headroom under the bridge client's 90s
     * HTTP timeout (cast + settle + reel + network all eat into the same
     * request) — see the `_HALT_TIMEOUT_S` comment in the Python bridge for
     * the deadlock this class of bug caused before. A longer wait is a loop
     * of `/fish` calls on the agent side, not one giant blocking call.
     */
    private const val MAX_WAIT_S = 60.0

    private data class Session(val thread: Thread, val cancelled: AtomicBoolean)

    /** At most one in-flight fishing session at a time. */
    private val current = AtomicReference<Session?>(null)

    private enum class BobberState { WAITING, CAUGHT, GONE }

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/fish") { ex -> handle(ex) }
        bridge.addRoute("POST", "/fish/stop") { _ -> handleStop() }
    }

    private fun handleStop(): BridgeResponse {
        val cancelled = cancelCurrent()
        return HttpBridge.ok(
            mapOf("cancelled" to cancelled),
            if (cancelled) "Fishing cancelled" else "Not fishing",
        )
    }

    private fun cancelCurrent(): Boolean {
        val s = current.getAndSet(null) ?: return false
        s.cancelled.set(true)
        s.thread.interrupt()
        return true
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val waitS = (body["wait_s"] as? Number)?.toDouble() ?: DEFAULT_WAIT_S
        if (waitS <= 0) return HttpBridge.err("wait_s must be > 0", status = 400)
        val waitMs = (waitS.coerceAtMost(MAX_WAIT_S) * 1000).toLong()

        cancelCurrent()
        val session = Session(Thread.currentThread(), AtomicBoolean(false))
        current.set(session)
        Thread.interrupted()

        try {
            return runFish(waitMs, session)
        } finally {
            current.compareAndSet(session, null)
            Thread.interrupted()
        }
    }

    private fun runFish(waitMs: Long, session: Session): BridgeResponse {
        UseRoute.ensureMainhandHolds("fishing_rod")?.let { return HttpBridge.err(it) }

        retractStrayHook()

        val before = UseRoute.snapshotCounts()

        val castOk = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait false
            val mgr = mc.interactionManager ?: return@submitAndWait false
            WorldHelpers.ensureNoScreenOpen(player)
            mgr.interactItem(player, Hand.MAIN_HAND)
            player.swingHand(Hand.MAIN_HAND)
            true
        }
        if (!castOk) return summary("error", false, detail = "no player/world to cast with")

        Thread.sleep(CAST_SETTLE_MS)

        val bobberId = TickThread.submitAndWait(timeoutMs = 1_000) { findOwnedBobberId() }
        if (bobberId == null) {
            return summary(
                "error", false,
                detail = "cast didn't spawn a bobber — check the rod isn't broken and there's room to cast",
            )
        }
        val castPos = TickThread.submitAndWait(timeoutMs = 1_000) { bobberPosition(bobberId) }

        var bit = false
        var lost = false
        val deadline = System.currentTimeMillis() + waitMs
        while (true) {
            if (session.cancelled.get()) {
                reel()
                return summary("cancelled", false, position = castPos)
            }
            if (System.currentTimeMillis() >= deadline) break
            when (TickThread.submitAndWait(timeoutMs = 1_000) { checkBobber(bobberId) }) {
                BobberState.CAUGHT -> { bit = true; break }
                BobberState.GONE -> { lost = true; break }
                BobberState.WAITING -> { }
            }
            try {
                Thread.sleep(POLL_MS)
            } catch (_: InterruptedException) {
                if (session.cancelled.get()) {
                    reel()
                    return summary("cancelled", false, position = castPos)
                }
            }
        }

        if (lost) {
            return summary(
                "lost", false, position = castPos,
                detail = "the hook disappeared before biting (line cut or broke) — cast again",
            )
        }

        val openWater = TickThread.submitAndWait(timeoutMs = 1_000) {
            (MinecraftClient.getInstance().world?.getEntityById(bobberId) as? FishingBobberEntity)?.isInOpenWater
        }

        reel()
        Thread.sleep(REEL_SETTLE_MS)
        val delta = UseRoute.computeDelta(before, UseRoute.snapshotCounts())

        return if (bit) {
            summary("caught", true, position = castPos, delta = delta)
        } else {
            val hint = if (openWater == false) {
                " — the hook wasn't in open water; recast into a clear water body"
            } else ""
            summary(
                "no_bite", false, position = castPos, delta = delta,
                detail = "no bite within ${waitMs / 1000}s$hint",
            )
        }
    }

    /**
     * Retract a hook left over from a previous crashed/interrupted `/fish`
     * call before casting — otherwise the cast click would reel *that* one
     * in instead of throwing a fresh line.
     */
    private fun retractStrayHook() {
        val stray = TickThread.submitAndWait(timeoutMs = 1_000) { findOwnedBobberId() } ?: return
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            mgr.interactItem(player, Hand.MAIN_HAND)
            player.swingHand(Hand.MAIN_HAND)
            Unit
        }
        Thread.sleep(CAST_SETTLE_MS)
        log.info("fish: retracted a stray hook (id {}) from a previous session before casting", stray)
    }

    /** Tick-thread: the id of the bobber owned by the player, if any. */
    private fun findOwnedBobberId(): Int? {
        val player = MinecraftClient.getInstance().player ?: return null
        val world = MinecraftClient.getInstance().world ?: return null
        return world.entities.firstOrNull { it is FishingBobberEntity && it.playerOwner === player }?.id
    }

    /** Tick-thread: current world position of entity [id], floored. */
    private fun bobberPosition(id: Int): List<Int>? {
        val world = MinecraftClient.getInstance().world ?: return null
        val e = world.getEntityById(id) ?: return null
        return listOf(e.x.toInt(), e.y.toInt(), e.z.toInt())
    }

    /** Tick-thread: has bobber [id] hooked a bite, disappeared, or still waiting? */
    private fun checkBobber(id: Int): BobberState {
        val world = MinecraftClient.getInstance().world ?: return BobberState.GONE
        val e = world.getEntityById(id)
        if (e !is FishingBobberEntity) return BobberState.GONE
        return if ((e as FishingBobberAccessor).mineclaude_getCaughtFish()) BobberState.CAUGHT else BobberState.WAITING
    }

    /** One more `interactItem` — reels in an active hook. Best-effort. */
    private fun reel() {
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            mgr.interactItem(player, Hand.MAIN_HAND)
            player.swingHand(Hand.MAIN_HAND)
            Unit
        }
    }

    private fun summary(
        reason: String,
        caught: Boolean,
        position: List<Int>? = null,
        delta: Map<String, Int> = emptyMap(),
        detail: String? = null,
    ): BridgeResponse {
        val data = mutableMapOf<String, Any>(
            "caught" to caught,
            "reason" to reason,
            "method" to "real",
        )
        position?.let { data["position"] = it }
        if (delta.isNotEmpty()) data["inventory_delta"] = delta
        val deltaStr = if (delta.isNotEmpty()) {
            " (" + delta.entries.joinToString(", ") { "${if (it.value > 0) "+" else ""}${it.value} ${it.key}" } + ")"
        } else ""
        val msg = when (reason) {
            "caught" -> "Caught something$deltaStr"
            "no_bite" -> detail ?: "No bite"
            "lost" -> detail ?: "Lost the hook"
            "cancelled" -> "Fishing cancelled"
            "error" -> "Fishing errored: ${detail ?: "unknown"}"
            else -> reason
        }
        val ok = reason == "caught" || reason == "cancelled"
        return if (ok) HttpBridge.ok(data, msg) else BridgeResponse("error", msg, data, 200)
    }
}
