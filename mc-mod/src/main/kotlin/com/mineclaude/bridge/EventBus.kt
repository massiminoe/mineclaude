package com.mineclaude.bridge

import com.google.gson.Gson
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents
import net.minecraft.client.network.ClientPlayerEntity
import org.slf4j.LoggerFactory

/**
 * Phase 6 — bridges Fabric client events to the events WebSocket.
 *
 * Hooks three event sources, all called on the client tick thread:
 *   - chat   (`ClientReceiveMessageEvents.GAME`)
 *   - death  (`ClientEntityEvents.ENTITY_UNLOAD` filtered to local player + dead)
 *   - respawn (state machine on local-player presence after a death)
 *
 * Wire shapes match the legacy bridge bit-for-bit so the agent's existing
 * dispatcher in `agent/agent.py:_handle_event` doesn't need any changes:
 *
 *   {"type": "chat",    "data": {"username": <str>, "message": <str>}}
 *   {"type": "death",   "data": {"message": "Player died"}}
 *   {"type": "respawn", "data": {"message": "Player respawned"}}
 *
 * Death detection nuance: yarn 1.21.5 doesn't ship the
 * `ClientLivingEntityEvents.LIVING_DEATH` callback in the version of
 * fabric-api we pin to, so we approximate it with `ENTITY_UNLOAD` for
 * the local player while the player's death screen is showing
 * (`player.isDead` is true on the client, but the more reliable signal
 * is `player.health <= 0`). Respawn is detected when the local player is
 * non-null + alive *after* we'd previously reported them dead.
 *
 * Both transitions are tracked via a single `wasDead` flag on the bus
 * that flips on each event, so we can't double-fire either direction.
 */
object EventBus {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.events")!!
    private val gson = Gson()

    // Player chat regex — matches `<Name> message`, optionally preceded by
    // [Not Secure] or other server-side tags. Mirrors the legacy poller's
    // `re.search(r"<(\w+)>\s*(.*)", msg)` — capturing group 1 is the
    // username, group 2 is the rest of the line.
    private val CHAT_REGEX = Regex("<(\\w+)>\\s*(.*)")
    // §x MC formatting and ANSI escape codes the chat pipeline can sneak
    // in — strip them before regex match so the username group isn't
    // polluted. Mirrors legacy `re.sub(r"\x1b\[[0-9;]*m|§.", "", raw)`.
    private val FORMATTING_STRIP = Regex("\\u001B\\[[0-9;]*m|§.")

    // True when our last broadcast for this player was a `death`. Flipped
    // back to false on respawn. Initial state is false because the agent
    // joins alive.
    @Volatile private var wasDead = false

    fun register() {
        registerChat()
        registerDeathRespawn()
        log.info("EventBus: hooked chat / death / respawn")
    }

    private fun registerChat() {
        ClientReceiveMessageEvents.GAME.register(
            ClientReceiveMessageEvents.Game { message, overlay ->
                // Overlay messages are tick-spam (action bar text, etc.) —
                // legacy Minescript chat listener doesn't fire on these.
                if (overlay) return@Game
                val raw = message.string ?: return@Game
                val cleaned = FORMATTING_STRIP.replace(raw, "").trim()
                val match = CHAT_REGEX.find(cleaned) ?: return@Game
                val username = match.groupValues[1]
                val text = match.groupValues[2].trim()
                // Skip command-echoes that leaked through. Same filter as
                // legacy chat_event_poller line 899.
                if (text.startsWith("/")) return@Game
                pushChat(username, text)
            }
        )
    }

    /**
     * Register a death+respawn signal. Implementation: each tick, check
     * the local player's state and emit a transition event when we
     * cross alive↔dead boundaries.
     *
     * Why not `ClientEntityEvents.ENTITY_UNLOAD`? On a server, the player
     * isn't unloaded on death — just put on the death screen with health
     * 0. So unload-based detection misses it. The cheapest reliable
     * signal is health-poll on `ClientTickEvents.END_CLIENT_TICK`, which
     * matches what the legacy bridge does at 5s cadence (only it polls
     * on its own thread). Polling at tick rate (50 ms) is so cheap it
     * doesn't need the legacy debouncer.
     */
    private fun registerDeathRespawn() {
        ClientTickEvents.END_CLIENT_TICK.register(
            ClientTickEvents.EndTick { client ->
                val player: ClientPlayerEntity? = client.player
                if (player == null) {
                    // Disconnected / pre-spawn / between worlds — leave
                    // wasDead alone; we'll re-evaluate on next tick.
                    return@EndTick
                }
                val dead = player.health <= 0f || player.isDead
                if (dead && !wasDead) {
                    wasDead = true
                    pushDeath()
                } else if (!dead && wasDead) {
                    wasDead = false
                    pushRespawn()
                }
            }
        )
    }

    private fun pushChat(username: String, message: String) {
        val ws = EventsWebSocket.current() ?: return
        if (ws.clientCount() == 0) return
        val payload = mapOf(
            "type" to "chat",
            "data" to mapOf("username" to username, "message" to message),
        )
        ws.pushEvent(gson.toJson(payload))
    }

    private fun pushDeath() {
        log.info("EventBus: player died, broadcasting death event")
        val ws = EventsWebSocket.current() ?: return
        val payload = mapOf("type" to "death", "data" to mapOf("message" to "Player died"))
        ws.pushEvent(gson.toJson(payload))
    }

    private fun pushRespawn() {
        log.info("EventBus: player respawned, broadcasting respawn event")
        val ws = EventsWebSocket.current() ?: return
        val payload = mapOf("type" to "respawn", "data" to mapOf("message" to "Player respawned"))
        ws.pushEvent(gson.toJson(payload))
    }

}
