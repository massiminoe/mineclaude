import type { ReflexEvent } from "../types";

interface Props {
  reflexes: ReflexEvent[];
  now: number;
}

// Reflex types that indicate harm already taken, vs. ambient warnings.
const FAULT_TYPES = new Set(["damage_taken", "entered_lava", "started_drowning"]);

function fmtAge(ts: number, now: number): string {
  const age = Math.max(0, now - ts);
  const m = Math.floor(age / 60);
  const s = Math.floor(age % 60);
  return `−${m}:${s.toString().padStart(2, "0")}`;
}

function describe(r: ReflexEvent): string {
  const d = r.data ?? {};
  switch (r.type) {
    case "damage_taken": {
      const parts: string[] = [];
      if (typeof d.amount === "number") parts.push(`−${d.amount} hp`);
      if (typeof d.attacker_kind === "string") parts.push(String(d.attacker_kind));
      else if (typeof d.source === "string") parts.push(String(d.source));
      return parts.join(" · ");
    }
    case "hostile_nearby": {
      const parts: string[] = [];
      if (typeof d.kind === "string") parts.push(String(d.kind));
      if (typeof d.distance === "number") parts.push(`${(d.distance as number).toFixed(1)}m`);
      return parts.join(" ");
    }
    case "tool_broke":
      return typeof d.item === "string" ? String(d.item) : "";
    default: {
      const s = JSON.stringify(d);
      return s === "{}" ? "" : s;
    }
  }
}

export function Reflexes({ reflexes, now }: Props) {
  return (
    <section className="rail-section rail-reflexes">
      <div className="sec-hd">
        <span className="lbl">Reflexes</span>
      </div>
      <div className="rail-scroll">
        {reflexes.length === 0 && <div className="rail-empty">no hazards</div>}
        {reflexes.map((r) => (
          <div
            key={`${r.ts}-${r.type}`}
            className={`rfx${FAULT_TYPES.has(r.type) ? " rfx-fault" : ""}`}
          >
            <span className="rfx-t">{fmtAge(r.ts, now)}</span>
            <span className="rfx-ty">{r.type}</span>
            <span className="rfx-d">{describe(r)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
