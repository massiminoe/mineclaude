package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.Entity
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /attack` — swing at an entity by name or type. Native impl uses
 * [`ClientPlayerInteractionManager.attackEntity`] (the same path as a
 * real left-click on a mob), plus [Entity.swingHand] for the visible
 * arm swing. Replicates the legacy 3.5-block reach + auto-navigate
 * behaviour from `_attack_real`.
 *
 * Successful response: `{"attacked": true, "method": "real"}`.
 * On miss: `{"attacked": false, "error": "..."}`.
 */
object AttackRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.attack")!!

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/attack") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val entityId = (body["entity_id"] as? String).orEmpty()
        if (entityId.isEmpty()) {
            return HttpBridge.err("Missing 'entity_id' parameter", status = 400)
        }

        // First lookup on the tick thread — captures position + reach state.
        val lookup = TickThread.submitAndWait(timeoutMs = 2_000) { findOnTick(entityId) }
        when (lookup) {
            is FindResult.NotFound ->
                return HttpBridge.err("Entity $entityId not found")
            is FindResult.Error ->
                return HttpBridge.err(lookup.message)
            is FindResult.OutOfReach -> {
                if (!Navigation.navigateNear(BlockPos.ofFloored(lookup.x, lookup.y, lookup.z), reach = 2.5)) {
                    return HttpBridge.err("Could not navigate within melee range")
                }
                return swingAtEntity(entityId)
            }
            is FindResult.Ready -> return swingAtEntity(entityId)
        }
    }

    private sealed interface FindResult {
        data object NotFound : FindResult
        data class Error(val message: String) : FindResult
        data class Ready(val entityId: Int) : FindResult
        data class OutOfReach(val x: Double, val y: Double, val z: Double) : FindResult
    }

    private fun findOnTick(entityId: String): FindResult {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return FindResult.Error("no player")
        val world = mc.world ?: return FindResult.Error("no world")
        WorldHelpers.ensureNoScreenOpen(player)

        val target = matchEntity(world.entities, entityId) ?: return FindResult.NotFound
        val dist = WorldHelpers.playerDistance(player, target.x, target.y, target.z)
        return if (dist > 3.5) FindResult.OutOfReach(target.x, target.y, target.z)
        else FindResult.Ready(target.id)
    }

    private fun matchEntity(entities: Iterable<Entity>, query: String): Entity? {
        val q = query.lowercase()
        for (entity in entities) {
            if (entity is net.minecraft.client.network.ClientPlayerEntity) continue
            val name = entity.name.string.lowercase().removePrefix("minecraft:")
            val type = Registries.ENTITY_TYPE.getId(entity.type).path.lowercase()
            if (q == name || q == type) return entity
            // Partial match — agent often passes "zombie" expecting any
            // zombie. Mirrors the legacy `entity_id.lower() in ...` check
            // but stricter (substring on either side avoids false hits).
            if (q in name || q in type) return entity
        }
        return null
    }

    private fun swingAtEntity(entityId: String): BridgeResponse {
        val outcome = TickThread.submitAndWait(timeoutMs = 2_000) {
            val mc = MinecraftClient.getInstance()
            val player = mc.player ?: return@submitAndWait "no player"
            val world = mc.world ?: return@submitAndWait "no world"
            val mgr = mc.interactionManager ?: return@submitAndWait "no interaction manager"

            val target = matchEntity(world.entities, entityId)
                ?: return@submitAndWait "Entity $entityId not found"

            WorldHelpers.lookAtPosition(player, target.x, target.y, target.z)
            mgr.attackEntity(player, target)
            player.swingHand(Hand.MAIN_HAND)
            ""
        }
        if (outcome.isNotEmpty()) {
            return HttpBridge.err(outcome)
        }
        log.info("attack: attacked {} (real)", entityId)
        return HttpBridge.ok(mapOf("attacked" to true, "method" to "real"), "Attacked $entityId")
    }
}
