# Mental model — playing well

How to drive the bot competently. The mechanics are in primitives.md/tools.md;
this is the judgment that separates a bot that thrives from one that dies at
dusk with a wooden pickaxe.

## Observe before you commit

`get_state()` is your eyes — it carries position, health, hunger, biome, time,
inventory, equipped items, the current action, recent reflex fires, and buffered
events. Read it before deciding; don't act on a snapshot ten actions stale. Use
`screenshot(...)` only when you need *visual* context (verify a build, read a
sign, survey terrain) — the state snapshot already has the numbers. Aim the
camera deliberately with `look_at=[x,y,z]` or `yaw`/`pitch`; after Baritone
navigation the bot faces an arbitrary direction.

## One action at a time

`execute` is single-flight and blocking: the bot does exactly one thing at a
time, and your call returns when it's done. Don't try to overlap actions. If you
need to abort what's running (a reflex fired, the player said stop, you changed
your mind), call `interrupt()` — it's out-of-band and purges the slot. Actions
have a 5-minute timeout.

## Match preparation to how hard a task is to abort

Before anything that takes you far from base or runs uninterruptibly — deep
mining, long expeditions, fights you can't disengage — ask "what would I regret
not having out there?" Tools wear out, hunger ticks, drops pile up, mobs spawn in
shadows. One more pickaxe or stack of food is cheap; climbing 100 blocks back for
what you forgot isn't. A diamond run isn't *go mine* — it's *provision, then mine,
then return*. Budget durability: an iron pickaxe at remaining≈120 won't survive a
full diamond trip.

## Take small steps

Long traversals (>~50 blocks) and big mutating loops (>~8 breaks/places per
`execute`) are blind — you can't observe the world while one runs. Break long
moves into legs at natural waypoints (surface, top of mine, biome edge); cap loops
and return to re-observe. Narrate transitions with `say(...)` on multi-step work
so players can follow ("got 4 iron, heading deeper") — not every step.

## Day / night

`get_state().player["time"]` is the raw tick. Hostiles spawn at night (tick
13000–23000 within each 24000-tick day). Plan shelter and combat readiness around
the phase. At dusk, either be fortified or be somewhere you can fight.

## Hunger

When hunger < 18 and you have food, eat (`useItem(food)`) before continuing long
work — at 6 you stop sprinting, at 0 you take damage. Cooked meats and bread are
high-saturation. Note the side effect: equipping food swaps your hand off your
tool — re-`equip` the pickaxe/sword after eating mid-task.

## Mining discipline

- **Descend with a staircase**, never straight down: step down one, forward one,
  repeat. You fall into lava or strand yourself otherwise; the staircase lets you
  walk back up without spending blocks.
- **Equip the right tool first** — pickaxe for stone/ore, axe for wood, shovel for
  dirt/sand, sword for mobs. Bare-handed stone drops nothing and is ~6× slower.
- **Vertical (trees): bottom-up.** **3D clusters (ore/stone): highest y first** —
  a block above your target occludes the crosshair and cascades "not target"
  errors; clearing the top layer first removes the occluders.
- **`collectItems()` after breaking** — drops sit on the ground. After a multi-break
  run, a final `collectItems(radius=10)` sweeps what you walked past.

## Building discipline

1. **Bill of materials first.** Enumerate every cell, count by type, confirm
   inventory ≥ need, gather the shortfall *before* placing. Running dry mid-wall
   strands you outside at dusk.
2. **A flat heightmap is not a clear site.** `getHeightmap` finds standable ground,
   not open sky — canopy above the footprint silently blocks the roof. Clear the
   whole build *volume* (walls + roof + 1–2 above) before placing anything.
3. **Build in order:** clear volume → walls bottom-up → roof → door/lighting last.
   Clearing leaves *after* walls are up breaks your own walls (you mine whatever
   the eye-ray hits). Stacked structures must go bottom-up — a layer anchors on the
   row below; skip the bottom and everything above fails "no adjacent solid block".
4. **Side effect — placing swaps your hand to the block.** Re-`equip` your tool
   before the next break/attack, or you fall back to bare-hands speed.
5. For build sites, `getHeightmap(x0,z0,w,h)` once and reduce in Python — never
   loop a per-cell query (that trap ate minutes of wall-time on a 20×20 sweep).

## Combat

`attack(entity_id)` loops swings to a kill and auto-paths into melee — one call
per kill, not per swing. Get the id from `getNearbyEntities`/`findEntities`. Equip
a sword first. The reflex layer already retaliates / flees on `damage_taken`
(see events.md) — don't redo what it did, but verify it worked via `get_state`.

## When something goes wrong

If `get_state` shows a `cancelled` action and a recent reflex, the reflex
preempted you — that's expected, not your error. Verify the recovery (position,
HP) and continue. If an `execute` returns `status:"failed"`, read `error` and
adapt; offer the player an alternative rather than silently retrying the same
thing.
