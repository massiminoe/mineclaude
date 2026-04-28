package com.mineclaude.bridge

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpHandler
import com.sun.net.httpserver.HttpServer
import org.slf4j.LoggerFactory
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicLong

/**
 * Thin wrapper around the JDK's built-in HttpServer.
 *
 * Routes are registered as `addRoute(method, path, handler)` and dispatched
 * on the server's worker pool. Handlers return a [BridgeResponse]; the
 * dispatcher handles JSON encoding + HTTP framing.
 *
 * No tick-thread scheduling lives here — handlers that need MC state will
 * delegate to a tick-thread submitter (added in Phase 1) so the worker pool
 * stays available for incoming requests.
 */
class HttpBridge(private val host: String, private val port: Int) {

    // serializeNulls so empty container slots emit `{"item": null, "count": 0}`
    // instead of `{"count": 0}`. Default Gson drops null map values, which
    // breaks the agent contract: `state['input']['item']` would KeyError on
    // empty slots. Side-effect-free for non-null fields.
    private val gson: Gson = GsonBuilder().serializeNulls().create()
    private val routes = mutableMapOf<RouteKey, RouteHandler>()
    private val startedAt = System.currentTimeMillis()
    private val requestsServed = AtomicLong(0)
    private val lastErrorTs = AtomicLong(0)
    private var server: HttpServer? = null

    fun start() {
        registerBuiltins()
        val srv = HttpServer.create(InetSocketAddress(host, port), 0)
        srv.executor = Executors.newFixedThreadPool(4) { r ->
            Thread(r, "mineclaude-bridge-http").apply { isDaemon = true }
        }
        srv.createContext("/", DispatcherHandler())
        srv.start()
        server = srv
        log.info("HTTP server listening on http://{}:{}/ ({} routes)", host, port, routes.size)
    }

    fun stop() {
        server?.stop(0)
        server = null
    }

    fun addRoute(method: String, path: String, handler: RouteHandler) {
        routes[RouteKey(method.uppercase(), path)] = handler
        PortedEndpoints.register(path)
    }

    private fun registerBuiltins() {
        addRoute("GET", "/health") {
            ok(
                mapOf(
                    "ok" to true,
                    "kind" to "native-mod",
                    "uptime_s" to (System.currentTimeMillis() - startedAt) / 1000,
                    "requests_served" to requestsServed.get(),
                    "last_error_ts" to lastErrorTs.get().takeIf { it > 0 },
                    "tick_thread_pending" to TickThread.pendingCount(),
                    "tick_thread_total" to TickThread.totalSubmitted(),
                    // Endpoints this mod has taken ownership of. Updated as
                    // each phase lands; agent reads this to know what to
                    // route here vs. the Minescript bridge.
                    "ported" to PortedEndpoints.list(),
                ),
                message = "healthy",
            )
        }
    }

    private inner class DispatcherHandler : HttpHandler {
        override fun handle(exchange: HttpExchange) {
            requestsServed.incrementAndGet()
            try {
                val key = RouteKey(exchange.requestMethod.uppercase(), exchange.requestURI.path)
                val handler = routes[key]
                if (handler == null) {
                    write(exchange, err("no route ${key.method} ${key.path}", status = 404))
                    return
                }
                write(exchange, handler.handle(exchange))
            } catch (t: Throwable) {
                lastErrorTs.set(System.currentTimeMillis())
                log.error("handler threw on {} {}", exchange.requestMethod, exchange.requestURI, t)
                // Unexpected: a handler raised. 500 is correct here so
                // the agent sees it as an infrastructure failure rather
                // than a semantic one.
                write(exchange, err(t.message ?: t.javaClass.simpleName, status = 500))
            } finally {
                exchange.close()
            }
        }
    }

    private fun write(exchange: HttpExchange, response: BridgeResponse) {
        val body = mapOf(
            "status" to response.status,
            "message" to response.message,
            "data" to response.data,
        )
        val payload = gson.toJson(body).toByteArray(StandardCharsets.UTF_8)
        exchange.responseHeaders.add("Content-Type", "application/json")
        exchange.sendResponseHeaders(response.httpStatus, payload.size.toLong())
        exchange.responseBody.use { it.write(payload) }
    }

    companion object {
        val log = LoggerFactory.getLogger("mineclaude-bridge.http")!!

        fun ok(data: Any? = null, message: String = "ok") =
            BridgeResponse("success", message, data ?: emptyMap<String, Any>(), 200)

        /**
         * Semantic error: the request was syntactically fine but the
         * handler couldn't fulfill it (item not in inventory, action
         * couldn't run, etc). Returns HTTP 200 with `status:"error"` —
         * matches the legacy bridge's `_err()` helper, which agents
         * already know how to read. For framing-level failures (bad JSON,
         * missing required params) pass `status = 400`; for unexpected
         * exceptions thrown by a handler the dispatcher emits 500.
         */
        fun err(message: String, status: Int = 200) =
            BridgeResponse("error", message, emptyMap<String, Any>(), status)

        @Suppress("unused") // wired in later phases
        fun partial(data: Any? = null, message: String = "partial") =
            BridgeResponse("partial", message, data ?: emptyMap<String, Any>(), 200)
    }
}

private data class RouteKey(val method: String, val path: String)

fun interface RouteHandler {
    fun handle(exchange: HttpExchange): BridgeResponse
}

/** Wire shape matches the Python bridge: `{status, message, data}`. */
class BridgeResponse(
    val status: String,
    val message: String,
    val data: Any,
    val httpStatus: Int,
)
