package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import org.slf4j.LoggerFactory
import java.io.IOException
import java.net.URLDecoder
import java.nio.charset.StandardCharsets

/**
 * `GET /video/stream` — long-lived MJPEG stream for the monitor UI.
 *
 * Mirrors `bridge/server.py:handle_video_stream` bit-for-bit:
 *   - One persistent ffmpeg per client, x11grab from `:99`.
 *   - Optional brightness-lift filter from `MONITOR_VIDEO_FILTER` env
 *     (default `eq=gamma=2.0:brightness=0.08:contrast=1.15`); empty string
 *     disables. Stream-only — `/screenshot` keeps authentic lighting so
 *     Claude's vision tool sees what the player sees.
 *   - Frames split on JPEG SOI (FFD8) / EOI (FFD9), wrapped in
 *     `multipart/x-mixed-replace; boundary=frame` parts.
 *   - Client disconnect (IOException on write) → SIGKILL ffmpeg.
 *
 * JDK HttpServer streams chunked when sendResponseHeaders is called with
 * contentLength=0, so no manual Transfer-Encoding management needed.
 */
object VideoStreamRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.videostream")!!

    private const val DISPLAY = ":99"
    private const val SIZE = "854x480"
    private const val MAX_FPS = 15
    private const val DEFAULT_FPS = 10
    private const val DEFAULT_QUALITY = 5
    private const val DEFAULT_FILTER = "eq=gamma=2.0:brightness=0.08:contrast=1.15"

    fun register(bridge: HttpBridge) {
        bridge.addRawRoute("GET", "/video/stream") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange) {
        val params = parseQuery(ex.requestURI.rawQuery)
        val fps = (params["fps"]?.toIntOrNull() ?: DEFAULT_FPS).coerceIn(1, MAX_FPS)
        val quality = (params["quality"]?.toIntOrNull() ?: DEFAULT_QUALITY).coerceIn(2, 31)
        val filter = System.getenv("MONITOR_VIDEO_FILTER") ?: DEFAULT_FILTER

        val cmd = mutableListOf(
            "ffmpeg",
            "-f", "x11grab", "-r", fps.toString(), "-video_size", SIZE, "-i", DISPLAY,
        )
        if (filter.isNotEmpty()) cmd.addAll(listOf("-vf", filter))
        cmd.addAll(listOf("-vcodec", "mjpeg", "-q:v", quality.toString(), "-f", "mjpeg", "pipe:1"))

        val proc = try {
            ProcessBuilder(cmd).redirectError(ProcessBuilder.Redirect.DISCARD).start()
        } catch (e: Exception) {
            log.error("Failed to launch ffmpeg for video stream", e)
            ex.responseHeaders.add("Content-Type", "text/plain")
            ex.sendResponseHeaders(500, 0)
            ex.responseBody.use { it.write("ffmpeg launch failed: ${e.message}".toByteArray()) }
            return
        }

        log.info("Video stream client connected (fps={}, ffmpeg pid={})", fps, proc.pid())
        ex.responseHeaders.add("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        ex.responseHeaders.add("Cache-Control", "no-cache")
        ex.sendResponseHeaders(200, 0) // 0 → chunked

        val out = ex.responseBody
        try {
            val input = proc.inputStream
            val readBuf = ByteArray(65536)
            var buf = ByteArray(0)
            while (true) {
                val n = input.read(readBuf)
                if (n < 0) break
                buf += readBuf.copyOfRange(0, n)
                while (true) {
                    val start = indexOf(buf, SOI, 0)
                    if (start < 0) {
                        // No SOI yet — discard everything (keeps buf small).
                        buf = ByteArray(0)
                        break
                    }
                    val end = indexOf(buf, EOI, start + 2)
                    if (end < 0) {
                        // Partial frame — keep tail.
                        buf = buf.copyOfRange(start, buf.size)
                        break
                    }
                    val frame = buf.copyOfRange(start, end + 2)
                    buf = buf.copyOfRange(end + 2, buf.size)
                    val header = "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.size}\r\n\r\n"
                        .toByteArray(StandardCharsets.US_ASCII)
                    out.write(header)
                    out.write(frame)
                    out.write("\r\n".toByteArray(StandardCharsets.US_ASCII))
                    out.flush()
                }
            }
        } catch (_: IOException) {
            // Client disconnected — fall through to cleanup.
        } catch (t: Throwable) {
            log.warn("Video stream error", t)
        } finally {
            proc.destroyForcibly()
            try { proc.waitFor() } catch (_: InterruptedException) {}
            try { out.close() } catch (_: Exception) {}
            log.info("Video stream client disconnected, ffmpeg killed")
        }
    }

    private val SOI = byteArrayOf(0xFF.toByte(), 0xD8.toByte())
    private val EOI = byteArrayOf(0xFF.toByte(), 0xD9.toByte())

    private fun indexOf(haystack: ByteArray, needle: ByteArray, from: Int): Int {
        if (needle.isEmpty() || haystack.size - from < needle.size) return -1
        outer@ for (i in from..haystack.size - needle.size) {
            for (j in needle.indices) {
                if (haystack[i + j] != needle[j]) continue@outer
            }
            return i
        }
        return -1
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
