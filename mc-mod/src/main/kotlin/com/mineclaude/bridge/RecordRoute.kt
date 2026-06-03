package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents
import org.slf4j.LoggerFactory
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * `/record/{start,stop,roll,status}` — single-file gameplay recorder.
 *
 * One ffmpeg taps the `:99` framebuffer (the same read-only x11grab as
 * `/screenshot` + `/video/stream`, so the three coexist) and writes ONE
 * continuous file per recording — not the rotating 5-minute segments the
 * old `entrypoint.sh` recorder produced. The container is **mp4** (H.264) for
 * native playback — QuickTime opens it directly, no VLC/IINA needed. Accepted
 * trade-off: a clean stop/roll/shutdown finalizes the moov atom via SIGTERM,
 * but an abrupt SIGKILL (hard crash before the shutdown hook runs) can leave
 * the still-open file unplayable. Encoder settings (5 fps, libx264 CRF 28,
 * keyframe every 5 s, `MONITOR_VIDEO_FILTER` brighten) match the old recorder
 * so the only behavioural change from it is "one file instead of many".
 *
 * Lifecycle:
 *   - **Auto-start:** when `RECORD_VIDEO=1`, the recorder kicks off the first
 *     tick the client is actually in a world — so the file captures gameplay,
 *     not the title/loading screen (the old recorder waited on `/health` in
 *     bash for the same reason). The gate reuses `END_CLIENT_TICK` rather than
 *     a connection event, and fires exactly once via an [AtomicBoolean].
 *   - `POST /record/start` — start if idle (optional `{name}` labels the file).
 *   - `POST /record/stop`  — finalize the current file.
 *   - `POST /record/roll`  — finalize current + open a fresh file. This is the
 *     "new video without restarting the container" trigger: the agent fires it
 *     on startup so each run gets its own file. Rotates an *active* recording
 *     only — a no-op if nothing's recording (so it self-gates on RECORD_VIDEO),
 *     never cold-starts. Use /record/start to begin from idle.
 *   - `GET  /record/status` — `{recording, file, started_at_ms, duration_s, dir}`.
 *
 * Stop/roll send SIGTERM (`Process.destroy`) so ffmpeg writes the mp4 moov
 * atom cleanly, with a [STOP_GRACE_S]s grace before SIGKILL. Launching
 * ffmpeg needs no MC state, so the route handlers run straight on the HTTP
 * worker pool with no tick-thread hop (same as ScreenshotRoute /
 * VideoStreamRoute); auto-start offloads to a daemon thread to keep the
 * render thread free.
 */
object RecordRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.record")!!

    private const val DISPLAY = ":99"
    private const val SIZE = "854x480"
    private const val FPS = 5
    private const val CRF = 28
    private const val DEFAULT_FILTER = "eq=gamma=2.0:brightness=0.08:contrast=1.15"
    private const val STOP_GRACE_S = 5L

    private val recordDir: String =
        System.getenv("RECORD_DIR")?.takeIf { it.isNotBlank() } ?: "/recordings"
    private val tsFormat = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")

    // All process-state mutation goes through `lock`. The HTTP worker pool and
    // the auto-start thread both call in, so start/stop/roll must be atomic.
    private val lock = Any()
    private var proc: Process? = null
    private var currentFile: String? = null
    private var startedAtMs: Long = 0
    private val autoStarted = AtomicBoolean(false)

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/record/start") { ex -> handleStart(ex) }
        bridge.addRoute("POST", "/record/stop") { handleStop() }
        bridge.addRoute("POST", "/record/roll") { ex -> handleRoll(ex) }
        bridge.addRoute("GET", "/record/status") { statusResponse() }

        if (System.getenv("RECORD_VIDEO") == "1") {
            // Kick the recorder the first tick we're actually in a world. Until
            // then this is a cheap null-compare; after it fires once the
            // AtomicBoolean short-circuits every subsequent tick.
            ClientTickEvents.END_CLIENT_TICK.register(
                ClientTickEvents.EndTick { client ->
                    if (!autoStarted.get() && client.world != null && client.player != null) {
                        if (autoStarted.compareAndSet(false, true)) {
                            // Off the render thread — fork/exec shouldn't stutter ticks.
                            Thread {
                                synchronized(lock) {
                                    if (proc?.isAlive != true) {
                                        try {
                                            startRecording(null)
                                            log.info("recorder auto-started (RECORD_VIDEO=1)")
                                        } catch (e: Exception) {
                                            log.error("recorder auto-start failed", e)
                                        }
                                    }
                                }
                            }.apply { isDaemon = true; name = "mineclaude-recorder-autostart" }.start()
                        }
                    }
                }
            )
        }
    }

    /** Finalize the open file on client shutdown so it isn't SIGKILLed mid-write. */
    fun shutdown() {
        synchronized(lock) { stopRecording() }
    }

    private fun handleStart(ex: HttpExchange): BridgeResponse {
        val name = try { nameParam(ex) } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        return synchronized(lock) {
            if (proc?.isAlive == true) {
                HttpBridge.ok(statusData(), "already recording")
            } else try {
                startRecording(name)
                HttpBridge.ok(statusData(), "recording started")
            } catch (e: Exception) {
                log.error("record start failed", e)
                HttpBridge.err("failed to start recorder: ${e.message}", status = 500)
            }
        }
    }

    private fun handleStop(): BridgeResponse = synchronized(lock) {
        if (proc?.isAlive != true) {
            HttpBridge.ok(statusData(), "not recording")
        } else {
            val file = currentFile
            stopRecording()
            HttpBridge.ok(statusData(), "recording stopped: $file")
        }
    }

    private fun handleRoll(ex: HttpExchange): BridgeResponse {
        val name = try { nameParam(ex) } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        return synchronized(lock) {
            if (proc?.isAlive != true) {
                // Roll rotates an *active* recording; it won't cold-start one
                // (use /record/start for that). So a blind roll-on-startup from
                // the agent is a no-op when RECORD_VIDEO=0 — no need for the host
                // agent and the container to agree on whether recording is on.
                HttpBridge.ok(statusData(), "not recording")
            } else {
                val prev = currentFile
                stopRecording()
                try {
                    startRecording(name)
                    HttpBridge.ok(statusData() + mapOf("previous_file" to prev), "rolled to new file")
                } catch (e: Exception) {
                    log.error("record roll failed", e)
                    HttpBridge.err("finalized $prev but new recorder failed: ${e.message}", status = 500)
                }
            }
        }
    }

    private fun statusResponse(): BridgeResponse = synchronized(lock) {
        HttpBridge.ok(statusData(), if (proc?.isAlive == true) "recording" else "idle")
    }

    // --- internals; callers hold `lock` (except the self-contained launch) ---

    private fun startRecording(name: String?) {
        File(recordDir).mkdirs()
        val ts = LocalDateTime.now().format(tsFormat)
        val label = name?.let { sanitize(it) }
        val base = if (label != null) "play-$ts-$label" else "play-$ts"
        val path = uniquePath(base)

        val filter = System.getenv("MONITOR_VIDEO_FILTER") ?: DEFAULT_FILTER
        val cmd = mutableListOf(
            "ffmpeg", "-nostdin", "-loglevel", "warning", "-y",
            "-f", "x11grab", "-r", FPS.toString(), "-video_size", SIZE, "-i", DISPLAY,
        )
        if (filter.isNotEmpty()) cmd.addAll(listOf("-vf", filter))
        cmd.addAll(
            listOf(
                "-c:v", "libx264", "-preset", "veryfast", "-crf", CRF.toString(),
                "-pix_fmt", "yuv420p", "-g", "25", "-an",
                path,
            )
        )

        val logFile = File("/tmp/recorder.log")
        val p = ProcessBuilder(cmd)
            .redirectOutput(ProcessBuilder.Redirect.appendTo(logFile))
            .redirectError(ProcessBuilder.Redirect.appendTo(logFile))
            .start()
        proc = p
        currentFile = path
        startedAtMs = System.currentTimeMillis()
        log.info("recorder started -> {} (ffmpeg pid {})", path, p.pid())
    }

    private fun stopRecording() {
        val p = proc ?: return
        if (p.isAlive) {
            // SIGTERM → ffmpeg flushes and writes the mp4 moov atom, then exits.
            p.destroy()
            if (!p.waitFor(STOP_GRACE_S, TimeUnit.SECONDS)) {
                log.warn("recorder didn't exit within {}s of SIGTERM, forcing", STOP_GRACE_S)
                p.destroyForcibly()
            }
            log.info("recorder stopped -> {}", currentFile)
        }
        proc = null
    }

    private fun statusData(): Map<String, Any?> {
        val alive = proc?.isAlive == true
        return mapOf(
            "recording" to alive,
            "file" to currentFile,
            "started_at_ms" to (if (alive) startedAtMs else null),
            "duration_s" to (if (alive) (System.currentTimeMillis() - startedAtMs) / 1000 else null),
            "dir" to recordDir,
        )
    }

    private fun nameParam(ex: HttpExchange): String? =
        (ex.jsonBody()["name"] as? String)?.trim()?.takeIf { it.isNotEmpty() }

    /** Append `-2`, `-3`, … if a same-second roll would collide. */
    private fun uniquePath(base: String): String {
        var candidate = "$recordDir/$base.mp4"
        var n = 2
        while (File(candidate).exists()) {
            candidate = "$recordDir/$base-$n.mp4"
            n++
        }
        return candidate
    }

    private fun sanitize(s: String): String =
        s.map { if (it.isLetterOrDigit() || it == '-' || it == '_') it else '-' }
            .joinToString("")
            .trim('-')
            .take(40)
            .ifEmpty { "rec" }
}
