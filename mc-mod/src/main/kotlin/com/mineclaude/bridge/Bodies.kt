package com.mineclaude.bridge

import com.google.gson.Gson
import com.google.gson.JsonSyntaxException
import com.sun.net.httpserver.HttpExchange
import java.nio.charset.StandardCharsets

/**
 * Small JSON body parser shared by the write routes. Handles empty bodies
 * by returning an empty map so callers can `body["x"] ?: default` uniformly.
 *
 * Throws [BodyParseException] on malformed JSON; the route handler turns
 * that into a 400 with a clear message.
 */
private val gson = Gson()

internal fun HttpExchange.jsonBody(): Map<String, Any?> {
    val raw = requestBody.use { it.readBytes() }
    if (raw.isEmpty()) return emptyMap()
    val text = String(raw, StandardCharsets.UTF_8).trim()
    if (text.isEmpty()) return emptyMap()
    @Suppress("UNCHECKED_CAST")
    return try {
        gson.fromJson(text, Map::class.java) as? Map<String, Any?> ?: emptyMap()
    } catch (e: JsonSyntaxException) {
        throw BodyParseException("invalid JSON body: ${e.message}")
    }
}

internal class BodyParseException(message: String) : RuntimeException(message)
