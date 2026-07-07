package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory
import kotlin.math.sqrt

/**
 * Autonomic shield-vs-projectile reflex — raise the offhand guard against
 * incoming hostile fire (a skeleton's arrow, a blaze's fireball) and lower it
 * once the volley passes, entirely on the client tick thread.
 *
 * # Why it lives here, not in the Python reflex layer
 * Driving the bridge from a Python reflex means *preempting* — cancelling the
 * running action to take the single-flight slot. A momentary guard-raise
 * shouldn't derail a mine / walk / idle. Running inside END_CLIENT_TICK
 * sidesteps the slot entirely: we call `interactItem` / `useKey` directly (no
 * [TickThread.submitAndWait] — we are already on the tick thread), the very
 * same primitives [ShieldRoute] and [AttackRoute] use. It never touches the
 * action queue, never preempts, never wakes the agent. This is the
 * generalization of the block-strike rhythm [AttackRoute] already runs between
 * swings, lifted out to fire *outside* a combat call.
 *
 * # Ownership (the arbiter)
 * The use-key has deliberate owners: the melee block-rhythm, the bow draw
 * ([AttackRoute]), and standalone [ShieldRoute]. Auto-shield is the lowest-
 * priority consumer — it stands down whenever any is active
 * ([UseKeyArbiter.claimedByOther]) so it never clobbers a deliberate block or
 * draw. That leaves its domain exactly the gap those don't cover: not in a
 * combat call, not in a `/block` — the bot mining, walking, or waiting on the
 * agent to decide.
 *
 * # v1 constraints (deliberate)
 *  - **Shield must already be in the offhand.** Auto-equip needs
 *    [EquipRoute.ensureOffhand], which does its own [TickThread.submitAndWait]
 *    and so can't run from inside the tick (it would deadlock). Requiring a
 *    pre-equipped shield keeps v1 correct; `/block`, the creeper reflex, and
 *    the melee loop all park a shield in the offhand anyway.
 *  - **Suppressed while the bot is being actively steered** (sustained motion).
 *    Blocking throttles movement to ~20% and re-facing a threat fights whoever
 *    owns rotation (Baritone), so auto-shield is a stationary-first guard. A
 *    single knockback impulse decays out of the motion EMA within a few ticks,
 *    so it does not read as locomotion.
 *  - **One direction at a time** — a shield only covers the way you face, so we
 *    face the most recent threat.
 *  - We only re-face when the current look is well off the threat, to minimize
 *    fighting a stationary rotation owner (a `/break`/`/place` in flight).
 *
 * # Trigger
 * Phase 1 (this): **reactive** — [EventBus]'s damage_taken branch calls
 * [onRangedDamage] when a hit came from a ranged hostile, so we guard the rest
 * of the volley (the first arrow is already spent by the time health drops).
 * Phase 2 will add a pre-impact projectile-trajectory scan feeding the same
 * [pendingThreat], so the first shot is blocked too.
 *
 * Default on (disable with `AUTO_SHIELD=0`); toggled at runtime via
 * `POST /autoshield {enabled}`.
 */
object AutoShield {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.autoshield")!!

    /** Default-on; `AUTO_SHIELD=0` opts out (mirrors CameraDirector's gate).
     *  Read on the tick thread, written by the toggle route's worker thread. */
    @Volatile private var enabled = System.getenv("AUTO_SHIELD") != "0"

    /** Are WE holding the guard up right now? (Distinct from `player.isBlocking`,
     *  which is also true when a deliberate owner blocks.) Tick-thread only. */
    private var raised = false

    // Threat state — the world position to face while guarding, and when it was
    // staged, so a stale threat (bot walked off, mob gone) expires rather than
    // triggering a pointless raise minutes later. Tick-thread confined.
    private var pendingThreat: Vec3d? = null
    private var pendingKind: String? = null
    private var pendingThreatTick = 0L

    // Hold the guard until this tick, refreshed on every fresh threat so a
    // volley keeps it up without per-tick flapping. Skeletons fire ~every
    // 20-40 ticks; the debounce spans the gap between shots.
    private var holdUntilTick = 0L
    private var tickCount = 0L

