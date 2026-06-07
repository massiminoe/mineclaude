package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.hit.HitResult
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /break` — break a block at world coordinates via real player
 * actions only. No `/setblock` cheat, no fallback path: if Baritone can't
 * reach the target or the swing times out, the agent gets an honest error.
 *
 * The hard work is on the tick thread: [MinecraftClient.interactionManager]
 * is the supported client path for survival-mining. We call
 *
 *   - [`ClientPlayerInteractionManager.attackBlock(pos, side)`] once to
 *     start breaking
 *   - [`updateBlockBreakingProgress(pos, side)`] each tick to keep ticking
 *     down the per-block break timer
 *   - [`cancelBlockBreaking()`] in `finally` to release the partial state
 *     if we time out
 *
 * The HTTP worker thread loops, nudging progress once per client tick.
 * Pacing comes from [TickThread.submitAndWait] blocking until the next
 * tick drains (~50 ms) — NOT from a worker-side sleep, which would stack
 * on top of the tick-wait and slow mining to ~⅔ speed. We can't run the
 * loop entirely on the tick thread because that would freeze MC's
 * render/input for the duration.
 *
 * # Occlusion handling
 * MC's eye-ray can intersect a nearer block first (angle too shallow,
 * dirt above stone, etc.). Without auto-clearing the occluder we'd swing
 * at the wrong block and the target's getBlockState wouldn't change → a
 * silent 15 s timeout. We detect occlusion via a fresh raycast after
 * aiming and recursively /break the occluder up to depth=2. Containers
 * and other "decision-worthy" blocks are excluded via [WorldHelpers.OCCLUDER_DENYLIST]
 * and bubble up as a clear error so the agent can decide.
 */
object BreakRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.break")!!

    // Obsidian is 9.4s with a diamond pickaxe at full speed; under amd64
    // emulation the client can tick below 20 TPS, stretching wall-clock
    // break time. 30s leaves headroom for the slowest legitimate mines.
    private const val DEADLINE_MS = 30_000L
    // Just a yield. The real pacing is submitAndWait blocking until the next
    // client tick (~50ms) — see the loop in swingUntilBroken. A large sleep
    // here STACKS on top of that tick-wait, dropping updateBlockBreakingProgress
    // from ~20/s to ~13/s (≈⅔ vanilla mining speed) and pushing obsidian past
    // the deadline. Keep this near-zero so the loop self-paces to the tick rate.
    private const val POLL_INTERVAL_MS = 5L
    private const val MAX_OCCLUDER_DEPTH = 2

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/break") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val x = (body["x"] as? Number)?.toInt() ?: 0
        val y = (body["y"] as? Number)?.toInt() ?: 0
        val z = (body["z"] as? Number)?.toInt() ?: 0
        return breakBlock(BlockPos(x, y, z), occluderDepth = 0)
    }

    /**
     * Break [pos]. Recursive on occlusion (auto-clears the blocking
     * neighbour up to [MAX_OCCLUDER_DEPTH]).
     */
    private fun breakBlock(pos: BlockPos, occluderDepth: Int): BridgeResponse {
        // Initial state probe + idempotency check on the tick thread.
        val originalBlock: String? = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait null
            val world = mc.world ?: return@submitAndWait null
            WorldHelpers.ensureNoScreenOpen(player)
            val state = world.getBlockState(pos)
            if (state.isAir) null else WorldHelpers.blockIdAt(pos)
        }
        if (originalBlock == null) {
            // Idempotent: target already air. Same wire shape as legacy's
            // already_gone success.
            log.info("break: target already air at {} (no-op)", pos)
            return HttpBridge.ok(
                mapOf(
                    "broken" to true,
                    "already_gone" to true,
                    "block" to "air",
                    "method" to "no-op",
                ),
                "Block at ${pos.x}, ${pos.y}, ${pos.z} was already air (no-op)",
            )
        }

        // Self-navigate within reach if needed. Baritone has its own 15 s
        // cap inside Navigation.navigateNear, plus a 5 s motion-stall bail,
        // so unreachable targets fail in bounded time.
        val inReach = TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait false
            WorldHelpers.isBlockWithinReach(p, pos)
        }
        if (!inReach) {
            val nav = Navigation.navigateNear(pos)
            if (nav is Navigation.Result.Failed) {
                val displayName = originalBlock.removePrefix("minecraft:")
                return HttpBridge.err(
                    "Couldn't reach $displayName at (${pos.x}, ${pos.y}, ${pos.z}) to break: ${nav.reason}",
                )
            }
        }

        // Aim at block centre.
        TickThread.submitAndWait(timeoutMs = 1_000) {
            val p = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
            WorldHelpers.lookAtBlock(p, pos)
            Unit
        }
        Thread.sleep(100)

        // Occlusion check — raycast and see if we'd actually hit `pos` from
        // here. If not, recursively clear the occluder (or bubble up if it
        // is on the denylist).
        val occluder: Pair<String, BlockPos>? =
            TickThread.submitAndWait(timeoutMs = 1_000) { raycastOccluder(pos) }
        if (occluder != null) {
            val occluderType = occluder.first
            val occluderPos = occluder.second
            if (occluderType in WorldHelpers.OCCLUDER_DENYLIST) {
                return HttpBridge.err(
                    "Crosshair is on $occluderType at (${occluderPos.x}, ${occluderPos.y}, " +
                        "${occluderPos.z}), not target (${pos.x}, ${pos.y}, ${pos.z}). " +
                        "Won't auto-break $occluderType (container/functional block) — " +
                        "agent should decide whether to break it."
                )
            }
            if (occluderDepth >= MAX_OCCLUDER_DEPTH) {
                return HttpBridge.err(
                    "Crosshair is on $occluderType at (${occluderPos.x}, ${occluderPos.y}, " +
                        "${occluderPos.z}), not target (${pos.x}, ${pos.y}, ${pos.z}). " +
                        "Reached auto-occluder-clear depth limit ($MAX_OCCLUDER_DEPTH) — " +
                        "target appears deeply buried, try a different approach."
                )
            }
            log.info(
                "break: auto-clearing occluder {} at {} to reach target {} [depth={}]",
                occluderType, occluderPos, pos, occluderDepth,
            )
            val sub = breakBlock(occluderPos, occluderDepth + 1)
            if (sub.status == "error") return sub
            // Re-aim at true target before falling through to the swing loop.
            TickThread.submitAndWait(timeoutMs = 1_000) {
                val p = MinecraftClient.getInstance().player ?: return@submitAndWait Unit
                WorldHelpers.lookAtBlock(p, pos)
                Unit
            }
            Thread.sleep(100)
        }

        // Swing loop. Returns true when the block becomes air, false on
        // timeout. cancelBlockBreaking is unconditionally fired in finally
        // so we don't leak a half-broken block onto the next request.
        val broken = swingUntilBroken(pos)
        if (!broken) {
            return HttpBridge.err("Timed out breaking $originalBlock")
        }
        log.info("break: broke {} at {} (real)", originalBlock, pos)
        return HttpBridge.ok(
            mapOf(
                "broken" to true,
                "block" to originalBlock.removePrefix("minecraft:"),
                "method" to "real",
            ),
            "Broke block at ${pos.x}, ${pos.y}, ${pos.z}",
        )
    }

    /**
     * Run the player's crosshair raycast on the tick thread. Returns
     * `(occluderName, occluderPos)` if the crosshair is hitting a block
     * that ISN'T [target], or null if there's no occluder (or no hit).
     */
    private fun raycastOccluder(target: BlockPos): Pair<String, BlockPos>? {
        val mc = MinecraftClient.getInstance()
        val hit = mc.crosshairTarget ?: return null
        if (hit.type != HitResult.Type.BLOCK) return null
        val bhr = hit as? BlockHitResult ?: return null
        val hitPos = bhr.blockPos
        if (hitPos == target) return null
        val name = WorldHelpers.blockIdAt(hitPos)
        return name to hitPos
    }

    private fun swingUntilBroken(pos: BlockPos): Boolean {
        // First tick: open the break (attackBlock fires the START_DESTROY_BLOCK
        // packet + sets local breakingBlock state).
        TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait Unit
            val mgr = mc.interactionManager ?: return@submitAndWait Unit
            val side = WorldHelpers.playerFacingSide(player, pos)
            mgr.attackBlock(pos, side)
            player.swingHand(Hand.MAIN_HAND)
            Unit
        }

        val deadline = System.currentTimeMillis() + DEADLINE_MS
        try {
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(POLL_INTERVAL_MS)
                val done = TickThread.submitAndWait(timeoutMs = 2_000) { tickBreakProgress(pos) }
                if (done) return true
            }
        } finally {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                MinecraftClient.getInstance().interactionManager?.cancelBlockBreaking()
                Unit
            }
        }
        return false
    }

    /**
     * Single-tick advance of the break. Called from a TickThread submission
     * once per ~50 ms loop iteration. Returns true when the block is gone.
     */
    private fun tickBreakProgress(pos: BlockPos): Boolean {
        val mc = MinecraftClient.getInstance()
        val world = mc.world ?: return false
        val player = mc.player ?: return false
        val mgr = mc.interactionManager ?: return false

        if (world.getBlockState(pos).isAir) return true

        val side = WorldHelpers.playerFacingSide(player, pos)
        // updateBlockBreakingProgress returns true on completion in some
        // versions and on continued-progress in others; we don't rely on
        // its return value — the world.getBlockState check above is the
        // authoritative signal.
        mgr.updateBlockBreakingProgress(pos, side)
        // Match the visible mining animation — vanilla doAttack swings
        // every tick while breaking.
        player.swingHand(Hand.MAIN_HAND)
        // Heartbeat the idle camera while we're still grinding this block, so
        // a slow break (obsidian, wrong tool) can't outlive the dormancy
        // window and pan the head away mid-mine.
        CameraDirector.noteFunctionalAim()
        return false
    }
}
