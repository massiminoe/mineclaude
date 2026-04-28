package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient

/**
 * Phase 3 fire-and-forget Baritone routes: /stop, /explore, /follow, /mine.
 *
 * All four ship the same chat-string pattern as the legacy bridge
 * (`bridge/baritone.py`): build a `#`-prefixed Baritone command, dispatch
 * via `player.networkHandler.sendChatMessage` so Baritone's chat hook
 * intercepts before the packet leaves the client, return immediately with
 * `{"command": cmd}` payload.
 *
 * The agent owns completion detection — it polls `/status` and
 * `/nearby/blocks` after a `/mine` or `/follow`, and calls `/stop` when it's
 * done. This matches legacy semantics; the only thing changing is which
 * process runs the chat-send (Minescript RPC → in-process tick task).
 *
 * /goto is *not* here — it has arrival polling and lives in `GotoRoute.kt`.
 */
object MovementRoutes {
    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/stop") { handleStop() }
        bridge.addRoute("POST", "/explore") { handleExplore() }
        bridge.addRoute("POST", "/follow") { ex -> handleFollow(ex) }
        bridge.addRoute("POST", "/mine") { ex -> handleMine(ex) }
    }

    private fun handleStop(): BridgeResponse {
        val cmd = "#stop"
        sendBaritoneCommand(cmd)
        return HttpBridge.ok(mapOf("command" to cmd), "Stopped")
    }

    private fun handleExplore(): BridgeResponse {
        val cmd = "#explore"
        sendBaritoneCommand(cmd)
        return HttpBridge.ok(mapOf("command" to cmd), "Exploring")
    }

    private fun handleFollow(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val player = (body["player"] as? String).orEmpty()
        if (player.isEmpty()) {
            return HttpBridge.err("Missing 'player' parameter", status = 400)
        }
        // `distance` body field accepted but ignored — matches legacy. Baritone's
        // `#follow player` doesn't take a distance arg on the chat surface.
        val cmd = "#follow player $player"
        sendBaritoneCommand(cmd)
        return HttpBridge.ok(mapOf("command" to cmd), "Following $player")
    }

    private fun handleMine(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val block = (body["block"] as? String).orEmpty()
        if (block.isEmpty()) {
            return HttpBridge.err("Missing 'block' parameter", status = 400)
        }
        // `count` is optional; a missing/0/negative value omits the count
        // arg entirely (mine indefinitely until /stop), matching
        // bridge/baritone.py:mine.
        val count = (body["count"] as? Number)?.toInt() ?: 0
        val cmd = if (count > 0) "#mine $count $block" else "#mine $block"
        sendBaritoneCommand(cmd)
        val message = if (count > 0) "Mining $count $block" else "Mining $block"
        return HttpBridge.ok(mapOf("command" to cmd), message)
    }
}

/**
 * Send a Baritone `#…` chat command via the player's network handler. The
 * fabric chat-message hook fires before the message ships, so Baritone
 * intercepts it client-side and never sends a player-chat packet. This is
 * the same path ChatRoute uses for `#`/`\\` messages.
 */
internal fun sendBaritoneCommand(cmd: String) {
    TickThread.submitAndWait(timeoutMs = 2_000) {
        val player = MinecraftClient.getInstance().player
            ?: error("no player — not connected to a world")
        player.networkHandler.sendChatMessage(cmd)
        Unit
    }
}
