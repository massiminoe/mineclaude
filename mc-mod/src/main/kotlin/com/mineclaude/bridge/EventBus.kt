package com.mineclaude.bridge

import com.google.gson.Gson
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents
import net.minecraft.client.network.ClientPlayerEntity
import net.minecraft.entity.SpawnGroup
import net.minecraft.item.Item
import net.minecraft.item.ItemStack
import net.minecraft.registry.Registries
import org.slf4j.LoggerFactory

/**
 * Bridges Fabric client events to the events WebSocket.
 *
 * All events share the same wire envelope:
 *
 *   {"type": "<event-type>", "data": {...}}
 *
 * Hooks (all called on the client tick thread unless noted):
 *   - chat   (`ClientReceiveMessageEvents.CHAT` for player chat,
 *             `ClientReceiveMessageEvents.GAME` for `/say`+`/tellraw`-wrapped chat)
 *   - death / respawn   (alive↔dead transitions in END_CLIENT_TICK)
 *   - damage_taken      (mixin on network handler stashes [pendingDamage];
 *                        END_CLIENT_TICK reads + clears + emits)
 *   - entered_lava      (END_CLIENT_TICK edge detection, 2-tick debounce
 *                        on entry; the agent's reflex handler awaits
 *                        escape completion, so no end-of-hazard event
 *                        is needed)
 *   - started_drowning  (END_CLIENT_TICK edge on air ≤ 60 while submerged;
 *                        same await-completion model as entered_lava)
 *   - tool_broke        (END_CLIENT_TICK detects the held damageable
 *                        stack vanishing without a slot change)
 *   - hostile_nearby    (END_CLIENT_TICK edge on a SpawnGroup.MONSTER mob
 *                        entering HOSTILE_RADIUS; scan throttled to every
 *                        few ticks. Informational only — the agent's
 *                        handler records it, never preempts.)
 *
 * Death detection nuance: yarn 1.21.5 doesn't ship the
 * `ClientLivingEntityEvents.LIVING_DEATH` callback in the version of
 * fabric-api we pin to, so we approximate it with a `health <= 0` poll.
 */
object EventBus {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.events")!!
    private val gson = Gson()

    // Player chat regex — matches `<Name> message`, optionally preceded by
    // [Not Secure] or other server-side tags.
    private val CHAT_REGEX = Regex("<(\\w+)>\\s*(.*)")
    // §x MC formatting and ANSI escape codes the chat pipeline can sneak
    // in — strip them before regex match.
    private val FORMATTING_STRIP = Regex("\\u001B\\[[0-9;]*m|§.")

    // Death state — flipped on each fire so we can't double-emit.
    @Volatile private var wasDead = false

    // Lava edge state. `lavaTickCount` debounces entry so a one-tick brush
    // (jumping over a 1-block lava channel) doesn't trip the reflex.
    @Volatile private var wasInLava = false
    @Volatile private var lavaTickCount = 0
    private const val LAVA_ENTRY_DEBOUNCE_TICKS = 2

    // Drowning edge state. Threshold of 60 air ticks (3s) before drowning
    // damage starts at 0 — gives a handler real time to react.
    @Volatile private var wasDrowning = false
    private const val DROWNING_AIR_THRESHOLD = 60

    // Damage attribution. Mixin on the damage-tilt packet handler stashes
    // [pendingDamage] off the tick thread; END_CLIENT_TICK reads + clears
    // it on a HP drop. `lastHealth` tracks the last observed value so we
    // can compute the amount even when the packet arrived a tick or two
    // before the health update.
    @Volatile private var lastHealth: Float = -1f

    @Volatile var pendingDamage: PendingDamage? = null
        @JvmStatic get
        @JvmStatic set
    private const val PENDING_DAMAGE_TTL_TICKS = 5
    @Volatile private var pendingDamageAge = 0

    /**
     * Stashed by the damage-tilt mixin on the network thread. Read +
     * cleared on the tick thread. All fields are immutable so a single
     * volatile reference is sufficient for cross-thread publication.
     */
    data class PendingDamage(
        val source: String,
        val attackerKind: String?,
        val attackerId: Int?,
        val attackerPos: Triple<Double, Double, Double>?,
    )

    // Tool-break tracking. We watch the currently-held mainhand stack: if
    // the previous tick saw a damageable item in slot N and this tick
    // slot N is empty (or replaced) without the player switching slots,
    // the stack was consumed by durability — emit `tool_broke`.
    @Volatile private var lastHeldSlot: Int = -1
    @Volatile private var lastHeldItem: Item? = null
    @Volatile private var lastHeldWasDamageable: Boolean = false

