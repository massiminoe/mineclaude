package com.mineclaude.bridge

import net.fabricmc.api.ClientModInitializer
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents
import org.slf4j.LoggerFactory

/**
 * Mineclaude native bridge — entrypoint.
 *
 * Boots an HTTP server on 127.0.0.1:8081 that progressively takes over
 * endpoints from the Minescript-backed Python bridge on :8080. See
 * docs/superpowers/specs/2026-04-27-native-mod-bridge-plan.md for the
 * cutover order.
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
        // Phase 4 container manipulation — craft and smelt via clickSlot.
        // /equip armor was already native (Phase 2b); 4c retires the
        // cross-bridge sync barrier in EquipRoute now that nothing else
        // can leave a stale ScreenHandler open.
        CraftRoute.register(bridge)
        SmeltRoute.register(bridge)

        bridge.start()

        // Stop cleanly so a `/stop` from the launcher doesn't leak the
        // listener thread on next reload.
        ClientLifecycleEvents.CLIENT_STOPPING.register(
            ClientLifecycleEvents.ClientStopping {
                log.info("mineclaude bridge: stopping")
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
        val log = LoggerFactory.getLogger("mineclaude-bridge")!!
    }
}
