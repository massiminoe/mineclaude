// Pure parser for the agent's memory.md document.
//
// Memory is freeform Markdown, but the agent is prompted to follow a shape:
//
//   # Memory
//
//   ## Locations
//   - home_base | overworld | 120, 70, -40 | crafting table + bed
//   - diamond_cave | overworld | 50, -56, -6 | found 4 diamonds
//
//   ## Notes
//   - Sleep at night — bed is in home_base
//   - Lava lake near (200, -50, 200) — avoid when mining
//
// We extract the location entries and notes. If neither section parses,
// callers should render `raw` as a preformatted fallback.

export interface MemoryLocation {
  name: string;
  dimension: string;
  x: number;
  y: number;
  z: number;
  notes: string;
}

export interface MemoryView {
  locations: MemoryLocation[];
  notes: string[];
  raw: string;
}

const BULLET_RE = /^\s*[-*]\s*(.+?)\s*$/;

export function parseMemory(raw: string): MemoryView {
  const trimmed = raw.trim();
  if (!trimmed) return { locations: [], notes: [], raw: "" };

  const locations: MemoryLocation[] = [];
  const notes: string[] = [];
  let current: "locations" | "notes" | null = null;

  for (const line of trimmed.split(/\r?\n/)) {
    const headingMatch = line.match(/^##\s+(.+?)\s*$/);
    if (headingMatch) {
      const h = headingMatch[1].toLowerCase();
      if (h.includes("location")) current = "locations";
      else if (h.includes("note")) current = "notes";
      else current = null;
      continue;
    }
    const bullet = line.match(BULLET_RE);
    if (!bullet || !current) continue;
    const text = bullet[1].trim();
    if (current === "locations") {
      const loc = parseLocation(text);
      if (loc) locations.push(loc);
    } else if (current === "notes") {
      notes.push(text);
    }
  }

  return { locations, notes, raw: trimmed };
}

// Parse `name | dimension | x, y, z | notes`. Tolerant of:
//   - missing trailing notes column
//   - missing dimension (defaults to overworld)
//   - whitespace anywhere
//   - coords separated by commas or spaces
function parseLocation(line: string): MemoryLocation | null {
  const parts = line.split("|").map((p) => p.trim());
  if (parts.length < 2) return null;

  const name = parts[0];
  if (!name) return null;

  // Find the part that looks like coordinates (contains a comma or three numbers).
  let dimension = "overworld";
  let coordsPart: string | null = null;
  let notes = "";

  if (parts.length === 2) {
    coordsPart = parts[1];
  } else if (parts.length === 3) {
    if (looksLikeCoords(parts[1])) {
      coordsPart = parts[1];
      notes = parts[2];
    } else {
      dimension = parts[1] || "overworld";
      coordsPart = parts[2];
    }
  } else {
    dimension = parts[1] || "overworld";
    coordsPart = parts[2];
    notes = parts.slice(3).join(" | ");
  }

  if (!coordsPart) return null;
  const coords = parseCoords(coordsPart);
  if (!coords) return null;

  return { name, dimension: dimension.toLowerCase(), ...coords, notes };
}

function looksLikeCoords(s: string): boolean {
  return /-?\d+\s*[,\s]\s*-?\d+\s*[,\s]\s*-?\d+/.test(s);
}

function parseCoords(s: string): { x: number; y: number; z: number } | null {
  const nums = s.match(/-?\d+/g);
  if (!nums || nums.length < 3) return null;
  const [x, y, z] = nums.slice(0, 3).map((n) => parseInt(n, 10));
  if (Number.isNaN(x) || Number.isNaN(y) || Number.isNaN(z)) return null;
  return { x, y, z };
}
