#!/usr/bin/env node
// Generate frontend/public/itemIcons.json — a { itemName: dataURL } map of
// Minecraft 1.21.5 item/block textures, so the monitor can render real icons
// instead of bare text.
//
// Why generate-and-commit (mirrors scripts/gen_skill_docs.py): the textures
// come from the `minecraft-assets` dev dependency; baking them into one JSON at
// build time means the runtime has zero icon deps and works offline. Re-run
// after a MC version bump.
//
//   node scripts/gen_item_icons.mjs
//
// The output lives in frontend/public/ (not src/) so Vite serves it as a
// standalone cacheable asset, fetched once at runtime — it never bloats the JS
// bundle.
//
// Texture notes: these are flat 16x16 face textures, NOT the isometric 3D block
// renders vanilla shows in-inventory. Multi-face blocks (furnace, crafting
// table, chest) fall back to a representative face via the resolver below. The
// frontend scales them with image-rendering:pixelated so they stay crisp.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const VERSION = "1.21.5";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const frontendDir = path.join(repoRoot, "frontend");
const outPath = path.join(frontendDir, "public", "itemIcons.json");

// minecraft-assets and pngjs are devDependencies of the frontend, so resolve
// them from there.
const require = createRequire(path.join(frontendDir, "package.json"));
const mcAssets = require("minecraft-assets");
const { PNG } = require("pngjs");

const assets = mcAssets(VERSION);
if (!assets) {
  console.error(`minecraft-assets has no data for ${VERSION}`);
  process.exit(1);
}
const dir = assets.directory; // .../data/<version>/

// Resolve an item/block name to a texture file path (relative to `dir`).
// Priority: the asset entry's own texture → exact-name block/item face →
// getTexture()'s model-derived fallback (covers multi-face blocks).
function resolveTexture(name, entry) {
  const candidates = [];
  if (entry?.texture && entry.texture !== "minecraft:missingno") {
    candidates.push(entry.texture.replace(/^minecraft:/, ""));
  }
  // Prefer the plainly-named face (e.g. blocks/oak_log side, not oak_log_top).
  candidates.push(`blocks/${name}`, `items/${name}`);
  try {
    const t = assets.getTexture(name);
    if (t) {
      candidates.push(
        String(t)
          .replace(/^minecraft:/, "")
          .replace(/^block\//, "blocks/")
          .replace(/^item\//, "items/"),
      );
    }
  } catch {
    /* getTexture throws for unknown names — ignore */
  }
  for (const c of candidates) {
    const p = path.join(dir, `${c}.png`);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

// Beds have no flat item/block face texture — the item entry's `texture`
// resolves to the base color's plain wool square (e.g. white_bed → a blank
// white tile that vanishes against the monitor's dark UI), and the block
// entry resolves to bare oak planks. Neither reads as "a bed". The real
// texture lives in the entity atlas (entity/bed/<color>.png, used to render
// the in-world 3D model) — its top-left 24x24 corner happens to contain the
// head-of-bed view (headboard + pillow + quilt), which crops cleanly into a
// recognizable, correctly-colored icon.
const BED_CROP = 24;
function bedIcon(name) {
  const m = /^(\w+)_bed$/.exec(name);
  if (!m) return null;
  const p = path.join(dir, "entity", "bed", `${m[1]}.png`);
  if (!fs.existsSync(p)) return null;
  const src = PNG.sync.read(fs.readFileSync(p));
  const out = new PNG({ width: BED_CROP, height: BED_CROP });
  PNG.bitblt(src, out, 0, 0, BED_CROP, BED_CROP, 0, 0);
  return PNG.sync.write(out);
}

// Build a name → entry map (items take precedence, blocks fill gaps).
const byName = {};
for (const e of assets.itemsArray) byName[e.name] = e;
for (const e of assets.blocksArray) if (!byName[e.name]) byName[e.name] = e;

const icons = {};
let hit = 0;
const misses = [];
for (const name of Object.keys(byName).sort()) {
  const bed = bedIcon(name);
  if (bed) {
    icons[name] = `data:image/png;base64,${bed.toString("base64")}`;
    hit++;
    continue;
  }
  const file = resolveTexture(name, byName[name]);
  if (!file) {
    misses.push(name);
    continue;
  }
  const b64 = fs.readFileSync(file).toString("base64");
  icons[name] = `data:image/png;base64,${b64}`;
  hit++;
}

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, JSON.stringify(icons));

const bytes = fs.statSync(outPath).size;
console.log(`Wrote ${outPath}`);
console.log(`  ${hit} icons (${(bytes / 1024).toFixed(0)} KB), ${misses.length} skipped`);
if (misses.length) console.log(`  skipped: ${misses.join(", ")}`);
