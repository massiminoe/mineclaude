package com.mineclaude.bridge

import net.minecraft.client.MinecraftClient
import net.minecraft.item.ItemStack
import net.minecraft.registry.Registries

/**
 * `/status` — port of the Minescript-backed status read.
 *
 * Shape contract is documented in
 * `docs/superpowers/specs/2026-04-27-native-mod-phase1-response-shapes.md`.
 * Snapshots are read on the tick thread (MC requires it) and shared via a
 * 50 ms TickCache so a burst of /status + /nearby/... doesn't hit the tick
 * queue three times per Claude iteration.
 */
object PlayerStatusRoutes {
    private val statusCache = TickCache(ttlMs = 50, read = ::readStatusOnTick)

    fun register(bridge: HttpBridge) {
        bridge.addRoute("GET", "/status") {
            HttpBridge.ok(statusCache.get(), message = "Status retrieved")
        }
    }

    /**
     * Captures a player snapshot. Must be called on the tick thread —
     * [TickCache] enforces this via [TickThread.submit].
     */
    private fun readStatusOnTick(): Map<String, Any> {
        val mc = MinecraftClient.getInstance()
        val player = mc.player
        val world = mc.world

        if (player == null || world == null) {
            // Pre-world load (main menu, lobby): emit a zeroed snapshot
            // matching the legacy shape so callers don't crash.
            return mapOf(
                "position" to mapOf("x" to 0.0, "y" to 0.0, "z" to 0.0),
                "health" to 0.0,
                "hunger" to 0,
                "inventory" to emptyList<Map<String, Any>>(),
                "biome" to "unknown",
                "time" to 0,
            )
        }

        val pos = player.pos
        // `world.timeOfDay` is the persistent day-ticks counter, matching
        // the legacy `world_info.day_ticks` field. `world.time` is total
        // elapsed ticks and would diverge after sleeping.
        val time = world.timeOfDay

        val biome = try {
            val biomeKey = world.getBiome(player.blockPos).key
            // key returns Optional<RegistryKey<Biome>> — `.get().value`
            // gives an Identifier whose path is the biome name.
            biomeKey.map { it.value.path }.orElse("unknown")
        } catch (t: Throwable) {
            "unknown"
        }

        return mapOf(
            "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
            "health" to player.health.toDouble(),
            "hunger" to player.hungerManager.foodLevel,
            "inventory" to readInventory(player.inventory),
            "biome" to biome,
            "time" to time,
            // Held hotbar slot (0..8). Useful for diagnosing "why is the
            // wrong tool in mainhand" — the source of truth on the client.
            "held_slot" to player.inventory.selectedSlot,
        )
    }

    private fun readInventory(inv: net.minecraft.entity.player.PlayerInventory): List<Map<String, Any>> {
        val result = mutableListOf<Map<String, Any>>()
        for (slot in 0 until inv.size()) {
            val stack: ItemStack = inv.getStack(slot)
            if (stack.isEmpty) continue
            // Identifier.path strips the `minecraft:` prefix the same way
            // the legacy bridge does, e.g. `oak_planks` not `minecraft:oak_planks`.
            val name = Registries.ITEM.getId(stack.item).path
            if ("air" in name) continue
            result.add(
                mapOf(
                    "name" to name,
                    "count" to stack.count,
                    "slot" to slot,
                )
            )
        }
        return result
    }
}