    // Sustained-motion detector. EMA of per-tick horizontal displacement — a
    // one-tick knockback blip won't cross the threshold, a Baritone walk does.
    private var lastX = Double.NaN
    private var lastZ = Double.NaN
    private var speedEma = 0.0

    private const val HOLD_DEBOUNCE_TICKS = 15L   // ~0.75s past the last threat
    private const val THREAT_TTL_TICKS = 40L      // drop a threat unacted-on for ~2s
    private const val MOVING_EMA_EPS = 0.055      // blocks/tick — a sustained walk clears this
    private const val EMA_ALPHA = 0.45
    private const val REFACE_DOT = 0.5            // re-face only if look is >~60° off the threat

    /** Ranged-hostile attacker kinds (entity path ids, no `minecraft:`) whose
     *  hits mean "under fire — guard up". Generous on purpose; easy to tune. */
    private val RANGED_ATTACKER_KINDS = setOf(
        "skeleton", "stray", "bogged", "wither_skeleton",
        "blaze", "ghast", "pillager", "witch", "illusioner",
        "piglin", "drowned", "wither", "ender_dragon",
    )

    /** Damage-source ids that are projectiles regardless of attacker attribution
     *  (covers cases where the shooter entity didn't resolve). */
    private val PROJECTILE_SOURCES = setOf(
        "arrow", "trident", "fireball", "wither_skull",
        "thrown", "mob_projectile", "dragon_breath",
    )

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/autoshield") { ex -> handleToggle(ex) }
        log.info("auto-shield: {} (AUTO_SHIELD)", if (enabled) "on" else "off")
    }

    private fun handleToggle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        (body["enabled"] as? Boolean)?.let { enabled = it }
        return HttpBridge.ok(
            mapOf("enabled" to enabled),
            "auto-shield ${if (enabled) "on" else "off"}",
        )
    }

    /**
     * Reactive trigger, called from [EventBus]'s damage_taken branch (tick
     * thread) on every hit. If the hit came from a ranged hostile, stage a
     * threat facing the attacker so the next [tick] raises the guard for the
     * rest of the volley. No-op for melee hits, environmental damage, or when
     * the attacker position is unknown (nothing to face).
     */
    fun onRangedDamage(
        source: String?,
        attackerKind: String?,
        attackerPos: Triple<Double, Double, Double>?,
    ) {
        if (!enabled) return
        val kind = attackerKind?.removePrefix("minecraft:")
        val src = source?.removePrefix("minecraft:")
        val ranged = (kind != null && kind in RANGED_ATTACKER_KINDS) ||
            (src != null && src in PROJECTILE_SOURCES)
        if (!ranged) return
        if (attackerPos == null) return  // no direction → can't aim a directional block
        pendingThreat = Vec3d(attackerPos.first, attackerPos.second, attackerPos.third)
        pendingKind = kind ?: src
        pendingThreatTick = tickCount
    }

    /** Called every END_CLIENT_TICK from [EventBus], after the damage branch. */
    fun tick(client: MinecraftClient) {
        tickCount++
        val player = client.player ?: return

        // 1. Yield to any deliberate use-key owner FIRST. If a combat session is
        // taking over, hand it a clean lowered state (it tracks its own shield
        // and would otherwise desync — swinging with our guard still up). NEVER
        // release for a standalone /block: it owns the key and expects it held,
        // so touching it would drop a deliberate block. We fire this once (the
        // next tick `raised` is already false), before the melee loop's own
        // shield rhythm engages, so there's no fight over the key.
        if (UseKeyArbiter.claimedByOther()) {
            if (raised && !ShieldRoute.isBlocking()) lowerOnTick(player)
            raised = false
            pendingThreat = null
            return
        }

        // 2. Disabled → drop the guard if we had it up.
        if (!enabled) {
            if (raised) lowerOnTick(player)
            return
        }

        // 3. v1: shield must already be in the offhand.
        if (!offhandIsShield(player)) {
            if (raised) lowerOnTick(player)
            pendingThreat = null
            return
        }

        // 4. Suppress while the bot is being actively steered — don't throttle a
        // walk or fight Baritone over rotation. Keep the pending threat: once
        // the bot stops we can still guard against a fresh volley.
        if (updateMotion(player)) {
            if (raised) lowerOnTick(player)
            return
        }

        // 5. Expire a stale threat that we never got to act on.
        if (pendingThreat != null && tickCount - pendingThreatTick > THREAT_TTL_TICKS) {
            pendingThreat = null
        }

        val threat = pendingThreat
        if (threat != null) {
            pendingThreat = null
            holdUntilTick = tickCount + HOLD_DEBOUNCE_TICKS
            faceIfNeeded(player, threat)
            if (!raised) {
                raiseOnTick(player)
                raised = true
                emitRaised(threat)
            }
        } else if (raised && tickCount >= holdUntilTick) {
            // Volley window elapsed with no fresh threat — drop the guard.
            lowerOnTick(player)
        }
        // else: raised and still within the hold window → keep the guard up
        // (the pinned use-key holds it; nothing to do this tick).
    }

    /** Drop all state and release the guard. Tick thread; called on death. */
    fun reset() {
        if (raised) MinecraftClient.getInstance().player?.let { lowerOnTick(it) }
        raised = false
        pendingThreat = null
        pendingKind = null
        holdUntilTick = 0L
        lastX = Double.NaN
        lastZ = Double.NaN
        speedEma = 0.0
    }

    // -- tick-thread primitives (mirror ShieldRoute / AttackRoute) --------------

    private fun offhandIsShield(player: ClientPlayerEntity): Boolean {
        val off = player.offHandStack
        return !off.isEmpty && Registries.ITEM.getId(off.item).path == "shield"
    }

    /** Start the offhand use and pin the key so the tick loop won't drop it. */
    private fun raiseOnTick(player: ClientPlayerEntity) {
        val mc = MinecraftClient.getInstance()
        val mgr = mc.interactionManager ?: return
        mgr.interactItem(player, Hand.OFF_HAND)
        mc.options.useKey.setPressed(true)
    }

    private fun lowerOnTick(player: ClientPlayerEntity) {
        val mc = MinecraftClient.getInstance()
        mc.options.useKey.setPressed(false)
        mc.interactionManager?.stopUsingItem(player)
        raised = false
    }

    /**
     * Face the threat only when the current look is well off it — a shield
     * covers ~180° around the look direction, so we don't need pinpoint aim, and
     * skipping the re-face when we already roughly face the shooter avoids
     * needless rotation churn (and fighting a stationary rotation owner).
     */
    private fun faceIfNeeded(player: ClientPlayerEntity, threat: Vec3d) {
        val dx = threat.x - player.x
        val dz = threat.z - player.z
        val h = sqrt(dx * dx + dz * dz)
        if (h < 1e-3) return
        val look = player.rotationVector
        val lookH = sqrt(look.x * look.x + look.z * look.z)
        val dot = if (lookH < 1e-6) -1.0 else (look.x * dx + look.z * dz) / (lookH * h)
        if (dot < REFACE_DOT) {
            WorldHelpers.lookAtPosition(player, threat.x, threat.y, threat.z)
        }
    }

    /** EMA of per-tick horizontal displacement; true once sustained. */
    private fun updateMotion(player: ClientPlayerEntity): Boolean {
        val x = player.x
        val z = player.z
        if (lastX.isNaN()) {
            lastX = x; lastZ = z
            return false
        }
        val dx = x - lastX
        val dz = z - lastZ
        lastX = x; lastZ = z
        val step = sqrt(dx * dx + dz * dz)
        speedEma = speedEma * (1 - EMA_ALPHA) + step * EMA_ALPHA
        return speedEma > MOVING_EMA_EPS
    }

    private fun emitRaised(threat: Vec3d) {
        val data = mutableMapOf<String, Any>(
            "action" to "raised",
            "facing" to mapOf("x" to threat.x, "y" to threat.y, "z" to threat.z),
        )
        pendingKind?.let { data["threat"] = it }
        EventBus.emit("auto_shield", data)
    }
}
