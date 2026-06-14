# Mental model — playing well

How to drive the bot competently. The mechanics are in primitives.md/tools.md;
this is the judgment that separates a bot that thrives from one that dies at
dusk with a wooden pickaxe.

## Observe before you commit

`get_state()` is your numbers — it carries position, health, hunger, biome, time,
inventory, equipped items, the current action, recent reflex fires, and buffered
events. Read it before deciding; don't act on a snapshot ten actions stale.

But the numbers are only half your eyes. **`screenshot(...)` is the other half,
and you should reach for it far more often than feels necessary.** The state
snapshot tells you *what* you have and *where* you are; it cannot tell you that a
tree is behind a wall, that the ore vein continues left, that your wall has a
gap, that the cow you're chasing wandered off a cliff, or that it's visibly dusk.
Look **before** you commit to navigating, mining, building, or fighting — and
look **again afterward** to confirm the world actually changed the way you
intended. A bot that acts on numbers alone builds a confident, wrong mental
picture and walks it straight into a wall. When in doubt, take the screenshot;
it's cheap and it's the only thing that catches the gap between what you think
the world is and what it is. Aim the camera deliberately with `look_at=[x,y,z]`
or `yaw`/`pitch`; after Baritone navigation the bot faces an arbitrary direction,
so a screenshot without aiming often shows you nothing useful.

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
full diamond trip. And wear what armor you have — `equip(piece, "head"|"chest"|
"legs"|"feet")` before any fight or deep run turns a lethal hit into a survivable
one (the bot's first death was a no-armor zombie kill).

## Take small steps

Long traversals (>~50 blocks) and big mutating loops (>~8 breaks/places per
`execute`) are blind — you can't observe the world while one runs. Break long
moves into legs at natural waypoints (surface, top of mine, biome edge); cap loops
and return to re-observe. Narrate transitions with `say(...)` on multi-step work
so players can follow ("got 4 iron, heading deeper") — not every step.

## Day / night

`get_state().player["time"]` is the raw tick. Hostiles spawn at night (tick
13000–23000 within each 24000-tick day). At dusk you have two answers: fortify
(or be somewhere you can fight), or **skip the night** — if you have a bed,
`sleepInBed(x, y, z)` fast-forwards to morning and resets your spawn point. It
confirms you actually fell asleep (daytime / monsters-too-close / obstructed fail
loudly with the reason) and returns `night_skipped`. A lone sleeper is enough to
skip the night on this server, so a returned `night_skipped: false` means you were
interrupted mid-sleep (took damage) rather than "nobody else slept". Beds are
cheap insurance: 3 wool + 3 planks, and one carried to a mining camp saves the
dusk scramble.

## Hunger

When hunger < 18 and you have food, eat (`useItem(food)`) before continuing long
work — at 6 you stop sprinting, at 0 you take damage. Cooked meats and bread are
high-saturation. Note the side effect: equipping food swaps your hand off your
tool — re-`equip` the pickaxe/sword after eating mid-task.

## Inventory and dropping items

`inventory_slots` in `get_state` is `"M/36"` — occupied of the 36 storage slots.
When it reads `36/36` you have **no empty slot**, and a craft output or a pickup
of a *new* item type silently fails (the classic "+0 chest (inventory full?)" —
the action reports success but nothing landed). Watch the count as it climbs:
once you're near full, stop and deal with it *before* the next craft or
`collectItems`, not after a silent loss. The fix is to **store** the surplus in a
chest, not to throw it away — `chestStore` keeps it; you've usually mined for
that loot, so dumping it is the last resort.

When you *do* need to shed items, know that **`discard` drops them on the ground
right next to you, and the bot auto-picks-up nearby item entities** — so a naive
discard followed by any movement (or just standing there) tends to suck the same
items straight back in, and a `collectItems` will definitely re-grab them. So:

- To free space, prefer `chestStore` (keeps the items) over `discard` (loses
  them) whenever a chest is reachable.
- If you genuinely want junk *gone* (cobblestone glut, rotten flesh), `discard`
  then **walk well away** (>~8 blocks) before the next `collectItems`, or the
  pickup radius will undo it. Don't `collectItems` in the spot you just dumped.
- Dropping an item to hand it to a player has the same trap — drop it *at their
  feet*, then move off, so you don't re-collect your own gift.

## Mining discipline

- **Descend with a staircase**, never straight down: step down one, forward one,
  repeat. You fall into lava or strand yourself otherwise; the staircase lets you
  walk back up without spending blocks.
- **Tools:** `breakBlockAt` auto-selects a tool that can harvest the block
  (pickaxe for stone/ore, axe for wood, shovel for dirt/sand), so you won't mine
  bare-handed even with a torch left in hand. It grabs the BEST suitable tool
  (highest tier → fastest), so unmanaged it'll spend your diamond pickaxe on
  cobblestone. To be conservative, `equip` the cheaper tool yourself — a tool you
  already hold that works is kept, never overridden. `attack` does NOT auto-equip
  — equip a sword before fighting.
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
4. **Side effect — placing swaps your hand to the block.** `breakBlockAt` re-picks
   a mining tool on its own, so a following break is fine — but `attack` doesn't,
   so re-`equip` your sword before fighting or you swing the block bare-handed.
5. For build sites, `getHeightmap(x0,z0,w,h)` once and reduce in Python — never
   loop a per-cell query (that trap ate minutes of wall-time on a 20×20 sweep).
   To inspect a specific *set* of cells (footprint preflight, re-checking known
   coords), batch them with `getBlocks([(x,y,z), ...])` — one tick, one
   round-trip. Looping `getBlock` instead pays one server tick *per cell*,
   served serially.

## Combat

`attack(entity_id)` fights to a kill — one call per kill, not per swing. It
runs a pathfinder + combat module concurrently: Baritone continuously paths
after the moving target (terrain-smart — it jumps, rounds corners, climbs)
while a fast loop swings the instant the mob is in reach. You don't sequence a
`goToPosition` before it — the approach is built in, and if the target is faster
than you or walled off it gives up with `out_of_reach` rather than hanging. Get
the id from `getNearbyEntities`/`findEntities`. When
you filter, match on `type` (the lowercase id — `sheep`, `zombie`), never `name`
(the display-cased label — `Sheep`); hand-filtering `getNearbyEntities()` on
`name == "sheep"` silently misses every mob. `findEntities` is case-insensitive,
so prefer it. Equip a sword first. The reflex layer already retaliates / flees on
`damage_taken`, and auto-retreats from a creeper that wanders within ~6 blocks
(it preempts whatever you're doing and walks you clear before the fuse can
light — a `cancelled` action plus a `hostile_nearby` reflex entry is that, not
your error). It does NOT auto-fight creepers — charging one into melee is how
you get blown up; after the retreat, you decide whether to shoot it, kite it, or
move on. (See events.md.) Don't redo what the reflex did — verify via `get_state`.

`attack` carries its own shield: if a shield is in your offhand (it auto-equips
one when that hand is free), the loop raises the guard between swings and drops
it to strike — so an in-melee block comes for free, you don't orchestrate it.
Keep a shield in the inventory and the bot fights with a guard up; put something
else in the offhand on purpose (a totem) and it's left alone.

`attackRanged(entity_id)` is the bow counterpart — fight a target with arrows
instead of a sword. Same one-call-per-kill contract, same id source. It
auto-equips a bow and owns the ballistics: it arcs each full-charge shot for
gravity and leads the moving target, so you just pick who to shoot. It's
**stationary** — it holds its ground and volleys, it does NOT chase or kite, so
the target has to stay in bow range with a clear line of sight; it ends with
`out_of_reach` (drifted out of range) or `no_line_of_sight` (a block in the way)
rather than repositioning. Keep a bow and arrows in the inventory or it ends
`out_of_ammo`/errors. This is the answer to the post-retreat creeper, a skeleton
you'd rather not close with, or anything you can hit from safety — reach for it
over `attack` whenever you don't want to be in melee.

For defense *outside* the swing rhythm, `block(duration_s, look_at=(x,y,z))`
raises a shield and holds it for that window without attacking (auto-equips the
shield to the offhand). A shield just sitting in the offhand does nothing —
`block` is what mitigates the hit when you're not swinging. It only protects the
direction you face, so pass `look_at` pointed at the threat. You can't `block`
and `attack` at once — use `block` to tank a skeleton's volley or a creeper's
approach (where you *don't* want to charge in), then `attack`. Check the
returned `blocking`.

## When something goes wrong

If `get_state` shows a `cancelled` action and a recent reflex, the reflex
preempted you — that's expected, not your error. Verify the recovery (position,
HP) and continue. If an `execute` returns `status:"failed"`, read `error` and
adapt; offer the player an alternative rather than silently retrying the same
thing.
