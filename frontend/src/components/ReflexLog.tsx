import { useEffect, useState } from "react";
import type { ReflexEvent } from "../types";

interface Props {
  reflexes: ReflexEvent[];
}

// Maps event type → CSS class for the colored left border.
const TYPE_TONE: Record<string, string> = {
  damage_taken: "danger",
  entered_lava: "lava",
  started_drowning: "water",
  tool_broke: "tool",
};

export function ReflexLog({ reflexes }: Props) {
  // Re-render every second so the relative timestamps stay accurate without
  // each event needing its own timer.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (reflexes.length === 0) {
    return <div className="reflex-empty">No reflex events yet.</div>;
  }

  const now = Date.now() / 1000;
  return (
    <div className="reflex-log">
      {reflexes.map((evt, i) => (
        <div key={`${evt.ts}-${i}`} className={`reflex-row ${TYPE_TONE[evt.type] ?? "muted"}`}>
          <span className="reflex-label">
            <span className="reflex-type">{evt.type}</span>
            {formatHint(evt) && (
              <span className="reflex-hint">{formatHint(evt)}</span>
            )}
          </span>
          <span className="reflex-ago">{formatAgo(now - evt.ts)}</span>
        </div>
      ))}
    </div>
  );
}

function formatHint(evt: ReflexEvent): string {
  const data = evt.data || {};
  if (evt.type === "damage_taken") {
    const amount = data.amount as number | undefined;
    const attacker = data.attacker_kind as string | undefined;
    const source = data.source as string | undefined;
    const bits: string[] = [];
    if (typeof amount === "number") bits.push(`${amount.toFixed(1)} dmg`);
    if (attacker) bits.push(`from ${attacker}`);
    else if (source) bits.push(`from ${source}`);
    return bits.join(", ");
  }
  if (evt.type === "tool_broke") {
    const item = data.item as string | undefined;
    return item ?? "";
  }
  return "";
}

function formatAgo(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}
