package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.SpawnGroup
import net.minecraft.network.packet.c2s.play.ClientCommandC2SPacket
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.hit.HitResult
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /sleep {x, y, z, wait_s?}` — sleep in the bed at the given coords.
 *
 * # Why this isn't just `/interact` on a bed
 *
 * Sleeping is the one block interaction whose success can't be read off the
 * click. `interactionManager.interactBlock` predicts ACCEPTED client-side for
 * a bed even when the server is about to reject the sleep (daytime, monsters
 * nearby, obstructed) — so `/interact` would cheerfully report `interacted:
 * true` while the player never actually lay down. It also has no notion of
 * *waking*: a naive click leaves the bot asleep (or stuck in the sleep screen)
 * with no signal that the night passed.
 *
 * So this route owns the full lifecycle:
 *   1. Verify the target is a bed, navigate within reach, aim + click it.
 *   2. Confirm we actually entered sleep by polling `player.isSleeping` (the
 *      server round-trips the sleep state — that's the source of truth, not
 *      the click's optimistic accept flag). On failure, diagnose the cause
 *      (daytime / monsters / obstructed) client-side and return it.
 *   3. Block until the player wakes — the server wakes all sleepers and
 *      advances to morning once the sleep threshold is met. Bail after
 *      `wait_s` and send STOP_SLEEPING so the bot never hangs in bed: night
 *      won't skip if other players are awake or `playersSleepingPercentage`
 *      isn't met, and we report that honestly rather than blocking forever.
 *
 * Truth-in-return: `{slept, night_skipped, time}`. `slept` means we confirmed
 * the sleep; `night_skipped` means we woke into morning (vs. interrupted or
 * timed out still at night).
 */
object SleepRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.sleep")!!

    /** Wait for the server to accept the sleep + flip `isSleeping`. */
    private const val SLEEP_CONFIRM_MS = 1_500L
    private const val CONFIRM_POLL_MS = 100L
    /** Default cap on the wait-for-morning loop. Capped well under the client's 90s HTTP timeout. */
    private const val DEFAULT_WAKE_WAIT_MS = 20_000L
    private const val WAKE_POLL_MS = 250L
    /**
     * Grace window to let the clock settle after waking. On a night-skip the
     * server wakes sleepers a tick or two *before* it advances time to morning,
     * so sampling `timeOfDay` the instant `isSleeping` flips races the time
     * update and reads a still-night value. Poll for the morning time to land
     * before deciding `night_skipped`.
     */
    private const val WAKE_SETTLE_MS = 2_000L
    /** Vanilla monster-proximity check radius for "you may not rest now". */
    private const val MONSTER_RADIUS = 8.0
    /** First tick of the morning (time-of-day wraps at 24000). */
    private const val MORNING_END = 12542L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/sleep") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val x = (body["x"] as? Number)?.toInt() ?: return HttpBridge.err("Missing 'x' parameter", 400)
        val y = (body["y"] as? Number)?.toInt() ?: return HttpBridge.err("Missing 'y' parameter", 400)
        val z = (body["z"] as? Number)?.toInt() ?: return HttpBridge.err("Missing 'z' parameter", 400)
        val wakeWaitMs = ((body["wait_s"] as? Number)?.toDouble()?.times(1000))?.toLong() ?: DEFAULT_WAKE_WAIT_MS
        val target = BlockPos(x, y, z)

        // 1. Must be a bed.
        val blockId = TickThread.submitAndWait(1_000) {
            MinecraftClient.getInstance().world ?: return@submitAndWait null
            WorldHelpers.blockIdAt(target)
        } ?: return HttpBridge.err("no world")
        if (!blockId.endsWith("_bed")) {
            return HttpBridge.err("Block at ($x, $y, $z) is $blockId, not a bed")
        }

        // 2. Navigate within reach.
        val inReach = TickThread.submitAndWait(1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(p, target)
        }
        if (!inReach) {
            val nav = Navigation.navigateNear(target)
            if (nav is Navigation.Result.Failed) {
                return HttpBridge.err("couldn't reach the bed at ($x, $y, $z): ${nav.reason}")
            }
        }

        // 3. Aim + click the bed. We ignore the accept flag (unreliable for
        //    beds) — step 4 reads the real sleep state.
        val centre = WorldHelpers.blockCentre(target)
        TickThread.submitAndWait(2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            WorldHelpers.ensureNoScreenOpen(player)
            WorldHelpers.lookAtPosition(player, centre.x, centre.y, centre.z)
            val hr = player.raycast(WorldHelpers.BLOCK_REACH, 1.0f, false)
            if (hr.type == HitResult.Type.BLOCK) {
                mgr.interactBlock(player, Hand.MAIN_HAND, hr as BlockHitResult)
                player.swingHand(Hand.MAIN_HAND)
            }
            Unit
        }

        // 4. Confirm we entered sleep (server round-trips the state).
        if (!pollUntil(SLEEP_CONFIRM_MS, CONFIRM_POLL_MS) { isSleeping() }) {
            return HttpBridge.err("didn't sleep in the bed at ($x, $y, $z): ${diagnoseFailure(target)}")
        }

        // 5. Wait for morning / night-skip, then report honestly.
        val woke = pollUntil(wakeWaitMs, WAKE_POLL_MS) { !isSleeping() }
        if (!woke) {
            TickThread.submitAndWait(1_000) {
                val p = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
                p.networkHandler.sendPacket(
                    ClientCommandC2SPacket(p, ClientCommandC2SPacket.Mode.STOP_SLEEPING)
                )
                Unit
            }
            val time = timeOfDay()
            log.info("sleep: timed out asleep after {}ms, left the bed (time={})", wakeWaitMs, time)
            return HttpBridge.ok(
                mapOf("slept" to true, "night_skipped" to false, "time" to time),
                "Slept, but night didn't skip within ${wakeWaitMs / 1000}s — left the bed. " +
                    "Another player may be awake, or the playersSleepingPercentage gamerule isn't met.",
            )
        }
        // The night-skip time-set lands a tick or two after the wake, so poll
        // the clock for a short grace window until it settles into morning
        // before deciding. If it never reaches morning, it was a real
        // interruption (sleep at confirm time means it was night, not day).
        val skipped = pollUntil(WAKE_SETTLE_MS, CONFIRM_POLL_MS) { isMorning() }
        val time = timeOfDay()
        log.info("sleep: woke (time={}, night_skipped={})", time, skipped)
        return HttpBridge.ok(
            mapOf("slept" to true, "night_skipped" to skipped, "time" to time),
            if (skipped) "Slept through the night — it's morning now (time=$time)"
            else "Woke from the bed before morning (time=$time) — sleep was interrupted " +
                "(damage, a nearby mob, or a manual wake) and the night did not skip.",
        )
    }

    /** Best-effort client-side reason the bed click didn't put us to sleep. */
    private fun diagnoseFailure(bed: BlockPos): String = TickThread.submitAndWait(1_000) {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return@submitAndWait "no world"
        if (world.isDay && !world.isThundering) {
            return@submitAndWait "it's daytime — you can only sleep at night or during a thunderstorm"
        }
        val px = bed.x + 0.5
        val py = bed.y.toDouble()
        val pz = bed.z + 0.5
        val rSq = MONSTER_RADIUS * MONSTER_RADIUS
        for (e in world.entities) {
            if (!e.isAlive) continue
            if (e.type.spawnGroup != SpawnGroup.MONSTER) continue
            val dx = e.x - px; val dy = e.y - py; val dz = e.z - pz
            if (dx * dx + dy * dy + dz * dz <= rSq) {
                return@submitAndWait "monsters nearby — clear hostiles within ${MONSTER_RADIUS.toInt()} blocks first"
            }
        }
        "bed obstructed, occupied, or out of reach — check it's clear and you're standing next to it"
    }

    private fun isSleeping(): Boolean {
        val p = MinecraftClient.getInstance().player ?: return false
        return p.isSleeping
    }

    private fun timeOfDay(): Long =
        TickThread.submitAndWait(1_000) { MinecraftClient.getInstance().world?.timeOfDay ?: -1L }

    /**
     * Is the sky clock in the morning band? `timeOfDay` is an unbounded day
     * counter; the sky cycle is `time % 24000`, and `[0, MORNING_END)` is the
     * post-wake morning. Reads world directly — meant to be called *inside* a
     * tick-thread predicate (e.g. [pollUntil]).
     */
    private fun isMorning(): Boolean {
        val time = MinecraftClient.getInstance().world?.timeOfDay ?: return false
        val dayTime = if (time >= 0) time % 24000 else time
        return dayTime in 0 until MORNING_END
    }

    /**
     * Poll [predicate] on the tick thread every [pollMs] until it's true or
     * [budgetMs] elapses. Returns whether it became true. The wait happens on
     * the worker thread (Thread.sleep), matching GotoRoute/CollectRoute.
     */
    private fun pollUntil(budgetMs: Long, pollMs: Long, predicate: () -> Boolean): Boolean {
        var waited = 0L
        while (waited < budgetMs) {
            if (TickThread.submitAndWait(1_000) { predicate() }) return true
            Thread.sleep(pollMs)
            waited += pollMs
        }
        return TickThread.submitAndWait(1_000) { predicate() }
    }
}
