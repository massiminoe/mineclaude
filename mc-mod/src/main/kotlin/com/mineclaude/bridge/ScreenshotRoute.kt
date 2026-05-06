package com.mineclaude.bridge

import com.google.gson.Gson
import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import org.slf4j.LoggerFactory
import java.io.ByteArrayOutputStream
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.Base64
import java.util.concurrent.TimeUnit

/**
 * `GET /screenshot` — single-frame X11 framebuffer grab.
 *
 * We deliberately don't use MC's `ScreenshotRecorder` /
 * `NativeImage.writeTo()` because they produce 0-byte PNGs on the ARM64
 * Mesa llvmpipe stack the deployment container actually renders with.
 * ffmpeg's x11grab is the only reliable capture path here, and the mod
 * runs in the same container as Xvfb so `:99` is reachable from a child
 * process.
 *
 * Optional aim params let Claude point the camera before the snap so the
 * shot isn't whatever direction Baritone happened to leave the player
 * facing. Either explicit `yaw`/`pitch` (degrees, MC convention: yaw 0=south,
 * 90=west, 180=north, -90=east; pitch 0=horizontal, -90=up, 90=down) or
 * `look_at_x`/`look_at_y`/`look_at_z` (point the eye at a world coord). The
 * new rotation persists after the capture — Baritone clobbers it on the next
 * movement command anyway.
 */
object ScreenshotRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.screenshot")!!
    private val gson = Gson()

    private const val DISPLAY = ":99"
    private const val WIDTH = 854
    private const val HEIGHT = 480
    private const val SIZE = "${WIDTH}x${HEIGHT}"
    private const val FFMPEG_TIMEOUT_S = 5L

    // After we mutate yaw/pitch on the tick thread we need the renderer to
    // produce at least one frame at the new orientation before ffmpeg grabs.
    // 4 ticks (~200ms) is comfortably past one full lerp cycle from prev→current
    // so the captured frame is fully settled, not interpolated.
    private const val AIM_SETTLE_MS = 200L

    fun register(bridge: HttpBridge) {
        bridge.addRawRoute("GET", "/screenshot") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange) {
        val params = parseQuery(ex.requestURI.rawQuery)
        val format = (params["format"] ?: "jpeg").lowercase()
        val quality = (params["quality"]?.toIntOrNull() ?: 80).coerceIn(1, 100)
        val raw = params["raw"]?.lowercase() in setOf("true", "1")

        if (format != "jpeg" && format != "png") {
            writeJsonError(ex, 400, "format must be 'jpeg' or 'png'")
            return
        }

        val aim = try {
            parseAim(params)
        } catch (e: IllegalArgumentException) {
            writeJsonError(ex, 400, e.message ?: "invalid aim params")
            return
        }

        if (aim != null) {
            try {
                applyAim(aim)
            } catch (e: Exception) {
                log.error("Aim failed", e)
                writeJsonError(ex, 500, "Aim failed: ${e.message}")
                return
            }
            try { Thread.sleep(AIM_SETTLE_MS) } catch (_: InterruptedException) {}
        }

        val bytes = try {
            captureFrame(format, quality)
        } catch (e: Exception) {
            log.error("Screenshot failed", e)
            writeJsonError(ex, 500, "Screenshot failed: ${e.message}")
            return
        }

        if (raw) {
            val mediaType = if (format == "png") "image/png" else "image/jpeg"
            ex.responseHeaders.add("Content-Type", mediaType)
            ex.sendResponseHeaders(200, bytes.size.toLong())
            ex.responseBody.use { it.write(bytes) }
            return
        }

        val data = mapOf(
            "image" to Base64.getEncoder().encodeToString(bytes),
            "format" to format,
            "width" to WIDTH,
            "height" to HEIGHT,
            "size_bytes" to bytes.size,
        )
        val payload = gson.toJson(
            mapOf("status" to "success", "message" to "Screenshot captured", "data" to data)
        ).toByteArray(StandardCharsets.UTF_8)
        ex.responseHeaders.add("Content-Type", "application/json")
        ex.sendResponseHeaders(200, payload.size.toLong())
        ex.responseBody.use { it.write(payload) }
    }

    private fun captureFrame(format: String, quality: Int): ByteArray {
        // Quality mapping matches bridge/screenshot.py:29 — JPEG only;
        // PNG ignores quality.
        val qualityArgs = if (format == "png") emptyList()
        else listOf("-q:v", (32 - quality * 31 / 100).coerceIn(1, 31).toString())
        val codec = if (format == "png") "png" else "mjpeg"

        val cmd = mutableListOf(
            "ffmpeg", "-y",
            "-f", "x11grab",
            "-video_size", SIZE,
            "-i", DISPLAY,
            "-frames:v", "1",
        )
        cmd.addAll(qualityArgs)
        cmd.addAll(listOf("-f", "image2", "-vcodec", codec, "pipe:1"))

        val proc = ProcessBuilder(cmd).redirectErrorStream(false).start()
        val stdout = ByteArrayOutputStream()
        val stderr = ByteArrayOutputStream()
        // Drain stderr concurrently — if the pipe fills, ffmpeg blocks.
        val stderrThread = Thread {
            try { proc.errorStream.copyTo(stderr) } catch (_: Exception) {}
        }.apply { isDaemon = true; start() }
        try {
            proc.inputStream.copyTo(stdout)
        } catch (e: Exception) {
            proc.destroyForcibly()
            throw RuntimeException("reading ffmpeg stdout failed: ${e.message}", e)
        }
        if (!proc.waitFor(FFMPEG_TIMEOUT_S, TimeUnit.SECONDS)) {
            proc.destroyForcibly()
            throw RuntimeException("ffmpeg screenshot timed out after ${FFMPEG_TIMEOUT_S}s")
        }
        stderrThread.join(500)
        if (proc.exitValue() != 0) {
            val tail = stderr.toString(StandardCharsets.UTF_8).takeLast(500)
            throw RuntimeException("ffmpeg failed (rc=${proc.exitValue()}): $tail")
        }
        val bytes = stdout.toByteArray()
        if (bytes.isEmpty()) throw RuntimeException("ffmpeg produced no output")
        return bytes
    }

    private fun writeJsonError(ex: HttpExchange, status: Int, message: String) {
        val payload = gson.toJson(
            mapOf("status" to "error", "message" to message, "data" to emptyMap<String, Any>())
        ).toByteArray(StandardCharsets.UTF_8)
        ex.responseHeaders.add("Content-Type", "application/json")
        ex.sendResponseHeaders(status, payload.size.toLong())
        ex.responseBody.use { it.write(payload) }
    }

    private sealed class Aim {
        data class YawPitch(val yaw: Float, val pitch: Float) : Aim()
        data class LookAt(val x: Double, val y: Double, val z: Double) : Aim()
    }

    private fun parseAim(params: Map<String, String>): Aim? {
        val yaw = params["yaw"]?.toFloatOrNull()
        val pitch = params["pitch"]?.toFloatOrNull()
        val lx = params["look_at_x"]?.toDoubleOrNull()
        val ly = params["look_at_y"]?.toDoubleOrNull()
        val lz = params["look_at_z"]?.toDoubleOrNull()
        val hasYawPitch = yaw != null || pitch != null
        val lookAtParts = listOf(lx, ly, lz).count { it != null }
        if (hasYawPitch && lookAtParts > 0) {
            throw IllegalArgumentException("pass either yaw/pitch or look_at_x/y/z, not both")
        }
        if (lookAtParts in 1..2) {
            throw IllegalArgumentException("look_at requires all of look_at_x, look_at_y, look_at_z")
        }
        if (lookAtParts == 3) return Aim.LookAt(lx!!, ly!!, lz!!)
        if (hasYawPitch) {
            // Either-or: missing one defaults to current orientation, applied
            // on the tick thread by [applyAim] which has live access to player.
            return Aim.YawPitch(yaw ?: Float.NaN, pitch ?: Float.NaN)
        }
        return null
    }

    private fun applyAim(aim: Aim) = TickThread.submitAndWait(2_000) {
        val player = MinecraftClient.getInstance().player
            ?: throw RuntimeException("no player loaded")
        when (aim) {
            is Aim.YawPitch -> {
                if (!aim.yaw.isNaN()) player.yaw = aim.yaw
                if (!aim.pitch.isNaN()) player.pitch = aim.pitch.coerceIn(-90f, 90f)
            }
            is Aim.LookAt -> WorldHelpers.lookAtPosition(player, aim.x, aim.y, aim.z)
        }
    }

    private fun parseQuery(rawQuery: String?): Map<String, String> {
        if (rawQuery.isNullOrEmpty()) return emptyMap()
        return rawQuery.split("&").mapNotNull { pair ->
            val eq = pair.indexOf('=')
            if (eq < 0) return@mapNotNull null
            val k = URLDecoder.decode(pair.substring(0, eq), StandardCharsets.UTF_8)
            val v = URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8)
            k to v
        }.toMap()
    }
}
