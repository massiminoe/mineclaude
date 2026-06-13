package com.mineclaude.bridge

import net.minecraft.block.DoorBlock
import net.minecraft.block.enums.DoubleBlockHalf
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.client.network.ClientPlayerInteractionManager
import net.minecraft.registry.Registries
import net.minecraft.state.property.Properties
import net.minecraft.util.Hand
import net.minecraft.util.hit.BlockHitResult
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Vec3d
import org.slf4j.LoggerFactory

/**
 * Auto-close doors that Baritone auto-opened along the bot's path.
 *
 * Baritone opens doors in its way but never closes them — there's no
 * symmetric "close behind me" hook, and we drive Baritone over chat, so we
 * can't ask it to. Instead we observe and react: a tick-thread reflex (driven
 * from [EventBus]'s END_CLIENT_TICK callback, alongside the lava/drowning/
 * hostile state machines) tracks doors the bot opens while pathing and
 * right-clicks them shut once the bot has stepped clear.
 *
 * # How a door is attributed to the bot
 * Not "any open door near the bot" (that would close doors a human left open
 * or opened deliberately). We watch for a **closed→open transition** on a door
 * within scan range *while the bot is actively moving under our own Baritone
 * command* ([armed] + recent motion). A door that was already open when it
 * entered range never had its transition observed, so it's never tracked. This
 * precisely captures "a door Baritone opened to walk through".
 *
 * # The arm flag
 * [arm]/[disarm] are called by the four Baritone routes (/goto, /mine,
 * /follow, /explore ; cleared by /stop and /goto's exit). Attribution also
 * requires the player to have actually moved recently, so a stale `armed`
 * (e.g. a `#mine <count>` that self-completed without a /stop) can't make the
 * idle bot close doors a human opens nearby.
 *
 * # Closing
 * Once a tracked door's lower half is open, the bot is no longer standing in
 * the doorway, and the door is within interaction reach, we toggle it shut via
 * a synthetic [BlockHitResult] + `interactBlock` — the same view-rotation-free
 * path [PlaceRoute] uses, so it never fights Baritone's camera. If the bot
 * leaves reach before we get the chance (we missed the window), the door is
 * dropped rather than pathing back to it. Iron doors are skipped (redstone-only
 * — can't be hand-toggled).
 */
object DoorTrail {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.doortrail")!!

    // Scan every other tick (~10/s) — cheap, and fast enough to catch both the
    // pre-open "closed" snapshot and the post-traversal in-reach close window.
    private const val SCAN_INTERVAL_TICKS = 2
    private const val SCAN_RADIUS = 3        // horizontal half-extent (blocks)
    private const val SCAN_VERTICAL = 1      // ±y around the feet cell

    // Min squared horizontal travel between scans to count as "moving now".
    private const val MOVE_EPS = 0.04
    // Keep attributing for a short grace after the last motion, so the brief
    // pause Baritone makes to open a door doesn't read as "idle".
    private const val ACTIVE_GRACE_SCANS = 6  // ~0.6s at SCAN_INTERVAL_TICKS

    // Tolerate a couple of consecutive out-of-reach scans before giving up on
    // a door (rather than the bot pathing back). Smooths over momentary
    // overshoot on a fast diagonal exit, where one scan can read just out of
    // reach and the next is back in range.
    private const val MAX_OUT_OF_REACH_SCANS = 3

    // Doors that can't be opened/closed by hand (need redstone).
    private val SKIP_DOOR_IDS = setOf("iron_door")

    @Volatile private var armed = false

    // Tick-thread-only state (touched solely from [tick]).
    private val doorStates = HashMap<BlockPos, Boolean>()  // last-seen open state, in-range doors
    private val tracked = HashMap<BlockPos, Int>()         // door we owe a close → consecutive out-of-reach scans
    private var lastPos: Vec3d? = null
    private var idleScans = ACTIVE_GRACE_SCANS
    private var tickCounter = 0

    /** Arm attribution. Called from the Baritone movement routes (HTTP worker thread). */
    fun arm() { armed = true }

    /** Disarm attribution (movement ended). In-flight tracked doors still get closed. */
    fun disarm() { armed = false }

    /** Wipe all state — call on death so stale BlockPos from the old world don't linger. */
    fun reset() {
        doorStates.clear()
        tracked.clear()
        lastPos = null
        idleScans = ACTIVE_GRACE_SCANS
    }

