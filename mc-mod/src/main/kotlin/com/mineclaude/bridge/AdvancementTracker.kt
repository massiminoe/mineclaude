package com.mineclaude.bridge

import net.minecraft.advancement.AdvancementEntry
import net.minecraft.advancement.AdvancementProgress
import net.minecraft.advancement.PlacedAdvancement
import net.minecraft.client.MinecraftClient
import net.minecraft.client.network.ClientAdvancementManager
import org.slf4j.LoggerFactory
import java.util.concurrent.ConcurrentHashMap

/**
 * Tracks advancement (achievement) grants for observability + eval timing.
 *
 * Minecraft tracks advancement progress client-side in [ClientAdvancementManager],
 * but the only public hook is `setListener` — the progress map itself isn't
 * readable. So this tracker IS the source of truth for both surfaces:
 *
 *   - the `advancement` event, emitted the moment one is newly completed. It
 *     rides [EventBus]'s events WS into the Runtime's flushable buffer, where
 *     it shows up in get_state.events / wait_for_event(["advancement"]) and the
 *     session log — the "how quickly did the agent earn it" signal, stamped on
 *     receipt by the Runtime.
 *   - the GET /advancements snapshot ([snapshot]) — earned + in-progress, read
 *     off [progress].
 *
 * Attach is lazy + idempotent, driven from [EventBus]'s END_CLIENT_TICK: the
 * first tick that sees a ClientAdvancementManager attaches our listener; a new
 * manager instance (relog / world change brings a fresh network handler)
 * re-attaches and rebuilds state.
 *
 * Suppressing the initial full-state sync:
 *   ClientAdvancementManager replays ALL known progress synchronously when a
 *   listener is set, and re-syncs the full set on a `reset` packet (signalled
 *   by [onClear] then a setProgress burst). Both are indistinguishable from a
 *   real grant at the setProgress callback, so we gate emission on [seeding]:
 *   set true around setListener and in onClear, then cleared at the tail of
 *   every tick. A full-state sync completes synchronously within one client
 *   tick, so its setProgress calls seed [seen] silently; a genuine later grant
 *   arrives in its own packet/tick with seeding=false and emits.
 *
 *   Known limitation: a grant landing in the very same tick as a reset sync
 *   would be suppressed. That's only reachable at join — before any advancement
 *   has been earned — so it's irrelevant for the fresh-world-per-run eval case.
 *
 * Recipe filtering:
 *   Every `minecraft:recipes/...` unlock is itself an advancement (~1300 in
 *   vanilla 1.21.5, vs. ~110 real ones). They carry no display, so [setProgress]
 *   skips anything without one — the tracker follows only the real, toast-worthy
 *   story/nether/end/adventure/husbandry tree. Without this the event stream and
 *   the eval metric would drown in recipe-unlock noise.
 */
object AdvancementTracker {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.advancements")!!

    /** Immutable per-advancement view the /advancements route reads off-thread. */
    data class Snap(
        val id: String,
        val title: String,
        val frame: String,
        val done: Boolean,
        val obtained: Int,
        val total: Int,
    )

    // Full progress map, keyed by advancement id. ConcurrentHashMap because the
    // route reads it on an HttpServer worker thread while the listener writes it
    // on the client tick thread.
    private val progress = ConcurrentHashMap<String, Snap>()

    // Completed-advancement ids already accounted for (dedupe + seed). Mutated
    // only on the client thread (listener callbacks + tick attach).
    private val seen = HashSet<String>()

    // True while a full-state sync is in flight (listener-attach replay, or a
    // reset packet's onClear+setProgress burst). Cleared at the tail of tick().
    @Volatile private var seeding = false

    // The manager we're attached to, by identity. A new instance (relog /
    // dimension swap → fresh ClientPlayNetworkHandler) triggers re-attach.
    private var attached: ClientAdvancementManager? = null

