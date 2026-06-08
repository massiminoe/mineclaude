package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange

/**
 * `POST /use_item {item, hold_ms?}` — right-click in air with [item] held.
 *
 * Thin backwards-compatible shim over [UseRoute.performUse] with no aim: a
 * pure in-air use (eat, drink, throw snowball/pearl, cast rod, charge bow).
 * Equivalent to `/use {item, hold_ms}` with no `look_at`. The general form
 * (`/use`) adds aiming so the same path also fills/pours buckets and places
 * torches / lights flint & steel against a real raycast face.
 *
 * Preserves the legacy response shape `{used, item, hold_ms, result}` so
 * existing callers keep working. Hold duration is auto-detected from the
 * item unless `hold_ms` is passed (see [UseRoute]).
 */
object UseItemRoute {
    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/use_item") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty()
        val explicitHoldMs = (body["hold_ms"] as? Number)?.toLong()
        if (item.isEmpty()) return HttpBridge.err("Missing 'item' parameter", status = 400)
        if (explicitHoldMs != null && explicitHoldMs < 0) {
            return HttpBridge.err("hold_ms must be >= 0", status = 400)
        }

        return when (val outcome = UseRoute.performUse(item, lookAt = null, holdMsOverride = explicitHoldMs)) {
            is UseRoute.Outcome.Err -> HttpBridge.err(outcome.message, outcome.status)
            is UseRoute.Outcome.Ok ->
                if (!outcome.accepted) {
                    HttpBridge.err(
                        "use_item did nothing — $item may not be usable in air here " +
                            "(e.g. food requires hunger, ender pearl requires open sky)"
                    )
                } else {
                    HttpBridge.ok(
                        mapOf(
                            "used" to true,
                            "item" to item,
                            "hold_ms" to outcome.holdMs,
                            "result" to "accepted",
                        ),
                        "Used $item",
                    )
                }
        }
    }
}
