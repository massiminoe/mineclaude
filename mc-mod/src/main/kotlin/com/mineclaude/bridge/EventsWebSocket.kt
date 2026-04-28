package com.mineclaude.bridge

import org.java_websocket.WebSocket
import org.java_websocket.handshake.ClientHandshake
import org.java_websocket.server.WebSocketServer
import org.slf4j.LoggerFactory
import java.net.InetSocketAddress
import java.util.concurrent.CopyOnWriteArraySet

/**
 * WebSocket server for Phase 6 events (`/events`).
 *
 * Mirrors the legacy bridge's `_ws_clients` + `broadcast_event` pattern: a
 * thread-safe set of connected clients, a `broadcast` helper that
 * JSON-serializes once and ships text frames to each.
 *
 * Runs on its own listener port (default 8082) — separate from the JDK
 * HttpServer (8081) because that server doesn't natively support WS
 * upgrades. Stop is wired into `CLIENT_STOPPING` like the HTTP server.
 *
 * Java-WebSocket's `WebSocketServer` runs its own NIO selector loop, so
 * `send()` is non-blocking from the caller's POV — Fabric event callbacks
 * fire on the client tick thread and we want to return fast.
 */
class EventsWebSocket(host: String, port: Int) : WebSocketServer(InetSocketAddress(host, port)) {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.ws")!!
    private val clients = CopyOnWriteArraySet<WebSocket>()

    init {
        // Drop the connection on idle so a half-closed agent doesn't sit
        // forever on the server's client set.
        connectionLostTimeout = 60
    }

    override fun onOpen(conn: WebSocket, handshake: ClientHandshake) {
        clients.add(conn)
        log.info("ws: client connected ({} total) — {}", clients.size, conn.remoteSocketAddress)
    }

    override fun onClose(conn: WebSocket, code: Int, reason: String, remote: Boolean) {
        clients.remove(conn)
        log.info("ws: client disconnected ({} remain) code={}", clients.size, code)
    }

    override fun onMessage(conn: WebSocket, message: String) {
        // Broadcast-only stream — ignore any incoming text. Matches legacy.
    }

    override fun onError(conn: WebSocket?, ex: Exception) {
        // `conn` is null for server-level errors (e.g. socket bind failure).
        if (conn == null) {
            log.error("ws: server-level error", ex)
        } else {
            log.warn("ws: client error {} — dropping", conn.remoteSocketAddress, ex)
            clients.remove(conn)
        }
    }

    override fun onStart() {
        log.info("ws: listening on ws://{}/events", address)
    }

    /**
     * Broadcast [json] as a text frame to every connected client. Drops
     * any client whose send fails (closed mid-iteration, write buffer
     * exhausted, etc).
     */
    fun pushEvent(json: String) {
        if (clients.isEmpty()) return
        val dead = mutableListOf<WebSocket>()
        for (client in clients) {
            try {
                if (client.isOpen) client.send(json) else dead.add(client)
            } catch (t: Throwable) {
                log.warn("ws: send failed to {} — dropping", client.remoteSocketAddress, t)
                dead.add(client)
            }
        }
        if (dead.isNotEmpty()) clients.removeAll(dead.toSet())
    }

    fun clientCount(): Int = clients.size

    companion object {
        @Volatile private var instance: EventsWebSocket? = null

        /** Process-global accessor for the EventBus to push events. */
        fun current(): EventsWebSocket? = instance

        fun start(host: String, port: Int): EventsWebSocket {
            val ws = EventsWebSocket(host, port)
            // start() launches the NIO selector thread; non-blocking from here.
            ws.start()
            instance = ws
            return ws
        }

        fun shutdown() {
            val ws = instance ?: return
            try { ws.stop(1_000) } catch (t: Throwable) {
                LoggerFactory.getLogger("mineclaude-bridge.ws").warn("ws stop failed", t)
            }
            instance = null
        }
    }
}
