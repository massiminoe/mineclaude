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
 * old `entrypoint.sh` recorder produced. The container is **fragmented mp4**
 * (H.264, `+frag_keyframe+empty_moov`) for native playback — QuickTime opens
 * it directly, no VLC/IINA needed. Fragmenting means a small moov is written
 * up front and each GOP is flushed as a self-contained fragment, so there's no
 * trailing moov atom to lose: a file killed mid-write (hard crash, or a
 * `docker compose down` that SIGKILLs the container before the JVM's
 * CLIENT_STOPPING hook can stop us cleanly) stays playable up to the last
 * completed fragment. Encoder settings (libx264 CRF 28, keyframe every 5 s,
 * `MONITOR_VIDEO_FILTER` brighten) match the old recorder so the only
 * behavioural change from it is "one file instead of many".
 *
 * Capture rate defaults to [DEFAULT_FPS] and is set by `RECORD_FPS`, or per
 * call via `{fps}` on start/roll (a bare roll keeps the live rate). The ceiling
 * is [MAX_FPS] — options.txt pins the game to `maxFps:30`, and x11grab can only
 * sample what the framebuffer actually holds, so a higher ask buys duplicates.
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
 * Stop/roll send SIGTERM (`Process.destroy`) so ffmpeg flushes the final
 * fragment cleanly, with a [STOP_GRACE_S]s grace before SIGKILL (fragmenting
 * means an ungraceful exit only loses the in-flight fragment, not the file).
 * Launching ffmpeg needs no MC state, so the route handlers run straight on the HTTP
 * worker pool with no tick-thread hop (same as ScreenshotRoute /
 * VideoStreamRoute); auto-start offloads to a daemon thread to keep the
 * render thread free.
 */
object RecordRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.record")!!

    private const val DISPLAY = ":99"
    private const val SIZE = "854x480"
    private const val DEFAULT_FPS = 5
    // Ceiling is what the game actually paints: options.txt pins `maxFps:30`,
    // so asking x11grab for more than that just duplicates frames.
    private const val MAX_FPS = 30
    private const val CRF = 28
    private const val DEFAULT_FILTER = "eq=gamma=2.0:brightness=0.08:contrast=1.15"
    private const val STOP_GRACE_S = 5L

    private val recordDir: String =
        System.getenv("RECORD_DIR")?.takeIf { it.isNotBlank() } ?: "/recordings"

    /** `RECORD_FPS` sets the default capture rate; start/roll can override per call. */
    private val envFps: Int =
        (System.getenv("RECORD_FPS")?.trim()?.toIntOrNull() ?: DEFAULT_FPS).coerceIn(1, MAX_FPS)
    private val tsFormat = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")

    // All process-state mutation goes through `lock`. The HTTP worker pool and
    // the auto-start thread both call in, so start/stop/roll must be atomic.
    private val lock = Any()
    private var proc: Process? = null
    private var currentFile: String? = null
    private var currentFps: Int = envFps
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
                                            startRecording(null, envFps)
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
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val name = nameParam(body)
        val fps = fpsParam(body)
        return synchronized(lock) {
            if (proc?.isAlive == true) {
                HttpBridge.ok(statusData(), "already recording")
            } else try {
                startRecording(name, fps ?: envFps)
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
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val name = nameParam(body)
        val fps = fpsParam(body)
        return synchronized(lock) {
            if (proc?.isAlive != true) {
                // Roll rotates an *active* recording; it won't cold-start one
                // (use /record/start for that). So a blind roll-on-startup from
                // the agent is a no-op when RECORD_VIDEO=0 — no need for the host
                // agent and the container to agree on whether recording is on.
                HttpBridge.ok(statusData(), "not recording")
            } else {
                val prev = currentFile
                // A bare roll keeps the live rate; `{fps}` re-opens at a new one.
                val next = fps ?: currentFps
                stopRecording()
                try {
                    startRecording(name, next)
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

    private fun startRecording(name: String?, fps: Int) {
        File(recordDir).mkdirs()
        val ts = LocalDateTime.now().format(tsFormat)
        val label = name?.let { sanitize(it) }
        val base = if (label != null) "play-$ts-$label" else "play-$ts"
        val path = uniquePath(base)

        val filter = System.getenv("MONITOR_VIDEO_FILTER") ?: DEFAULT_FILTER
        val cmd = mutableListOf(
            "ffmpeg", "-nostdin", "-loglevel", "warning", "-y",
            "-f", "x11grab", "-r", fps.toString(), "-video_size", SIZE, "-i", DISPLAY,
        )
        if (filter.isNotEmpty()) cmd.addAll(listOf("-vf", filter))
        cmd.addAll(
            listOf(
                "-c:v", "libx264", "-preset", "veryfast", "-crf", CRF.toString(),
                // Keyframe every 5s at any rate — that's also the fragment size,
                // so it sets how much a SIGKILLed file loses at the tail.
                "-pix_fmt", "yuv420p", "-g", (fps * 5).toString(), "-an",
                // Fragmented mp4: a small moov is written up front and each GOP is
                // flushed as a self-contained fragment, so there's NO trailing moov
                // atom to lose. A SIGKILLed file (hard crash / `docker compose down`
                // before the JVM's CLIENT_STOPPING shutdown can SIGTERM us) stays
                // playable up to the last completed fragment — no clean stop needed.
                // fMP4 is the HLS/DASH container, so QuickTime still opens it natively.
                "-movflags", "+frag_keyframe+empty_moov", "-flush_packets", "1",
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
        currentFps = fps
        startedAtMs = System.currentTimeMillis()
        log.info("recorder started -> {} @{}fps (ffmpeg pid {})", path, fps, p.pid())
    }

    private fun stopRecording() {
        val p = proc ?: return
        if (p.isAlive) {
            // SIGTERM → ffmpeg flushes the final fragment and trailer, then exits.
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
            "fps" to (if (alive) currentFps else null),
            "default_fps" to envFps,
            "started_at_ms" to (if (alive) startedAtMs else null),
            "duration_s" to (if (alive) (System.currentTimeMillis() - startedAtMs) / 1000 else null),
            "dir" to recordDir,
        )
    }

    private fun nameParam(body: Map<String, Any?>): String? =
        (body["name"] as? String)?.trim()?.takeIf { it.isNotEmpty() }

    /** Null when absent — the caller decides whether that means env or "keep current". */
    private fun fpsParam(body: Map<String, Any?>): Int? =
        (body["fps"] as? Number)?.toInt()?.coerceIn(1, MAX_FPS)

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