    // Hostile-proximity edge state. We track the set of hostile entity IDs
    // currently within HOSTILE_RADIUS so `hostile_nearby` fires once when a
    // mob crosses into range (same edge model as lava re-entry), not every
    // tick it lingers. The scan walks `world.entities`, so we throttle it to
    // every HOSTILE_SCAN_INTERVAL_TICKS — ~4x/sec is ample for an
    // informational signal and keeps the walk off the per-tick hot path.
    @Volatile private var nearbyHostiles: Set<Int> = emptySet()
    @Volatile private var hostileScanCounter = 0
    private const val HOSTILE_SCAN_INTERVAL_TICKS = 5
    private const val HOSTILE_RADIUS = 6.0
    private val HOSTILE_RADIUS_SQ = HOSTILE_RADIUS * HOSTILE_RADIUS

    fun register() {
        registerChat()
        registerTickStateMachines()
        log.info("EventBus: hooked chat / death / lava / drowning / damage / tool / hostile")
    }

    private fun registerChat() {
        // CHAT fires for player-authored chat (signed or profileless).
        ClientReceiveMessageEvents.CHAT.register(
            ClientReceiveMessageEvents.Chat { message, _, sender, _, _ ->
                val username = sender?.name ?: return@Chat
                val raw = message.string ?: return@Chat
                val cleaned = FORMATTING_STRIP.replace(raw, "").trim()
                val text = when {
                    cleaned.startsWith("<$username>") ->
                        cleaned.removePrefix("<$username>").trim()
                    else ->
                        CHAT_REGEX.find(cleaned)?.groupValues?.get(2)?.trim() ?: cleaned
                }
                if (text.isEmpty()) return@Chat
                pushEvent("chat", mapOf("username" to username, "message" to text))
            }
        )
        // GAME catches /say, /tellraw, and system messages that wrap chat
        // in `<Name> message` format.
        ClientReceiveMessageEvents.GAME.register(
            ClientReceiveMessageEvents.Game { message, overlay ->
                if (overlay) return@Game
                val raw = message.string ?: return@Game
                val cleaned = FORMATTING_STRIP.replace(raw, "").trim()
                val match = CHAT_REGEX.find(cleaned) ?: return@Game
                val username = match.groupValues[1]
                val text = match.groupValues[2].trim()
                if (text.startsWith("/")) return@Game
                pushEvent("chat", mapOf("username" to username, "message" to text))
            }
        )
    }

