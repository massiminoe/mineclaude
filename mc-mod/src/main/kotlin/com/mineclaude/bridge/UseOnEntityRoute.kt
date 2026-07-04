package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.registry.Registries
import net.minecraft.util.Hand
import net.minecraft.util.hit.EntityHitResult
import net.minecraft.util.math.BlockPos
import org.slf4j.LoggerFactory

/**
 * `POST /use_on_entity {entity_id, item?}` — right-click an entity: the
 * use-key twin of `/attack`'s `attackEntity`, and the primitive behind the
 * whole animal-husbandry branch. Feeding (wheat on a cow), breeding (feed
 * two), leads, shears, milking (bucket on a cow), taming (bone on a wolf),
 * saddling, name tags, dyeing sheep — all the same click with a different
 * held item.
 *
 * Like `attackEntity`, `interactEntity` hits the explicit entity — no
 * crosshair raycast — which is why this route exists at all: `/use`'s
 * look-at + block-raycast path can't target an entity, so every entity
 * interaction was silently impossible. We aim the head anyway (recording +
 * server-side leniency), but the hit doesn't depend on it.
 *
 * Dispatch mimics a vanilla right-click's order: `interactEntityAtLocation`
 * first (armor stands care where you touch them), plain `interactEntity` on
 * PASS/FAIL fall-through. `ActionResult.isAccepted` is the truth signal.
 *
 * Deliberate exclusions:
 *  - **Vehicles** (boats, rafts, minecarts) are denied up front — boarding
 *    would trap the bot (dismount is a sneak gesture no route owns), so the
 *    click never happens. Rideable *mobs* (saddled horse/pig) can't be
 *    pre-denied the same way — feeding a horse is this route's bread and
 *    butter — so a click that ends with the bot mounted gets an immediate
 *    sneak-dismount backstop, reported honestly.
 *  - **Trading** — clicking a villager opens the merchant screen; we close
 *    it (same self-heal as `/use`) and return `status:"partial"` with
 *    `opened_screen` so the agent learns trading isn't driveable yet.
 *
 * Truth-in-return `{used, dispatch, item, entity:{id,type,name},
 * inventory_delta}`: `used` is the ActionResult; the delta is what actually
 * left/entered the inventory (wheat −1 on a successful feed, bucket→
 * milk_bucket on a milk). Love-mode is NOT verifiable client-side (hearts
 * are particles, not tracked data) — a feed that "took" shows the consumed
 * item; confirm a baby via /nearby/entities a few seconds later.
 */
object UseOnEntityRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.use_on_entity")!!

    /** Vanilla survival entity-interaction range (eye to target). */
    private const val ENTITY_REACH = 3.0

    /** Approach retries — the target can wander while Baritone walks. */
    private const val MAX_APPROACH_ATTEMPTS = 3

    /** Settle window so a screen-open / mount lands before we check. */
    private const val POST_CLICK_SETTLE_MS = 150L

    /** How long the sneak-dismount backstop holds the key before giving up. */
    private const val DISMOUNT_TIMEOUT_MS = 1_500L

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/use_on_entity") { ex -> handle(ex) }
    }

    private fun handle(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val entityId = (body["entity_id"] as? String ?: (body["entity_id"] as? Number)?.toInt()?.toString()).orEmpty()
        if (entityId.isEmpty()) {
            return HttpBridge.err("Missing 'entity_id' parameter", status = 400)
        }
        val item = (body["item"] as? String)?.takeIf { it.isNotEmpty() }

        // Resolve + gate BEFORE equipping or walking: a denied vehicle or a
        // missing entity shouldn't cost an equip shuffle or a Baritone trip.
        val first = resolveOnTick(entityId)
            ?: return HttpBridge.err("Entity $entityId not found nearby")
        if (isVehicle(first.type)) {
            return HttpBridge.err("Boarding boats/minecarts isn't supported — can't use $entityId (${first.type})")
        }

        if (item != null) {
            UseRoute.ensureMainhandHolds(item)?.let { return HttpBridge.err(it) }
        }

        // Walk within reach, re-resolving between attempts (the target moves).
        var snap = first
        var attempts = 0
        while (snap.dist > ENTITY_REACH) {
            if (attempts++ >= MAX_APPROACH_ATTEMPTS) {
                return HttpBridge.err(
                    "Couldn't get within reach of $entityId (${snap.type}) — " +
                        "still ${"%.1f".format(snap.dist)} blocks away after $attempts approaches"
                )
            }
            val nav = Navigation.navigateNear(BlockPos.ofFloored(snap.x, snap.y, snap.z))
            if (nav is Navigation.Result.Failed) {
                return HttpBridge.err("Couldn't reach $entityId (${snap.type}): ${nav.reason}")
            }
            snap = resolveOnTick(entityId)
                ?: return HttpBridge.err("Entity $entityId disappeared while approaching")
        }

        val before = UseRoute.snapshotCounts()

        val click = when (val c = TickThread.submitAndWait(timeoutMs = 2_000) { clickOnTick(entityId) }) {
            is Click.Error -> return HttpBridge.err(c.message)
            is Click.Gone -> return HttpBridge.err("Entity $entityId disappeared before the click")
            is Click.Done -> c
        }

        Thread.sleep(POST_CLICK_SETTLE_MS)
        val openedScreen = UseRoute.closeAnyScreen()
        val dismount = dismountIfMounted()

        val invDelta = UseRoute.computeDelta(before, UseRoute.snapshotCounts())

        val data = mutableMapOf<String, Any>(
            "used" to click.accepted,
            "dispatch" to click.dispatch,
            "item" to click.held,
            "entity" to mapOf("id" to click.id, "type" to click.type, "name" to click.name),
        )
        if (invDelta.isNotEmpty()) data["inventory_delta"] = invDelta
        val deltaStr = if (invDelta.isNotEmpty()) {
            " (" + invDelta.entries.joinToString(", ") { "${if (it.value > 0) "+" else ""}${it.value} ${it.key}" } + ")"
        } else ""

        log.info(
            "use_on_entity: {} on {} (id {}) accepted={} dispatch={} delta={}",
            click.held, click.type, click.id, click.accepted, click.dispatch, invDelta,
        )

        return when {
            openedScreen != null -> {
                data["opened_screen"] = openedScreen
                HttpBridge.partial(
                    data,
                    "Used ${click.held} on ${click.type} — opened a $openedScreen screen and closed it; " +
                        "screen-driven entity interactions (e.g. villager trading) aren't supported yet",
                )
            }
            dismount != null -> {
                data["mounted"] = true
                data["dismounted"] = dismount
                if (dismount) HttpBridge.partial(
                    data,
                    "Used ${click.held} on ${click.type} — that mounted the bot, so it dismounted; " +
                        "riding isn't supported",
                ) else HttpBridge.err(
                    "Used ${click.held} on ${click.type} — the bot mounted it and couldn't dismount; " +
                        "riding isn't supported"
                )
            }
            click.accepted -> HttpBridge.ok(data, "Used ${click.held} on ${click.type} (id ${click.id})$deltaStr")
            else -> HttpBridge.ok(
                data,
                "Clicked ${click.type} (id ${click.id}) with ${click.held} but nothing happened — " +
                    "the entity doesn't react to ${click.held}",
            )
        }
    }

    // -- tick-thread pieces -----------------------------------------------------

    private data class Snap(
        val id: Int, val type: String, val name: String,
        val x: Double, val y: Double, val z: Double, val dist: Double,
    )

    private fun resolveOnTick(entityId: String): Snap? = TickThread.submitAndWait(timeoutMs = 2_000) {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return@submitAndWait null
        val world = mc.world ?: return@submitAndWait null
        val target = EntityHelpers.matchEntity(world.entities, entityId) ?: return@submitAndWait null
        if (!target.isAlive || target.isRemoved) return@submitAndWait null
        Snap(
            id = target.id,
            type = EntityHelpers.typeId(target),
            name = target.name.string,
            x = target.x, y = target.y, z = target.z,
            dist = WorldHelpers.playerDistance(player, target.x, target.y, target.z),
        )
    }

    private sealed interface Click {
        data class Error(val message: String) : Click
        data object Gone : Click
        data class Done(
            val dispatch: String, val accepted: Boolean, val held: String,
            val id: Int, val type: String, val name: String,
        ) : Click
    }

    /** Aim at the entity and right-click it, vanilla dispatch order. Tick-thread only. */
    private fun clickOnTick(entityId: String): Click {
        val mc = MinecraftClient.getInstance()
        val player = mc.player ?: return Click.Error("no player — not connected to a world")
        val world = mc.world ?: return Click.Error("no world")
        val mgr = mc.interactionManager ?: return Click.Error("no interaction manager")
        WorldHelpers.ensureNoScreenOpen(player)

        val target = EntityHelpers.matchEntity(world.entities, entityId) ?: return Click.Gone
        if (!target.isAlive || target.isRemoved) return Click.Gone

        val held = player.mainHandStack.let {
            if (it.isEmpty) "empty_hand" else Registries.ITEM.getId(it.item).path
        }
        val centre = target.boundingBox.center
        WorldHelpers.lookAtPosition(player, centre.x, centre.y, centre.z)

        var dispatch = "at_location"
        var result = mgr.interactEntityAtLocation(
            player, target, EntityHitResult(target, centre), Hand.MAIN_HAND,
        )
        if (!result.isAccepted) {
            dispatch = "entity"
            result = mgr.interactEntity(player, target, Hand.MAIN_HAND)
        }
        if (result.isAccepted) player.swingHand(Hand.MAIN_HAND)

        return Click.Done(
            dispatch = dispatch, accepted = result.isAccepted, held = held,
            id = target.id, type = EntityHelpers.typeId(target), name = target.name.string,
        )
    }

    // -- vehicle handling ---------------------------------------------------------

    /**
     * Boats/rafts/minecarts by type-id suffix — covers every wood variant,
     * chest boats/rafts, and all minecart flavours without enumerating them.
     */
    private fun isVehicle(type: String): Boolean =
        type.endsWith("boat") || type.endsWith("raft") || type.endsWith("minecart")

    /**
     * Backstop for rideable mobs the denylist can't pre-filter (saddled
     * horse/pig/camel): if the click left the bot mounted, hold the sneak key
     * — vanilla's dismount gesture — until the vehicle lets go. Returns null
     * if the bot never mounted, else whether the dismount succeeded.
     */
    private fun dismountIfMounted(): Boolean? {
        val mounted = TickThread.submitAndWait(timeoutMs = 1_000) {
            MinecraftClient.getInstance().player?.hasVehicle() ?: false
        }
        if (!mounted) return null
        log.warn("use_on_entity: click mounted the bot — sneak-dismounting")
        try {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                MinecraftClient.getInstance().options.sneakKey.setPressed(true)
                Unit
            }
            val deadline = System.currentTimeMillis() + DISMOUNT_TIMEOUT_MS
            while (System.currentTimeMillis() < deadline) {
                Thread.sleep(100)
                val still = TickThread.submitAndWait(timeoutMs = 1_000) {
                    MinecraftClient.getInstance().player?.hasVehicle() ?: false
                }
                if (!still) return true
            }
            return false
        } finally {
            TickThread.submitAndWait(timeoutMs = 1_000) {
                MinecraftClient.getInstance().options.sneakKey.setPressed(false)
                Unit
            }
        }
    }
}
