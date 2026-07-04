package com.mineclaude.bridge

import com.mineclaude.bridge.mixin.FishingBobberAccessor
import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.ItemEntity
import net.minecraft.entity.projectile.FishingBobberEntity
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.sqrt

/**
 * `POST /fish {wait_s?, look_at_x/y/z?}` — cast a fishing rod, wait for a
 * bite, reel in, and report the catch. `POST /fish/stop` cancels an
 * in-flight session.
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
 *   2. Cast: aim at `look_at` if given (otherwise whatever direction the
 *      player already faces), then one `interactItem`. Confirm a bobber
 *      owned by the player actually spawned.
 *   3. **Landing check** (`bad_cast`): a raycast can't tell us where a
 *      projectile arcs to, and a bad aim (obstruction, no water in range,
 *      facing land) burned the FULL wait budget before this existed — the
 *      agent would wait 40s to learn the obvious. Instead we poll
 *      `isInOpenWater` for a couple seconds after the splash-down window;
 *      if it never lands in open water, retract and fail fast with
 *      `bad_cast` instead of silently timing out into a misleading `no_bite`.
 *   4. Poll (~100ms) the bobber's `caughtFish` flag until it flips true (a
 *      bite), the entity disappears (`lost` — cut short some other way), or
 *      `wait_s` elapses (`no_bite`).
 *   5. On a bite, reel in immediately (the vanilla catch window is only a
 *      couple of seconds). Vanilla catches don't teleport into the
 *      inventory — the loot spawns as a normal [ItemEntity] at the bobber
 *      with a velocity tossed toward the player, so a long-range cast can
 *      land the catch short of pickup range. We settle long enough for a
 *      close-range toss to actually reach the player and auto-pick-up, diff
 *      inventory counts, and if nothing landed but a freshly-spawned item
 *      exists nearby, report it honestly as `partial` (position + name)
 *      instead of a bare "caught" that hides the miss — the same
 *      truth-in-return discipline as the bucket routes' `verified` field.
 *      A `no_bite` timeout also reels in (so we don't leave the line out); a
 *      `lost` hook does NOT re-click (the hook's already gone server-side;
 *      clicking again would cast a fresh one and orphan it for the next call).
 *
 * Response shape: `{caught, reason, position, inventory_delta}` — always
 * present, even when empty, so the shape never silently drops a field the
 * agent might key on. `reason` is one of
 * `caught | no_bite | lost | bad_cast | cancelled | error`. Status is
 * `success` for `caught`/`cancelled`, `partial` when a bite was reeled in but
 * the loot didn't land in inventory (see above), `error` otherwise — mirrors
 * `/attack`: `no_bite`/`lost`/`bad_cast` are feedback the agent should act on
 * (recast elsewhere), not a crash.
 */
object FishingRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.fish")!!

    /** Bite-wait cadence. Fast enough to catch the few-second reel window. */
    private const val POLL_MS = 100L

    /** Let the cast's bobber-spawn packet + entity sync land before we look for it. */
    private const val CAST_SETTLE_MS = 250L

    /**
     * How long to give a fresh cast to actually splash down into open water
     * before declaring `bad_cast`. The bobber arcs through the air for a
     * moment first, so checking `isInOpenWater` immediately would false-fail
     * every cast; this window covers the flight, not the whole wait budget.
     */
    private const val LANDING_CHECK_MS = 2_000L
    private const val LANDING_POLL_MS = 150L

    /**
     * Let the reel's item-pickup packets land before diffing inventory. A
     * vanilla catch is tossed toward the player as a real [ItemEntity], not
     * granted directly — it takes real flight time to arrive and be
     * auto-picked-up, especially at range. Long enough for a close-range
     * toss to land; a long-range one may still miss (see [findFreshDrop]).
     */
    private const val REEL_SETTLE_MS = 1_500L

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

    private data class DroppedItem(val name: String, val position: List<Int>, val distance: Double)

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

        val anyLookKey = body.containsKey("look_at_x") || body.containsKey("look_at_y") || body.containsKey("look_at_z")
        val lookAt = parseLookAt(body)
        if (anyLookKey && lookAt == null) {
            return HttpBridge.err("look_at requires all three of look_at_x, look_at_y, look_at_z", status = 400)
        }

        cancelCurrent()
        val session = Session(Thread.currentThread(), AtomicBoolean(false))
        current.set(session)
        Thread.interrupted()

        try {
            return runFish(waitMs, lookAt, session)
        } finally {
            current.compareAndSet(session, null)
            Thread.interrupted()
        }
    }

    private fun parseLookAt(body: Map<String, Any?>): Vec3d? {
        val x = (body["look_at_x"] as? Number)?.toDouble()
        val y = (body["look_at_y"] as? Number)?.toDouble()
        val z = (body["look_at_z"] as? Number)?.toDouble()
        return if (x != null && y != null && z != null) Vec3d(x, y, z) else null
    }

    private fun runFish(waitMs: Long, lookAt: Vec3d?, session: Session): BridgeResponse {
        UseRoute.ensureMainhandHolds("fishing_rod")?.let { return HttpBridge.err(it) }

        retractStrayHook()

        val before = UseRoute.snapshotCounts()
        val preExistingItemIds = snapshotItemEntityIds()

        val castOk = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait false
            val mgr = mc.interactionManager ?: return@submitAndWait false
            WorldHelpers.ensureNoScreenOpen(player)
            if (lookAt != null) WorldHelpers.lookAtPosition(player, lookAt.x, lookAt.y, lookAt.z)
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

        if (!pollOpenWater(bobberId, LANDING_CHECK_MS)) {
            reel()
            return summary(
                "bad_cast", false, position = castPos,
                detail = "the hook didn't land in open water — it may have bounced off an obstacle or " +
                    "landed on dry ground. Stand at the water's edge and aim at open water (pass " +
                    "look_at_x/y/z at a water block, or reposition closer to a clear water body) and recast",
            )
        }

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

        reel()
        Thread.sleep(REEL_SETTLE_MS)
        val delta = UseRoute.computeDelta(before, UseRoute.snapshotCounts())

        return if (bit) {
            caughtResponse(castPos, delta, preExistingItemIds)
        } else {
            summary(
                "no_bite", false, position = castPos, delta = delta,
                detail = "no bite within ${waitMs / 1000}s",
            )
        }
    }

    /**
     * Bite confirmed + reeled. If the catch landed in inventory, report it
     * plainly. If not — a long-range toss can miss the player entirely —
     * look for a freshly-spawned [ItemEntity] near the player and report a
     * `partial` with its position rather than a bare "caught" that hides the
     * miss (the earlier version of this route did exactly that: `caught:
     * true` with an empty delta and a message that didn't say why).
     */
    private fun caughtResponse(
        position: List<Int>?,
        delta: Map<String, Int>,
        preExistingItemIds: Set<Int>,
    ): BridgeResponse {
        val data = mutableMapOf<String, Any>(
            "caught" to true,
            "reason" to "caught",
            "method" to "real",
            "inventory_delta" to delta,
        )
        position?.let { data["position"] = it }

        if (delta.isNotEmpty()) {
            val deltaStr = delta.entries.joinToString(", ") { "${if (it.value > 0) "+" else ""}${it.value} ${it.key}" }
            return HttpBridge.ok(data, "Caught something ($deltaStr)")
        }

        val dropped = findFreshDrop(preExistingItemIds)
        if (dropped != null) {
            data["dropped_at"] = dropped.position
            data["item"] = dropped.name
            return HttpBridge.partial(
                data,
                "Bit and reeled in a ${dropped.name}, but it landed ${"%.1f".format(dropped.distance)} " +
                    "blocks away instead of your inventory (at ${dropped.position}) — call collectItems() " +
                    "to grab it. Cast closer to where you stand so the toss reaches you directly",
            )
        }
        return HttpBridge.partial(
            data,
            "Bit and reeled in, but nothing landed in inventory and no freshly-dropped item was found " +
                "nearby — check getNearbyEntities() for a stray drop",
        )
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

    /**
     * Poll [bobberId]'s `isInOpenWater` for up to [budgetMs], returning true
     * the moment it settles into open water. False if it never does (bad
     * cast) or the bobber vanishes mid-check (treated as bad, not a bite —
     * the wait-for-bite loop starts fresh from a confirmed-landed bobber).
     */
    private fun pollOpenWater(bobberId: Int, budgetMs: Long): Boolean {
        val deadline = System.currentTimeMillis() + budgetMs
        while (System.currentTimeMillis() < deadline) {
            val state = TickThread.submitAndWait(timeoutMs = 1_000) {
                (MinecraftClient.getInstance().world?.getEntityById(bobberId) as? FishingBobberEntity)?.isInOpenWater
            }
            if (state == true) return true
            if (state == null) return false
            Thread.sleep(LANDING_POLL_MS)
        }
        return false
    }

    /** Tick-thread: ids of every [ItemEntity] currently in the world. */
    private fun snapshotItemEntityIds(): Set<Int> = TickThread.submitAndWait(timeoutMs = 1_000) {
        MinecraftClient.getInstance().world?.entities?.filterIsInstance<ItemEntity>()?.map { it.id }?.toSet()
            ?: emptySet()
    }

    /** Tick-thread: the nearest [ItemEntity] NOT in [preExisting] — a fresh drop. */
    private fun findFreshDrop(preExisting: Set<Int>): DroppedItem? = TickThread.submitAndWait(timeoutMs = 1_000) {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return@submitAndWait null
        val player = mc.player ?: return@submitAndWait null
        world.entities.filterIsInstance<ItemEntity>()
            .filter { it.id !in preExisting }
            .minByOrNull { it.squaredDistanceTo(player) }
            ?.let {
                DroppedItem(
                    name = Registries.ITEM.getId(it.stack.item).path,
                    position = listOf(it.x.toInt(), it.y.toInt(), it.z.toInt()),
                    distance = sqrt(it.squaredDistanceTo(player)),
                )
            }
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
            "inventory_delta" to delta,
        )
        position?.let { data["position"] = it }
        val msg = when (reason) {
            "no_bite" -> detail ?: "No bite"
            "lost" -> detail ?: "Lost the hook"
            "bad_cast" -> detail ?: "Bad cast"
            "cancelled" -> "Fishing cancelled"
            "error" -> "Fishing errored: ${detail ?: "unknown"}"
            else -> reason
        }
        val ok = reason == "cancelled"
        return if (ok) HttpBridge.ok(data, msg) else BridgeResponse("error", msg, data, 200)
    }
}
