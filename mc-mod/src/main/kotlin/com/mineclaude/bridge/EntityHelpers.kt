package com.mineclaude.bridge

import net.minecraft.entity.Entity
import net.minecraft.registry.Registries

/**
 * Entity lookup shared by the entity-targeting routes (`/attack`,
 * `/attack/ranged`, `/use_on_entity`). Lives here so each route resolves an
 * `entity_id` the same way — a divergent matcher would make "the id you got
 * from /nearby/entities" mean different things per route.
 */
internal object EntityHelpers {

    /**
     * Match [query] against [entities]: numeric-id exact match first (used by
     * the damage_taken reflex to retaliate against the exact attacker — skips
     * the name/type loop entirely so a stringified id can't accidentally
     * substring-match a mob name), then case-insensitive name/type equality,
     * then substring. The bot itself is never matched.
     */
    fun matchEntity(entities: Iterable<Entity>, query: String): Entity? {
        query.toIntOrNull()?.let { id ->
            for (entity in entities) {
                if (entity is net.minecraft.client.network.ClientPlayerEntity) continue
                if (entity.id == id) return entity
            }
            return null
        }
        val q = query.lowercase()
        for (entity in entities) {
            if (entity is net.minecraft.client.network.ClientPlayerEntity) continue
            val name = entity.name.string.lowercase().removePrefix("minecraft:")
            val type = Registries.ENTITY_TYPE.getId(entity.type).path.lowercase()
            if (q == name || q == type) return entity
            if (q in name || q in type) return entity
        }
        return null
    }

    /** Entity type id with the `minecraft:` namespace stripped (e.g. `"cow"`). */
    fun typeId(entity: Entity): String = Registries.ENTITY_TYPE.getId(entity.type).path
}
