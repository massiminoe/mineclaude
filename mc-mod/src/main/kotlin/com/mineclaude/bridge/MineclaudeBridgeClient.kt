package com.mineclaude.bridge

import net.fabricmc.api.ClientModInitializer
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents
import org.slf4j.LoggerFactory

/**
 * Mineclaude native bridge — entrypoint.
 *
 * Boots an HTTP server on 0.0.0.0:8081 + an events WebSocket on 8082.
 * The bridge owns every endpoint the agent and frontend hit — there is
 * no separate Python bridge process anymore. The Phase 0–8 migration
 * history lives in docs/superpowers/specs/2026-04-*-native-mod-*.
 */
class MineclaudeBridgeClient : ClientModInitializer {
    override fun onInitializeClient() {
        log.info("mineclaude bridge: starting on {}:{}", BIND_HOST, BIND_PORT)
        val bridge = HttpBridge(BIND_HOST, BIND_PORT)

        // Phase 1 read endpoints. Order doesn't matter — each registers its
        // own routes and adds them to PortedEndpoints.
        PlayerStatusRoutes.register(bridge)
        NearbyRoutes.register(bridge)
        ProbeRoute.register(bridge)
        // Phase 2 simple writes. /chat ships routed; /equip and /discard
        // ship implemented but unrouted (legacy still owns them) until
        // Phase 2b adds the inventory-move helper.
        ChatRoute.register(bridge)
        EquipRoute.register(bridge)
        DiscardRoute.register(bridge)
        // Phase 3 world mutations — break/place/attack via interactionManager.
        // /collect stays on the legacy bridge for now (Baritone-driven walk).
        BreakRoute.register(bridge)
        PlaceRoute.register(bridge)
        AttackRoute.register(bridge)
        // Phase 4 container manipulation — craft and the /furnace/* trio
        // via clickSlot. /equip armor was already native (Phase 2b); 4c
        // retires the cross-bridge sync barrier in EquipRoute now that
        // nothing else can leave a stale ScreenHandler open.
        CraftRoute.register(bridge)
        FurnaceRoute.register(bridge)
        ChestRoute.register(bridge)
        // Phase 5 movement — Baritone-driven /goto, /mine, /follow, /stop,
        // /explore, /collect. Same chat-string substrate as the legacy
        // bridge (#goto / #mine / #follow / #stop / #explore through
        // sendChatMessage) — Baritone hooks the chat path client-side.
        // /goto polls player position for arrival; /collect runs a
        // Baritone-driven walk loop; the rest are fire-and-forget.
        MovementRoutes.register(bridge)
        GotoRoute.register(bridge)
        CollectRoute.register(bridge)
        // Heightmap region query — returns a w×h grid of standable-y values
        // (feet/head clearance, non-replaceable floor) in one tick-thread
        // submission. Replaces the per-cell `/standable_y` endpoint, which
        // tempted nested loops in the agent (a 20×20×7×7 sweep ate a 4-min
        // window before this existed). /goto and /place now resolve y
        // server-side via the same predicate when callers omit it.
        HeightmapRoute.register(bridge)
        // Single-cell inspection — preflight for building loops so the
        // agent can verify a cell is replaceable before attempting a
        // placement that would fail with "Block already at …".
        BlockRoute.register(bridge)
        // Direct-input swim-up. Used by the drowning reflex to surface the
        // player before handing off to Baritone — Baritone can't path from
        // a fully submerged start (PathNode map size: 1 → instant give-up).
        SurfaceRoute.register(bridge)
        // Phase 7 vision — /screenshot and /video/stream. Both shell out
        // to ffmpeg x11grab from `:99` — the same approach as the legacy
        // bridge, because NativeImage.writeTo() produces 0-byte PNGs on
        // ARM64 Mesa llvmpipe. The mod runs in the same container as
        // Xvfb so the display is reachable from a child process.
        ScreenshotRoute.register(bridge)
        VideoStreamRoute.register(bridge)

        bridge.start()

        // Phase 6 events WS — separate listener port (8082). Hooks
        // Fabric's chat / death / respawn events directly, replacing the
        // legacy bridge's polled health monitor + Minescript EventQueue
        // chat poller. JDK HttpServer doesn't speak WS upgrades, so this
        // runs as its own Java-WebSocket WebSocketServer.
        log.info("mineclaude events WS: starting on {}:{}", BIND_HOST, WS_PORT)
        EventsWebSocket.start(BIND_HOST, WS_PORT)
        EventBus.register()

        // Stop cleanly so a `/stop` from the launcher doesn't leak the
        // listener threads on next reload.
        ClientLifecycleEvents.CLIENT_STOPPING.register(
            ClientLifecycleEvents.ClientStopping {
                log.info("mineclaude bridge: stopping")
                EventsWebSocket.shutdown()
                bridge.stop()
            }
        )
    }

    companion object {
        // 0.0.0.0 — Docker maps host:8081 → container:8081 via docker-proxy,
        // which connects to the container's 8081 over the bridge network.
        // Binding to 127.0.0.1 would leave docker-proxy unable to reach us
        // (RST on connect from the host). We're already in a sandboxed
        // headless container, so external bind is safe here.
        const val BIND_HOST = "0.0.0.0"
        const val BIND_PORT = 8081
        // Events WS lives on a dedicated port — JDK's HttpServer doesn't
        // support WS upgrades, so Java-WebSocket gets its own listener.
        const val WS_PORT = 8082
        val log = LoggerFactory.getLogger("mineclaude-bridge")!!
    }
}
