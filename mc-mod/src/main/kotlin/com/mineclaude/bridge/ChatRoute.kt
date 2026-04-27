package com.mineclaude.bridge

import com.google.gson.Gson
import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient

/**
 * `POST /chat` — send a chat message, slash command, or client-side
 * intercepted command (`#` Baritone, `\` Minescript script).
 *
 * Phase 2 first-port write endpoint. Routing semantics match the legacy
 * bridge (see `bridge/minescript_api.py:send_chat`):
 *
 *   - `#…` / `\…` → `sendChatMessage(message)`. These are intercepted
 *     client-side by Baritone's chat hook / Minescript's `\`-handler
 *     before the packet leaves; running them through the chat path is
 *     the only way to fire those listeners.
 *   - `/cmd…`     → `sendChatCommand("cmd…")` (strip the leading `/`).
 *   - plain text  → `sendChatCommand("tellraw @a {\"text\": \"[Claude] …\"}")`.
 *     Avoids signed-chat disconnect under `ONLINE_MODE=false`.
 *
 * Non-ASCII characters in plain messages are stripped (MC can't render
 * them), matching legacy.
 */
object ChatRoute {
    private val gson = Gson()

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/chat") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val message = (body["message"] as? String).orEmpty()
        if (message.isEmpty()) {
            return HttpBridge.err("Missing 'message' parameter", status = 400)
        }

        TickThread.submitAndWait(timeoutMs = 2_000) { sendOnTick(message) }
        return HttpBridge.ok(message = "Sent: $message")
    }

    private fun sendOnTick(message: String) {
        val player = MinecraftClient.getInstance().player
            ?: error("no player — not connected to a world")
        val handler = player.networkHandler

        when {
            message.startsWith("#") || message.startsWith("\\") -> {
                // Baritone / Minescript intercept on the chat path before
                // the packet ships. Going through sendChatMessage fires
                // their fabric chat hooks; for `#` Baritone returns early
                // and the message never reaches the server.
                handler.sendChatMessage(message)
            }
            message.startsWith("/") -> {
                handler.sendChatCommand(message.substring(1))
            }
            else -> {
                val clean = message.toCharArray().filter { it.code in 0..127 }.joinToString("")
                val textJson = gson.toJson(mapOf("text" to "[Claude] $clean"))
                handler.sendChatCommand("tellraw @a $textJson")
            }
        }
    }
}
