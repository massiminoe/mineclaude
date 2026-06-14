package com.mineclaude.bridge

import com.sun.net.httpserver.HttpExchange
import net.minecraft.client.MinecraftClient
import net.minecraft.enchantment.EnchantmentHelper
import net.minecraft.screen.ScreenHandler
import org.slf4j.LoggerFactory

/**
 * `POST /enchant {item, tier, [x,y,z]}`
 *
 * Enchants `item` at an enchanting table. Unlike the forging stations there
 * is no output slot — the table mutates the item in place and the result is
 * RANDOM (seeded server-side). The caller picks a TIER (1=top/cheapest,
 * 2=middle, 3=bottom/best), which maps to the three on-screen enchant buttons;
 * what enchantment(s) you actually get is not selectable.
 *
 *   EnchantmentScreenHandler slots:  0 = item,  1 = lapis lazuli
 *
 * Mechanics: place the item + up to `tier` lapis, click the tier's button via
 * clickButton(syncId, tier-1), let the server apply it, then shift the
 * enchanted item back. Success is detected by the XP-level drop (a successful
 * enchant always consumes tier levels) — robust regardless of what rolled.
 * The applied enchantments are read off the resulting item for truth-in-return.
 *
 * Two prerequisites the bridge can't fake and reports honestly on failure:
 *   - XP: the bot needs experience levels (>= the tier's displayed requirement
 *     to see/use it, and >= tier levels to pay). Earned via mining/smelting/combat.
 *   - Bookshelves: the displayed levels (and thus enchant quality) only climb
 *     toward 30 with up to 15 bookshelves ringing the table. A bare table caps low.
 */
object EnchantRoute {
    private val log = LoggerFactory.getLogger("mineclaude-bridge.enchant")!!

    private const val ITEM = 0
    private const val LAPIS = 1

    fun register(bridge: HttpBridge) {
        bridge.addRoute("POST", "/enchant") { ex -> handleEnchant(ex) }
    }

    private fun handleEnchant(ex: HttpExchange): BridgeResponse {
        val body = try { ex.jsonBody() } catch (e: BodyParseException) {
            return HttpBridge.err(e.message ?: "bad body", status = 400)
        }
        val item = (body["item"] as? String).orEmpty().removePrefix("minecraft:")
        if (item.isEmpty()) return HttpBridge.err("Missing 'item'", status = 400)
        val tier = ((body["tier"] as? Number)?.toInt() ?: 3).coerceIn(1, 3)
        val buttonId = tier - 1

        val pos = StationMenu.parsePos(
            (body["x"] as? Number)?.toInt(),
            (body["y"] as? Number)?.toInt(),
            (body["z"] as? Number)?.toInt(),
        ) ?: TickThread.submitAndWait(timeoutMs = 1_000) {
            StationMenu.findNearestBlock(setOf("enchanting_table"), radius = 16)
        } ?: return HttpBridge.err("No enchanting table nearby. Place one first.")

        StationMenu.ensureInReach(pos, "enchanting table")?.let { return it }

        var err: String? = null
        var enchantments: List<Map<String, Any?>> = emptyList()
        var cost = 0
        var lapisUsed = 0
        try {
            MenuClicker.withOpenedBlock(pos) { handler ->
                val (pi, ierr) = StationMenu.insertExact(handler, item, 1, ITEM)
                if (pi < 1) { err = ierr ?: "couldn't place $item"; return@withOpenedBlock }
                val (pl, _) = StationMenu.insertExact(handler, "lapis_lazuli", tier, LAPIS)
                if (pl < 1) {
                    err = "No lapis_lazuli in inventory — enchanting needs 1-3 lapis."
                    return@withOpenedBlock
                }
                Thread.sleep(MenuClicker.TICK_MS * 2)

                val before = StationMenu.enchantInfo(handler, ITEM)
                val xpBefore = StationMenu.playerLevel()

                TickThread.submitAndWait(timeoutMs = 2_000) {
                    val mc = MinecraftClient.getInstance()
                    mc.player ?: error("no player")
                    val mgr = mc.interactionManager ?: error("no interaction manager")
                    mgr.clickButton(handler.syncId, buttonId)
                    Unit
                }
                Thread.sleep(MenuClicker.TICK_MS * 3)

                val after = StationMenu.enchantInfo(handler, ITEM)
                val xpAfter = StationMenu.playerLevel()
                cost = (xpBefore - xpAfter).coerceAtLeast(0)

                // A successful enchant always spends levels and adds enchantments.
                val applied = cost > 0 || after.size > before.size
                if (!applied) {
                    err = "Enchant did not apply at tier $tier (have level $xpBefore). Need: " +
                        "level >= the tier's displayed requirement (place bookshelves around the " +
                        "table to raise it), >= $tier levels to pay, and >= $tier lapis lazuli."
                    return@withOpenedBlock
                }
                enchantments = after
                lapisUsed = pl
                // Return the now-enchanted item to the inventory.
                StationMenu.shiftMoveIfNonEmpty(handler, ITEM)
                Thread.sleep(MenuClicker.TICK_MS * 2)
            }
        } catch (t: Throwable) {
            return HttpBridge.err(t.message ?: t.javaClass.simpleName)
        }
        err?.let { return HttpBridge.err(it) }

        val names = enchantments.joinToString(", ") { "${it["name"]} ${it["level"]}" }
        log.info("enchant: {} tier {} -> [{}] (xp -{}) at {}", item, tier, names, cost, pos)
        return HttpBridge.ok(
            mapOf(
                "enchanted" to true,
                "item" to item,
                "tier" to tier,
                "enchantments" to enchantments,
                "xp_levels_spent" to cost,
                "lapis_used" to lapisUsed,
                "position" to mapOf("x" to pos.x, "y" to pos.y, "z" to pos.z),
                "method" to "real",
            ),
            "Enchanted $item -> [${names.ifEmpty { "?" }}] (xp -$cost)",
        )
    }
}

/**
 * Read the enchantments on the stack in [slot] as a list of {name, level}.
 * Empty list if the slot is empty or unenchanted. Kept on StationMenu so the
 * enchant route shares its tick-thread idioms; uses only the stable
 * EnchantmentHelper / ItemEnchantmentsComponent surface.
 */
internal fun StationMenu.enchantInfo(handler: ScreenHandler, slot: Int): List<Map<String, Any?>> =
    TickThread.submitAndWait(timeoutMs = 1_000) {
        val stack = handler.slots.getOrNull(slot)?.stack ?: return@submitAndWait emptyList()
        if (stack.isEmpty) return@submitAndWait emptyList()
        val comp = EnchantmentHelper.getEnchantments(stack)
        comp.enchantments.map { entry ->
            val name = entry.key.map { it.value.path }.orElse("unknown")
            mapOf<String, Any?>("name" to name, "level" to comp.getLevel(entry))
        }
    }
