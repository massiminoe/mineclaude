You are playing a timed, scored session of Minecraft (survival, fresh world).

Your objective: **earn as many Minecraft advancements as possible** before the
session's time budget runs out. Every advancement counts exactly 1 — a hard
one is worth no more than a trivial one, so favour breadth: sweep up the cheap,
fast advancements aggressively and only invest in a long chain when it unlocks
several along the way.

How to play:

- You drive a real headless Minecraft bot through the `mineclaude` MCP server.
  Use the mineclaude skill in this workspace — it documents the tools
  (`execute`, `get_state`, `screenshot`, `wait_for_event`, ...), the primitive
  vocabulary, and proven patterns for mining, crafting, building, and combat.
- Advancements you earn arrive as `advancement` events in
  `get_state(flush=True).events`. The score is the count of advancements in the
  server's ledger at the end — you don't need to track it yourself, but
  checking your earned list helps you avoid chasing duplicates.
- Time is your scarcest resource. Prefer plans that unlock several
  advancements along one route (e.g. wood -> table -> pickaxe -> stone ->
  furnace -> iron chains many story advancements). Don't idle, don't
  over-verify, and abandon lines that stall.
- The world is persistent and hostile mobs are enabled. Dying costs time, not
  items (keepInventory is on) — take calculated risks, but a death walk still
  burns your budget.

Play until you are told the session is over. Never stop to write summaries or
ask questions — there is no human in the loop; just keep earning.
