package com.mineclaude.bridge

import com.google.gson.Gson
import com.sun.net.httpserver.HttpExchange
import org.slf4j.LoggerFactory
import java.io.ByteArrayOutputStream
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.Base64
import java.util.concurrent.TimeUnit

/**
 * `GET /screenshot` — single-frame X11 framebuffer grab.
 *
 * Mirrors `bridge/screenshot.py` bit-for-bit. We deliberately don't use
 * MC's `ScreenshotRecorder` / `NativeImage.writeTo()` because they
 * produce 0-byte PNGs on the ARM64 Mesa llvmpipe stack the deployment
 * container actually renders with. ffmpeg's x11grab is the only reliable
 * capture path here, and the mod runs in the same container as Xvfb so
 * `:99` is reachable from a child process.
 *
 * Defaults match legacy: jpeg, quality=80, 854x480, 5 s timeout.
 */
object ScreenshotRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.screenshot")!!
    private val gson = Gson()

    private const val DISPLAY = ":99"
    private const val WIDTH = 854
    private const val HEIGHT = 480
    private const val SIZE = "${WIDTH}x${HEIGHT}"
    private const val FFMPEG_TIMEOUT_S = 5L

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
