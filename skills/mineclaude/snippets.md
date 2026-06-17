# Snippets — proven `execute` patterns

Each block is a body you pass as `execute(code=...)`. They `return` a short
status string and use `say(...)` to narrate. Run each phase of a big task as its
own `execute` so you re-observe between phases.

## Chop a tree (scan, group by trunk, finish one tree at a time)

```python
# One scan for any log type.
all_logs = await findMultipleBlocks([
    "oak_log", "birch_log", "spruce_log", "jungle_log",
    "acacia_log", "dark_oak_log", "mangrove_log",
], 32)
flat = [b for blocks in all_logs.values() for b in blocks]
if not flat:
    return "No logs nearby"

# Group by (x, z) column — each column is one trunk. Finish a trunk before
# jumping trees: switching means a long walk and often an unreachable stub.
trunks = {}
for b in flat:
    trunks.setdefault((b["x"], b["z"]), []).append(b)

nearest = min(trunks.values(), key=lambda t: min(l["distance"] for l in t))
nearest.sort(key=lambda l: l["y"])   # bottom-up
for b in nearest:
    await breakBlockAt(b["x"], b["y"], b["z"])
    await collectItems()
return f"Chopped {len(nearest)} logs from one trunk"
```

## Mine a 3D ore/stone cluster (top-down, NOT bottom-up)

```python
# Highest y first: a block above your target occludes the crosshair and
# cascades "not target" errors. Clearing the top layer removes the occluders.
stones = await findBlocks("stone", 8, 30)
if not stones:
    return "No stone nearby"
stones.sort(key=lambda b: (-b["y"], b["distance"]))   # y descending, then nearest
mined = 0
for b in stones[:6]:                                   # cap the loop, re-observe after
    try:
        await breakBlockAt(b["x"], b["y"], b["z"])
        mined += 1
    except Exception as e:
        log(f"skip {b['x']},{b['y']},{b['z']}: {e}")
await collectItems(radius=10)
return f"Mined {mined} stone"
```

## Build a shelter (bill of materials → clear volume → build in order)

Run the phases below as separate `execute` calls so you re-observe (night? mob?
tool broke?) between them.

```python
# PHASE 0 — define the structure ONCE so the count can't drift from the build.
x0, z0 = -7, -23                 # one outer corner (from a site scan)
W, D, H = 5, 5, 3                # outer width, depth, wall height (layers)
gy = 88                          # standable ground y (feet cell from getHeightmap)
wall_block, roof_block = "cobblestone", "birch_planks"
door = (x0 + 2, z0 + D - 1)
door_cells = {(door[0], gy, door[1]), (door[0], gy + 1, door[1])}

def ring(y):                     # wall perimeter at one height, minus the door
    return [(x, y, z)
            for x in range(x0, x0 + W) for z in range(z0, z0 + D)
            if (x in (x0, x0 + W - 1) or z in (z0, z0 + D - 1))
            and (x, y, z) not in door_cells]

wall_cells = [c for dy in range(H) for c in ring(gy + dy)]   # already bottom-up
roof_cells = [(x, gy + H, z) for x in range(x0, x0 + W) for z in range(z0, z0 + D)]

# PHASE 1 — bill of materials. Place nothing until this passes.
inv = {i["name"]: i["count"] for i in await getInventory()}
need = {wall_block: len(wall_cells), roof_block: len(roof_cells)}
short = {b: n - inv.get(b, 0) for b, n in need.items() if inv.get(b, 0) < n}
if short:
    return f"Short before building: {short} — gather these first"
return "BOM ok — clear the volume next"
```

```python
# PHASE 2 — clear the whole VOLUME (walls + roof + overhang), not just the floor.
await equip("stone_pickaxe")
for y in range(gy, gy + H + 2):
    for x in range(x0, x0 + W):
        for z in range(z0, z0 + D):
            if not (await getBlock(x, y, z))["replaceable"]:
                await breakBlockAt(x, y, z)
await collectItems(radius=10)
return "Volume cleared — walls next"
```

```python
# PHASE 3 — walls bottom-up, then roof, then door LAST. placeBlock auto-equips
# the block, so re-equip a tool afterwards if you'll mine next.
for (x, y, z) in wall_cells:
    try:
        await placeBlock(wall_block, x, z, y=y)
    except Exception as e:
        log(f"wall {x},{y},{z}: {e}")
for (x, y, z) in roof_cells:
    try:
        await placeBlock(roof_block, x, z, y=y)
    except Exception as e:
        log(f"roof {x},{y},{z}: {e}")
await placeBlock("birch_door", door[0], door[1], y=gy)
return "Shell sealed — door in"
```

## Skip the night with a bed

