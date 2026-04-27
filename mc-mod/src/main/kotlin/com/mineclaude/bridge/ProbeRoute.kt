package com.mineclaude.bridge

import net.fabricmc.loader.api.FabricLoader
import net.minecraft.SharedConstants

/**
 * `/probe` — identifies this bridge as the native mod and lists its
 * capabilities. Intentionally NOT shape-equivalent to the legacy bridge's
 * /probe (which dumps Minescript Python APIs); the legacy /probe has no
 * programmatic consumers, so we replace it with an identification body
 * agents and humans can use to tell which bridge they're hitting.
 */
object ProbeRoute {
    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/probe") {
            val modVersion = FabricLoader.getInstance()
                .getModContainer("mineclaude_bridge")
                .map { it.metadata.version.friendlyString }
                .orElse("unknown")
            HttpBridge.ok(
                mapOf(
                    "kind" to "native-mod",
                    "version" to modVersion,
                    "mc_version" to SharedConstants.getGameVersion().name,
                    "ported" to PortedEndpoints.list(),
                    "capabilities" to mapOf(
                        "tick_thread_executor" to true,
                        "microcache_ms" to 50,
                    ),
                ),
                message = "native-mod probe",
            )
        }
    }
}
