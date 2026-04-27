package com.mineclaude.bridge

import java.util.concurrent.atomic.AtomicReference

/**
 * Tiny TTL cache for tick-thread snapshot reads.
 *
 * MC reads (`getPlayerStatus`, `getNearbyBlocks`, etc.) are cheap on the
 * tick thread but burst-prone — a single Claude iteration may issue
 * `/status` plus two `/nearby/...` queries within a few milliseconds. Each
 * call schedules a separate `MinecraftClient.execute(...)` task; without
 * coalescing, we add three tick-thread round-trips where one would do.
 *
 * [TickCache] holds the most recent successful snapshot for [ttlMs] ms.
 * Reads inside the TTL window return the cached value synchronously. A
 * single in-flight refresh is shared across concurrent callers so a
 * cold-cache stampede still hits the tick thread once.
 *
 * TTL defaults to 50 ms (one tick at 20 TPS) — short enough that staleness
 * is bounded by tick granularity, long enough to absorb burst patterns.
 */
class TickCache<T : Any>(
    private val ttlMs: Long,
    private val read: () -> T,
) {
    private data class Snapshot<T>(val value: T, val expiresAt: Long)

    private val current = AtomicReference<Snapshot<T>?>(null)

    /** Lock guards [inFlight] mutations only — never held across a tick-thread wait. */
    private val lock = Any()

    @Volatile
    private var inFlight: java.util.concurrent.CompletableFuture<T>? = null

    /**
     * Returns the cached value if fresh; otherwise schedules a refresh on
     * the tick thread and waits up to [waitMs] ms for it. Concurrent callers
     * during a cold-cache miss share a single tick-thread submission.
     */
    fun get(waitMs: Long = 2_000): T {
        val now = System.currentTimeMillis()
        current.get()?.let { if (now < it.expiresAt) return it.value }

        val fut: java.util.concurrent.CompletableFuture<T> = synchronized(lock) {
            current.get()?.let { fresh ->
                if (System.currentTimeMillis() < fresh.expiresAt) {
                    return fresh.value
                }
            }
            inFlight ?: run {
                val newFut = TickThread.submit(read)
                inFlight = newFut
                newFut.whenComplete { value, err ->
                    if (err == null && value != null) {
                        current.set(Snapshot(value, System.currentTimeMillis() + ttlMs))
                    }
                    synchronized(lock) {
                        if (inFlight === newFut) inFlight = null
                    }
                }
                newFut
            }
        }

        return fut.get(waitMs, java.util.concurrent.TimeUnit.MILLISECONDS)
    }

    /** Force-clear the cached snapshot — used when a write invalidates the read. */
    fun invalidate() {
        current.set(null)
    }
}
