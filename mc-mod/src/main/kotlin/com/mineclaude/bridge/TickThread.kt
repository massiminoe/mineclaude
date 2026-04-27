package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicLong

/**
 * Schedules work onto the MC client tick thread.
 *
 * MC's mutable state (player, world, container menu) is only safe to touch
 * from the client thread. HTTP handlers run on the bridge worker pool, so
 * every handler that needs MC state goes through [submit] and waits on the
 * returned [CompletableFuture].
 *
 * The MC method `MinecraftClient.execute(Runnable)` adds a task to the
 * client's task queue, which is drained once per tick. Latency is therefore
 * bounded by one tick (~50 ms) under normal load.
 */
object TickThread {
    /** Pending tick-thread submissions that haven't completed yet. */
    private val pending = AtomicLong(0)

    /** Total submissions since boot — diagnostic counter for /health. */
    private val totalSubmitted = AtomicLong(0)

    fun pendingCount(): Long = pending.get()
    fun totalSubmitted(): Long = totalSubmitted.get()

    /**
     * Schedule [task] on the client tick thread. The future completes with
     * the task's return value (or its thrown exception). Tracks inflight
     * count for `/health` reporting.
     */
    fun <T : Any> submit(task: () -> T): CompletableFuture<T> {
        val future = CompletableFuture<T>()
        pending.incrementAndGet()
        totalSubmitted.incrementAndGet()
        val mc = MinecraftClient.getInstance()
        mc.execute {
            try {
                future.complete(task())
            } catch (t: Throwable) {
                future.completeExceptionally(t)
            } finally {
                pending.decrementAndGet()
            }
        }
        return future
    }

    /**
     * Convenience for HTTP handlers: submit + wait synchronously up to
     * [timeoutMs] milliseconds. Throws on timeout so the dispatcher returns
     * a clean 503 instead of holding the worker thread indefinitely.
     */
    fun <T : Any> submitAndWait(timeoutMs: Long = 2_000, task: () -> T): T {
        return try {
            submit(task).get(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (e: TimeoutException) {
            throw TickThreadTimeoutException(
                "tick-thread task did not complete within ${timeoutMs}ms (queue depth=${pending.get()})"
            )
        }
    }
}

class TickThreadTimeoutException(message: String) : RuntimeException(message)
