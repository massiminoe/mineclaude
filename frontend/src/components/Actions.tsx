import { useState } from "react";
import type { ActionItem, QueueState, SubActionItem } from "../types";

interface Props {
  queue: QueueState;
  now: number;
}

function fmtDur(seconds: number | null): string {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "";
  if (seconds < 100) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function actionDuration(a: ActionItem, now: number): number | null {
  if (a.started_at == null) return null;
  return (a.finished_at ?? now) - a.started_at;
}

function subDuration(s: SubActionItem, now: number): number | null {
  return (s.finished_at ?? now) - s.started_at;
}

function fmtArgs(args: Record<string, unknown> | null): string {
  if (!args) return "";
  return Object.values(args)
    .map((v) => (typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)))
    .join(" ");
}

function fmtResult(result: unknown): string {
  if (result == null) return "";
  return typeof result === "string" ? result : JSON.stringify(result);
}

const DOT: Record<ActionItem["status"], string> = {
  pending: "◌",
  running: "●",
  completed: "○",
  failed: "✕",
  cancelled: "◌",
};

function SubRow({ sub, now }: { sub: SubActionItem; now: number }) {
  return (
    <div className={`act-sub act-sub-${sub.status}`}>
      <span className="act-sub-nm">
        {sub.name} {fmtArgs(sub.args)}
      </span>
      <span className="act-sub-st">
        {sub.status === "started" ? "···" : fmtDur(subDuration(sub, now))}
      </span>
    </div>
  );
}

function ActionRow({
  action,
  now,
  expanded,
  onToggle,
}: {
  action: ActionItem;
  now: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  const dur = fmtDur(actionDuration(action, now));
  const outcome =
    action.status === "failed" ? action.error : fmtResult(action.result);
  // Collapsed running actions keep their live subaction trace visible —
  // that's the at-a-glance "what is it doing right now" signal — but capped
  // to the tail so a tight primitive loop can't flood the rail.
  const COLLAPSED_SUBS = 3;
  const allSubs = action.subactions;
  const subs = expanded
    ? allSubs
    : action.status === "running"
      ? allSubs.slice(-COLLAPSED_SUBS)
      : [];
  const hiddenSubs = expanded ? 0 : allSubs.length - subs.length;
  return (
    <div className={`act act-${action.status}${expanded ? " act-open" : ""}`}>
      <button className="act-toggle" onClick={onToggle}>
        <div className="act-row">
          <span className="act-dot">{DOT[action.status]}</span>
          <span className="act-id">{action.id.slice(0, 8)}</span>
          <span className="act-st">
            {action.status === "completed" ? "done" : action.status}
          </span>
          <span className="act-dur">{dur}</span>
          <span className="act-disc">{expanded ? "−" : "+"}</span>
        </div>
        {expanded ? (
          <pre className="act-code-full">{action.code}</pre>
        ) : (
          <div className="act-code">{action.code}</div>
        )}
        {outcome && (
          <div className={`act-res${expanded ? " act-res-full" : ""}`}>{outcome}</div>
        )}
      </button>
      {subs.length > 0 && (
        <div className="act-subs">
          {action.status === "running" && hiddenSubs > 0 && (
            <div className="act-sub act-sub-more">… {hiddenSubs} earlier</div>
          )}
          {subs.map((s) => (
            <SubRow key={s.id} sub={s} now={now} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Actions({ queue, now }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const rows: ActionItem[] = [
    ...(queue.running ? [queue.running] : []),
    ...queue.pending,
    ...queue.recent,
  ];
  return (
    <section className="rail-section rail-actions">
      <div className="sec-hd">
        <span className="lbl">Actions</span>
        <span className="lbl-r">single-flight</span>
      </div>
      <div className="rail-scroll">
        {rows.length === 0 && <div className="rail-empty">no actions yet</div>}
        {rows.map((a) => (
          <ActionRow
            key={a.id}
            action={a}
            now={now}
            expanded={expanded.has(a.id)}
            onToggle={() => toggle(a.id)}
          />
        ))}
      </div>
    </section>
  );
}
