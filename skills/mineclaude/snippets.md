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

## Fluids, obsidian, and a nether portal (the unified `use`)

`use(item, look_at=(x,y,z))` is the one right-click for buckets, torches, flint
& steel, doors — it aims the eye and dispatches on the real raycast. The key is
*where you aim*: a point on the water source fills, a point on a block face
places against that face.

```python
# Cast obsidian: pour water onto a lava SOURCE (flowing lava -> cobblestone).
# One pour flows across a pool and converts every source it touches.
r = await use("bucket", look_at=(-10.0, 68.9, 62.5))   # fill from the water surface
log(r["inventory_delta"])                               # {"water_bucket": 1, "bucket": -1}
await use("water_bucket", look_at=(-13.0, 68.9, 62.0))  # pour at the lava pool edge
await use("bucket", look_at=(-13.0, 68.9, 62.0))        # scoop the source back (clears flow)

# Mine the obsidian (diamond pickaxe; ~7-10s each). breakBlockAt self-navigates.
await equip("diamond_pickaxe")
for x in range(-15, -12):
    for z in range(62, 65):
        await breakBlockAt(x, 68, z)
await collectItems(10)
```

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