    /**
     * Single tick callback hosting every reflex-relevant state machine.
     * Order matters: damage attribution reads `lastHealth` from the tail
     * of the previous tick, so update it last.
     */
    private fun registerTickStateMachines() {
        ClientTickEvents.END_CLIENT_TICK.register(
            ClientTickEvents.EndTick { client ->
                val player: ClientPlayerEntity? = client.player
                if (player == null) {
                    // Disconnected / pre-spawn / between worlds — leave
                    // state alone; we'll re-evaluate on next tick.
                    return@EndTick
                }

                val currentHealth = player.health
                val dead = currentHealth <= 0f || player.isDead

                // 1. Death / respawn
                if (dead && !wasDead) {
                    wasDead = true
                    pushEvent("death", mapOf("message" to "Player died"))
                    // Reset transient state across death so we don't false-fire
                    // on respawn (player.air resets, isInLava clears, etc.).
                    lastHealth = -1f
                    pendingDamage = null
                    pendingDamageAge = 0
                    wasInLava = false
                    lavaTickCount = 0
                    wasDrowning = false
                    lastHeldItem = null
                    lastHeldWasDamageable = false
                    nearbyHostiles = emptySet()
                    hostileScanCounter = 0
                    return@EndTick
                }
                if (!dead && wasDead) {
                    wasDead = false
                    pushEvent("respawn", mapOf("message" to "Player respawned"))
                    // Don't fall through — lastHealth is -1, let next tick
                    // initialize cleanly.
                    lastHealth = currentHealth
                    return@EndTick
                }

                // 2. Damage taken — fire when health drops AND the hit
                // didn't kill (death event covers the kill case).
                if (lastHealth >= 0f && currentHealth < lastHealth && !dead) {
                    val pending = pendingDamage
                    val amount = lastHealth - currentHealth
                    val data = mutableMapOf<String, Any?>(
                        "amount" to amount.toDouble(),
                        "hp_before" to lastHealth.toDouble(),
                        "source" to (pending?.source ?: "unknown"),
                        "attacker_kind" to pending?.attackerKind,
                        "attacker_id" to pending?.attackerId,
                        "attacker_pos" to pending?.attackerPos?.let {
                            mapOf("x" to it.first, "y" to it.second, "z" to it.third)
                        },
                    )
                    pushEvent("damage_taken", data.filterValues { it != null }.mapValues { it.value!! })
                    pendingDamage = null
                    pendingDamageAge = 0
                }

                // Age out stale pending damage that never matched a HP drop
                // (e.g. server-cancelled damage, prediction mismatch).
                if (pendingDamage != null) {
                    pendingDamageAge++
                    if (pendingDamageAge > PENDING_DAMAGE_TTL_TICKS) {
                        pendingDamage = null
                        pendingDamageAge = 0
                    }
                }

                // 3. Lava edge — debounce entry to skip jump-overs. We
                // still flip wasInLava on exit so a fresh re-entry fires
                // a new entered_lava event, but we don't emit a paired
                // exited_lava: the agent's reflex handler awaits escape
                // completion before resuming Claude, so an end-of-hazard
                // signal would be redundant.
                val inLava = player.isInLava
                if (inLava) {
                    if (!wasInLava) {
                        lavaTickCount++
                        if (lavaTickCount >= LAVA_ENTRY_DEBOUNCE_TICKS) {
                            wasInLava = true
                            pushEvent("entered_lava")
                        }
                    }
                } else {
                    lavaTickCount = 0
                    wasInLava = false
                }

                // 4. Drowning edge — air drops below threshold while head
                // is in water. Use isSubmergedInWater (eyes in water) so
                // standing chest-deep doesn't fire. Same as lava: no paired
                // stopped_drowning event; the handler awaits its escape.
                val drowning = player.isSubmergedInWater && player.air <= DROWNING_AIR_THRESHOLD
                if (drowning && !wasDrowning) {
                    wasDrowning = true
                    pushEvent("started_drowning")
                } else if (!drowning) {
                    wasDrowning = false
                }

                // 5. Tool broke — held mainhand stack disappeared in-place.
                // Skip when the slot index changed (player switched hotbar)
                // or the held item changed identity (manual swap in slot).
                val heldSlot = player.inventory.selectedSlot
                val heldStack: ItemStack = player.mainHandStack
                val heldItem: Item? = if (heldStack.isEmpty) null else heldStack.item
                val heldDamageable = heldItem != null && heldStack.isDamageable
                if (lastHeldItem != null && lastHeldWasDamageable && heldSlot == lastHeldSlot) {
                    if (heldStack.isEmpty) {
                        // Stack vanished without a slot switch — the only
                        // vanilla path that does this is durability hitting
                        // the cap (or the player dropping; tracked but rare
                        // mid-action). Single fire per break.
                        val brokenItem = lastHeldItem
                        if (brokenItem != null) {
                            val name = Registries.ITEM.getId(brokenItem).path
                            pushEvent("tool_broke", mapOf("item" to name))
                        }
                    }
                }
                lastHeldSlot = heldSlot
                lastHeldItem = heldItem
                lastHeldWasDamageable = heldDamageable

                // 6. Hostile proximity — edge-triggered when a hostile mob
                // (SpawnGroup.MONSTER) crosses into HOSTILE_RADIUS. The scan
                // walks every loaded entity, so it's throttled to every
                // HOSTILE_SCAN_INTERVAL_TICKS rather than run each tick.
                // Informational only: the agent's handler just records the
                // fire for the next gameState, so we emit one event per newly
                // entered mob and let the handler coalesce bursts. IDs already
                // in `nearbyHostiles` stay suppressed until they leave + re-enter.
                hostileScanCounter++
                if (hostileScanCounter >= HOSTILE_SCAN_INTERVAL_TICKS) {
                    hostileScanCounter = 0
                    val world = client.world
                    if (world != null) {
                        val px = player.x
                        val py = player.y
                        val pz = player.z
                        val current = HashSet<Int>()
                        for (entity in world.entities) {
                            if (!entity.isAlive) continue
                            if (entity.type.spawnGroup != SpawnGroup.MONSTER) continue
                            val dx = entity.x - px
                            val dy = entity.y - py
                            val dz = entity.z - pz
                            val distSq = dx * dx + dy * dy + dz * dz
                            if (distSq > HOSTILE_RADIUS_SQ) continue
                            current.add(entity.id)
                            if (entity.id !in nearbyHostiles) {
                                pushEvent(
                                    "hostile_nearby",
                                    mapOf(
                                        "kind" to Registries.ENTITY_TYPE.getId(entity.type).path,
                                        "entity_id" to entity.id,
                                        "distance" to Math.sqrt(distSq),
                                        "pos" to mapOf(
                                            "x" to entity.x,
                                            "y" to entity.y,
                                            "z" to entity.z,
                                        ),
                                    ),
                                )
                            }
                        }
                        nearbyHostiles = current
                    }
                }

                // Health tracking lives at the tail — damage detection
                // above used the prior tick's value.
                lastHealth = currentHealth
            }
        )
    }

    /**
     * Generic event publisher — type + data envelope, JSON-serialized,
     * shipped over the events WS. Drops on the floor when no clients are
     * connected (the existing chat/death code did the same).
     */
    private fun pushEvent(type: String, data: Map<String, Any> = emptyMap()) {
        val ws = EventsWebSocket.current() ?: return
        if (ws.clientCount() == 0) return
        val payload = mapOf("type" to type, "data" to data)
        ws.pushEvent(gson.toJson(payload))
    }
}