    /**
     * Per-tick driver. MUST run on the client tick thread (called from
     * [EventBus] END_CLIENT_TICK). Detects doors Baritone auto-opens along the
     * path and shuts them once the bot has stepped through.
     */
    fun tick(client: MinecraftClient, player: ClientPlayerEntity) {
        if (++tickCounter < SCAN_INTERVAL_TICKS) return
        tickCounter = 0

        val world = client.world ?: return

        // Motion gate: "active" = moved within the last ACTIVE_GRACE_SCANS scans.
        val pos = player.pos
        val prev = lastPos
        val movedNow = prev == null || run {
            val dx = pos.x - prev.x; val dz = pos.z - prev.z
            (dx * dx + dz * dz) > MOVE_EPS * MOVE_EPS
        }
        idleScans = if (movedNow) 0 else (idleScans + 1)
        lastPos = pos
        val active = idleScans < ACTIVE_GRACE_SCANS

        // 1. Scan nearby door lower-halves; record state + detect closed→open.
        val feet = WorldHelpers.playerFeetCell(player)
        val seen = HashMap<BlockPos, Boolean>()
        for (dx in -SCAN_RADIUS..SCAN_RADIUS) {
            for (dy in -SCAN_VERTICAL..SCAN_VERTICAL) {
                for (dz in -SCAN_RADIUS..SCAN_RADIUS) {
                    val p = BlockPos(feet.x + dx, feet.y + dy, feet.z + dz)
                    val state = world.getBlockState(p)
                    if (state.block !is DoorBlock) continue
                    if (state.get(Properties.DOUBLE_BLOCK_HALF) != DoubleBlockHalf.LOWER) continue
                    val id = Registries.BLOCK.getId(state.block).path
                    if (id in SKIP_DOOR_IDS) continue
                    val open = state.get(Properties.OPEN)
                    seen[p] = open
                    // closed→open while we're driving Baritone and the bot is
                    // moving ⇒ Baritone opened it to walk through. Track it.
                    if (armed && active && doorStates[p] == false && open && p !in tracked) {
                        tracked[p] = 0
                        log.info("doortrail: tracking {} at {} (bot opened it)", id, p)
                    }
                }
            }
        }
        // Remembered states = exactly what's in range now (drops doors that
        // left the scan box; their history is no longer relevant).
        doorStates.clear()
        doorStates.putAll(seen)

        // 2. Close tracked doors the bot has stepped clear of.
        if (tracked.isEmpty()) return
        val mgr = client.interactionManager ?: return
        val it = tracked.iterator()
        while (it.hasNext()) {
            val entry = it.next()
            val p = entry.key
            val state = world.getBlockState(p)
            if (state.block !is DoorBlock || !state.get(Properties.OPEN)) {
                it.remove(); continue                 // broken, or already shut
            }
            // Sneaking would route the right-click to item-use (could place a
            // held block) instead of toggling the door — wait it out.
            if (player.isSneaking) continue
            // Still standing in the doorway — not through yet.
            if (WorldHelpers.playerOccupiesCell(player, p) ||
                WorldHelpers.playerOccupiesCell(player, p.up())
            ) continue
            // Reach + hit aim at the door's nearest surface point (not its
            // centre): on a steep-angle exit the near face is well within
            // range even when the centre isn't, and the hit lands cleanly on
            // the panel the bot is actually beside.
            val fh = WorldHelpers.nearestFaceHit(player, p)
            if (fh.dist > WorldHelpers.BLOCK_REACH) {
                // Out of reach this scan — give it a few scans to come back
                // before abandoning (vs. pathing the bot back to the door).
                if ((entry.value + 1) >= MAX_OUT_OF_REACH_SCANS) it.remove() else entry.setValue(entry.value + 1)
                continue
            }
            closeDoor(player, mgr, p, fh)
            it.remove()
        }
    }

    /** Toggle the (open) door at [pos] shut via a synthetic click — no view rotation. */
    private fun closeDoor(
        player: ClientPlayerEntity,
        mgr: ClientPlayerInteractionManager,
        pos: BlockPos,
        fh: WorldHelpers.FaceHit,
    ) {
        val hit = BlockHitResult(fh.pos, fh.face, pos, /*insideBlock=*/ false)
        val result = mgr.interactBlock(player, Hand.MAIN_HAND, hit)
        if (result.isAccepted) player.swingHand(Hand.MAIN_HAND)
        log.info("doortrail: closed door at {} (accepted={}, dist={})", pos, result.isAccepted, "%.2f".format(fh.dist))
    }
}