```python
# A bed is two blocks; either half works. findBlocks returns both — take the
# nearest. sleepInBed confirms you actually slept and waits for morning; it
# fails loudly in daytime or with monsters too close, and reports night_skipped.
beds = await findBlocks("white_bed", 16, 2)   # whatever colour you crafted
if not beds:
    return "No bed nearby — craft one (3 wool + 3 planks) and place it first"
b = beds[0]
r = await sleepInBed(b["x"], b["y"], b["z"])
if not r["night_skipped"]:
    return f"Didn't skip the night ({r['message']}) — clear hostiles and retry"
return f"Slept through to morning (time={r['time']})"
```

## Smelt a batch

```python
# ceil(items / 1.5) planks of fuel; or 1 coal per 8 items. Round up.
await furnaceLoad("raw_iron", 3, "birch_planks", 2)   # returns immediately
await sleep(30)                                        # 3 items × ~10s each
out = await furnaceExtract()
return f"smelted: {out}"
```

## Status check with logging

```python
stats = await getStats()
log(f"Health: {stats['health']}  Hunger: {stats['hunger']}")
inv = await getInventory()
log(f"Items: {len(inv)}")
return "Status check complete"
```

## Fluids, obsidian, and a nether portal

For **buckets, use the dedicated `fillBucket` / `emptyBucket` primitives**, not
`use(...)`. They own the finicky aim geometry and verify the result. Do NOT try
to cast obsidian with `use("water_bucket", look_at=lava)` — that overwrites the
lava cell with water (no obsidian), and on recessed/open pools the eye-raycast
keeps missing into the rim or the block under your feet. `use` stays for
torches, flint & steel, and doors (aim at a block face).

**How obsidian actually forms** (learned the hard way — read before you try):
- Obsidian appears only when water flows **down onto a still lava SOURCE** from a
  *separate* cell above. Pouring water straight into the lava cell just replaces
  the lava with water — no obsidian.
- **Flowing lava + water = COBBLESTONE.** Only still *source* blocks convert. You
  also can't `fillBucket` flowing lava ("fluid is flowing") — scoop sources only.
- **Lava on open ground spreads ~4 blocks** → flowing lava (a hazard, and the
  wrong block). You must *contain* it. Don't fight natural recessed lava pools —
  build your own contained source.
- `emptyBucket` needs `item=` when you hold **both** a `water_bucket` and a
  `lava_bucket` (else it errors asking which to pour).

```python
# Cast obsidian — the reliable recipe: a 2-deep walled pit contains both fluids,
# water poured ABOVE the lava flows down onto the source -> obsidian. Validated.
bx, bz, by = -77, -113, 61          # column + floor y (lava goes at by)
await equip("diamond_pickaxe")
await breakBlockAt(bx, by + 1, bz)  # dig the pit 2 deep so the walls contain it
await breakBlockAt(bx, by, bz)

await emptyBucket(bx, by, bz, item="lava_bucket")            # contained lava source
r = await emptyBucket(bx, by + 1, bz, item="water_bucket")   # flows DOWN onto it
log(r["verified"])                                            # confirm it took

await fillBucket(bx, by + 1, bz)    # recover the water — reusable forever
await breakBlockAt(bx, by, bz)      # mine the obsidian (~9s, diamond pick)
await collectItems(5)
return "1 obsidian cast"
```

To batch a portal's 10–14 obsidian: refill empty buckets by `fillBucket`-ing
**still lava sources** at a lava lake (flowing lava won't scoop), then run the
pit recipe per lava bucket. One obsidian per lava bucket; the water is reused.

```python
# Build + light a portal. Place bottom row -> side columns -> top row so every
# block anchors to a neighbour. Frame plane z=59, x=-8..-5, y=69..73.
for x in range(-8, -4):                      # bottom row on the ground
    await placeBlock("obsidian", x, 59, y=69)
for y in (70, 71, 72):                        # side columns
    await placeBlock("obsidian", -8, 59, y=y)
    await placeBlock("obsidian", -5, 59, y=y)
for x in range(-8, -4):                       # top row
    await placeBlock("obsidian", x, 59, y=73)

# Light from the GROUND, aiming at a TOP block: the eye is well below it, so the
# real ray hits its DOWN face -> fire lands in the interior-top cell and the
# portal ignites while you stay outside the portal plane (no nether teleport).
# Stand within ~4.5 blocks first or use() will try (and fail) to path upward.
await goToPosition(-6, 60)
await use("flint_and_steel", look_at=(-6, 73, 59))
```

```python
# Wall torch: aim at a point on the wall face you want it on. To put a torch on
# the NORTH face of the block at (x,y,z), aim just outside that face.
await use("torch", look_at=(x + 0.5, y + 0.5, z - 0.49))
```
