import { useMemo } from "react";
import type { TimelineEvent } from "../types";

interface Props {
  /** Hazard reflex fires (damage_taken, hostile_nearby, …). */
  reflexes: TimelineEvent[];
  /** Curated world events (chat, death, respawn, advancement). */
  events: TimelineEvent[];
  now: number;
}

// Visual category for the left accent spine + colouring. `fault` = harm taken
// (red), `hazard` = ambient warning (amber), plus the three world-event accents.
type Category = "fault" | "hazard" | "chat" | "grant" | "life";

const CATEGORY: Record<string, Category> = {
  damage_taken: "fault",
  entered_lava: "fault",
  started_drowning: "fault",
  death: "fault",
  hostile_nearby: "hazard",
  tool_broke: "hazard",
  chat: "chat",
  advancement: "grant",
  respawn: "life",
};

// Short uppercase tag shown in the gutter, per type.
const LABEL: Record<string, string> = {
  damage_taken: "damage",
  entered_lava: "lava",
  started_drowning: "drowning",
  hostile_nearby: "hostile",
  tool_broke: "tool broke",
  death: "death",
  respawn: "respawn",
  chat: "chat",
  advancement: "advancement",
};

function fmtAge(ts: number, now: number): string {
  const age = Math.max(0, now - ts);
  const m = Math.floor(age / 60);
  const s = Math.floor(age % 60);
  return `−${m}:${s.toString().padStart(2, "0")}`;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}
function num(v: unknown): number | null {
  return typeof v === "number" ? v : null;
}

function describe(e: TimelineEvent): string {
  const d = e.data ?? {};
  switch (e.type) {
    case "damage_taken": {
      const parts: string[] = [];
      const amt = num(d.amount);
      if (amt !== null) parts.push(`−${amt} hp`);
      const who = str(d.attacker_kind) ?? str(d.source);
      if (who) parts.push(who);
      return parts.join(" · ");
    }
    case "hostile_nearby": {
      const parts: string[] = [];
      const kind = str(d.kind);
      if (kind) parts.push(kind);
      const dist = num(d.distance);
      if (dist !== null) parts.push(`${dist.toFixed(1)}m`);
      return parts.join(" ");
    }
    case "tool_broke":
      return str(d.item) ?? "";
    case "death":
      return str(d.cause) ? `slain by ${str(d.cause)}` : "died";
    case "respawn":
      return "spawn point";
    case "advancement":
      return str(d.title) ?? str(d.id) ?? "";
    case "chat":
      // Speaker rendered separately (coloured); body is the message.
      return str(d.message) ?? "";
    default: {
      const s = JSON.stringify(d);
      return s === "{}" ? "" : s;
    }
  }
}

export function Events({ reflexes, events, now }: Props) {
  // Merge the two streams into one timeline, newest first. Dedupe on ts+type
  // in case a reflex ever doubles as a recorded event.
  const merged = useMemo(() => {
    const seen = new Set<string>();
    const all: TimelineEvent[] = [];
    for (const e of [...reflexes, ...events]) {
      const key = `${e.ts}-${e.type}`;
      if (seen.has(key)) continue;
      seen.add(key);
      all.push(e);
    }
    all.sort((a, b) => b.ts - a.ts);
    return all;
  }, [reflexes, events]);

  return (
    <section className="rail-section rail-events">
      <div className="sec-hd">
        <span className="lbl">Events</span>
        {merged.length > 0 && <span className="sec-count">{merged.length}</span>}
      </div>
      <div className="rail-scroll">
        {merged.length === 0 && <div className="rail-empty">no events</div>}
        {merged.map((e) => {
          const cat = CATEGORY[e.type] ?? "hazard";
          const who = e.type === "chat" ? str(e.data?.username) : null;
          return (
            <div key={`${e.ts}-${e.type}`} className={`evt evt-${cat}`}>
              <span className="evt-t">{fmtAge(e.ts, now)}</span>
              <span className="evt-ty">{LABEL[e.type] ?? e.type}</span>
              <span className="evt-d">
                {who && <span className="evt-who">{who}</span>}
                {describe(e)}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