    private val listener = object : ClientAdvancementManager.Listener {
        override fun setProgress(placed: PlacedAdvancement, prog: AdvancementProgress) {
            val entry = placed.advancementEntry
            val adv = entry.value()
            val displayOpt = adv.display()
            // Skip recipe + technical advancements: they carry no display (no
            // toast, absent from the advancements GUI). Vanilla 1.21.5 ships
            // ~1300 of these (every `minecraft:recipes/...` unlock is an
            // advancement) vs. the ~110 real ones — tracking them would flood
            // the event stream and swamp the eval metric. display.isPresent
            // cleanly selects the real story/nether/end/adventure/husbandry tree.
            val display = displayOpt.orElse(null) ?: return
            val id = entry.id().toString()
            val title = display.title.string
            val frame = display.frame.asString()
            val obtained = prog.obtainedCriteria.count()
            val total = obtained + prog.unobtainedCriteria.count()
            val done = prog.isDone
            progress[id] = Snap(id, title, frame, done, obtained, total)
            if (done) {
                // First time we see this advancement done, outside a full-state
                // sync → a real grant. seen.add returns false if already present.
                if (seen.add(id) && !seeding) {
                    val data = mutableMapOf<String, Any>(
                        "id" to id,
                        "title" to title,
                        "frame" to frame,
                        "description" to display.description.string,
                    )
                    adv.parent().ifPresent { data["parent"] = it.toString() }
                    EventBus.emit("advancement", data)
                    log.info("advancement earned: {} ({})", id, title)
                }
            } else {
                // Reverted to not-done. `/advancement revoke` resets in-place
                // via per-advancement progress updates (not the onClear full
                // reset), so drop it from `seen` — otherwise a later re-earn
                // sees it already-present and never emits. This is what makes
                // revoke → re-earn cycles (repeated eval trials without a world
                // regen) produce a fresh `advancement` event each time.
                seen.remove(id)
            }
        }

        override fun onClear() {
            // A reset packet is resyncing the full set; the setProgress burst
            // that follows (this same synchronous call) is state, not grants.
            seeding = true
            seen.clear()
            progress.clear()
        }

        override fun selectTab(entry: AdvancementEntry?) {}
        override fun onRootAdded(root: PlacedAdvancement) {}
        override fun onRootRemoved(root: PlacedAdvancement) {}
        override fun onDependentAdded(dependent: PlacedAdvancement) {}
        override fun onDependentRemoved(dependent: PlacedAdvancement) {}
    }

    /** Called from EventBus's END_CLIENT_TICK (player already non-null). */
    fun tick(client: MinecraftClient) {
        val mgr = client.player?.networkHandler?.advancementHandler
        if (mgr == null) {
            attached = null
            return
        }
        if (mgr !== attached) {
            seen.clear()
            progress.clear()
            seeding = true
            // Synchronous replay of existing progress fires here, suppressed.
            mgr.setListener(listener)
            attached = mgr
            log.info("advancement listener attached")
        }
        // Close the seeding window: any full-state sync (the attach replay
        // above, or a reset burst earlier this tick) has completed
        // synchronously by now, so later grants emit normally.
        seeding = false
    }

    /** Snapshot for GET /advancements. Reads the concurrent progress map. */
    fun snapshot(): Map<String, Any> {
        val all = progress.values.toList()
        val earned = all.asSequence().filter { it.done }
            .sortedBy { it.id }
            .map { mapOf("id" to it.id, "title" to it.title, "frame" to it.frame) }
            .toList()
        val inProgress = all.asSequence().filter { !it.done && it.obtained > 0 }
            .sortedBy { it.id }
            .map {
                mapOf(
                    "id" to it.id,
                    "title" to it.title,
                    "obtained" to it.obtained,
                    "total" to it.total,
                )
            }
            .toList()
        return mapOf(
            "earned" to earned,
            "earned_count" to earned.size,
            "in_progress" to inProgress,
        )
    }
}
