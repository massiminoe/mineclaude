package com.mineclaude.bridge

import net.minecraft.util.math.BlockPos
import java.util.ArrayDeque

/**
 * Pre-call snapshot used by [com.mineclaude.bridge.mixin.InteractionManagerEventsMixin]
 * to detect whether a `interactBlock` call actually placed a block.
 *
 * Lives outside the `mixin` subpackage on purpose: Mixin's classloader
 * forbids referencing helper types declared inside a mixin package
 * (`IllegalClassLoadError`). Anything the mixin calls into has to be a
 * regular class in a regular package.
 *
 * Five JVM fields rather than a data class: written and read on the same
 * thread within one method call, no equality/hashCode needed.
 */
class PlaceSnapshot(
    @JvmField val clickedPos: BlockPos,
    @JvmField val adjacentPos: BlockPos,
    @JvmField val clickedWasAir: Boolean,
    @JvmField val clickedWasReplaceable: Boolean,
    @JvmField val adjacentWasAir: Boolean,
)

/**
 * Bounded ring buffer of "what happened in the world since the last drain."
 *
 * Used by the per-iteration gameState injection pipeline: the agent calls
 * `/status` once per Claude iteration, and the response includes every
 * world-mutation event captured since the previous call. This gives Claude
 * visibility into things that happened *between* its turns — most importantly
 * Baritone's autonomous mining/scaffolding, which otherwise leaves no trace
 * in the agent's tool-result stream.
 *
 * Wire shape (per entry):
 *
 *   {"ts_ms": 1717000000000, "type": "block_broken", "block": "stone",
 *    "pos": {"x": 100, "y": 64, "z": 50}}
 *
 * The buffer is intentionally separate from [EventBus] / [EventsWebSocket]:
 *
 *   - EventBus events (chat, death, damage_taken, …) are *interrupt-style*:
 *     the agent reacts via reflexes mid-turn. They're pushed over WS so
 *     they arrive immediately.
 *   - EventLog events (block_broken, block_placed, entity_attacked, …) are
 *     *log-style*: Claude reads them at the start of its next iteration.
 *     They sit in this buffer until drained on the next `/status`.
 *
 * Drain semantics: drain returns and clears all currently-buffered entries
 * atomically. Multiple `/status` calls within the same tick will see events
 * once and then an empty list — the agent's polling cadence is what defines
 * "since last gameState," not arbitrary HTTP timing.
 *
 * Thread safety: any thread can push or drain. Mixins push on the client
 * tick thread (where world mutations happen); HTTP handlers drain on
 * worker threads. Synchronization is on `this`.
 *
 * Bounded: capped at [MAX_ENTRIES] to prevent unbounded growth if a long
 * Baritone session runs without the agent polling. Oldest entries are
 * dropped silently on overflow — this is "log-style" data, so dropping
 * 10-second-old breaks to make room for current ones is the right tradeoff.
 */
object EventLog {
    private const val MAX_ENTRIES = 500

    private val entries = ArrayDeque<Map<String, Any>>()

    /**
     * Append an event. Caller supplies the type and the data payload; we
     * stamp `ts_ms` and merge into a single map. Drops oldest on overflow.
     *
     * Safe to call from any thread (mixins call it from the tick thread).
     */
    fun push(type: String, data: Map<String, Any> = emptyMap()) {
        val entry = LinkedHashMap<String, Any>(data.size + 2)
        entry["ts_ms"] = System.currentTimeMillis()
        entry["type"] = type
        entry.putAll(data)
        synchronized(this) {
            if (entries.size >= MAX_ENTRIES) entries.pollFirst()
            entries.addLast(entry)
        }
    }

    /**
     * Atomically return + clear all buffered entries. Order is oldest-first.
     * Returns an empty list if nothing happened since the last drain.
     */
    fun drain(): List<Map<String, Any>> {
        synchronized(this) {
            if (entries.isEmpty()) return emptyList()
            val out = ArrayList<Map<String, Any>>(entries.size)
            out.addAll(entries)
            entries.clear()
            return out
        }
    }

    /**
     * Diagnostic — current buffer depth without draining. Useful for /health.
     */
    fun size(): Int = synchronized(this) { entries.size }
}
