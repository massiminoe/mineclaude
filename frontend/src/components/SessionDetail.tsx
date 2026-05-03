import { useEffect, useMemo, useState } from "react";
import type { Iteration, SessionDetail, ToolCall, TraceEvent, Turn } from "../trace";
import { buildTimeline, formatDuration, formatTs } from "../trace";
import { JsonView } from "./JsonView";

interface Props {
  stem: string;
  onBack: () => void;
}

export function SessionDetailView({ stem, onBack }: Props) {
  const [data, setData] = useState<SessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    fetch(`/api/sessions/${encodeURIComponent(stem)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j) => {
        if (!cancelled) setData(j);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [stem]);

  const timeline = useMemo(() => (data ? buildTimeline(data.events) : null), [data]);

  if (error) return <div className="trace-error">Failed to load session: {error}</div>;
  if (!data || !timeline) return <div className="trace-loading">Loading session {stem}…</div>;

  const summary = data.summary;
  const duration =
    summary.started_at && summary.ended_at
      ? formatDuration(summary.ended_at - summary.started_at)
      : "—";

  return (
    <div className="session-detail">
      <div className="session-detail-topbar">
        <button type="button" className="link-button" onClick={onBack}>← All sessions</button>
        <div className="session-detail-title">
          <div className="session-detail-id">{summary.session_id ?? stem}</div>
          <div className="session-detail-meta">
            {formatTs(summary.started_at)} · {duration} · {timeline.turns.length} turns ·{" "}
            {summary.iteration_count} iterations · {summary.tool_call_count} tools
            {summary.screenshot_count > 0 && ` · ${summary.screenshot_count} screenshots`}
            {summary.belief_mismatch_count > 0 && (
              <span className="trace-warn"> · {summary.belief_mismatch_count} belief mismatches</span>
            )}
          </div>
        </div>
      </div>
      <div className="session-detail-body">
        <TurnSidebar turns={timeline.turns} />
        <div className="session-detail-main">
          {timeline.preamble.length > 0 && (
            <div className="turn-card">
              <div className="turn-card-header">Preamble</div>
              {timeline.preamble.map((ev, i) => (
                <SideEventRow key={i} ev={ev} stem={stem} />
              ))}
            </div>
          )}
          {timeline.turns.map((turn, idx) => (
            <TurnView key={idx} turn={turn} index={idx} stem={stem} />
          ))}
          {timeline.turns.length === 0 && timeline.preamble.length === 0 && (
            <div className="trace-empty">Session has no events.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function TurnSidebar({ turns }: { turns: Turn[] }) {
  if (turns.length === 0) return null;
  return (
    <aside className="turn-sidebar">
      <div className="turn-sidebar-label">Turns</div>
      {turns.map((turn, idx) => {
        const userText = (turn.user?.data as { message?: string })?.message ?? "(no message)";
        return (
          <a key={idx} href={`#turn-${idx}`} className="turn-sidebar-row">
            <span className="turn-sidebar-num">{idx + 1}</span>
            <span className="turn-sidebar-text">{userText.slice(0, 60)}</span>
            <span className="turn-sidebar-iter">{turn.iterations.length}i</span>
          </a>
        );
      })}
    </aside>
  );
}

function TurnView({ turn, index, stem }: { turn: Turn; index: number; stem: string }) {
  const userText = (turn.user?.data as { username?: string; message?: string }) ?? {};
  const duration = formatDuration(turn.endTs - turn.startTs);
  return (
    <section id={`turn-${index}`} className="turn-card">
      <div className="turn-card-header">
        <span className="turn-card-num">Turn {index + 1}</span>
        <span className="turn-card-duration">{duration}</span>
      </div>
      {turn.user && (
        <div className="turn-user">
          <span className="turn-user-name">{userText.username ?? "user"}:</span>
          <span className="turn-user-text">{userText.message}</span>
        </div>
      )}
      {turn.iterations.map((iter) => (
        <IterationView key={iter.number} iter={iter} stem={stem} />
      ))}
      {turn.loose.map((ev, i) => (
        <SideEventRow key={`loose-${i}`} ev={ev} stem={stem} />
      ))}
      {turn.agentReplies.length > 0 && (
        <div className="turn-final-replies">
          {turn.agentReplies.map((r, i) => (
            <div key={i} className="turn-agent-reply">
              <span className="turn-agent-name">claude:</span>
              <span className="turn-agent-text">{(r.data as { text?: string }).text}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function IterationView({ iter, stem }: { iter: Iteration; stem: string }) {
  const [open, setOpen] = useState(true);
  const responseBlocks =
    (iter.response?.data?.blocks as Array<{ type: string; text?: string | null; tool_name?: string | null; tool_input?: unknown }>) ??
    [];
  const stopReason = (iter.response?.data as { stop_reason?: string } | undefined)?.stop_reason;

  return (
    <div className="iteration-card">
      <button type="button" className="iteration-header" onClick={() => setOpen((v) => !v)}>
        <span className="iteration-chevron">{open ? "▾" : "▸"}</span>
        <span className="iteration-num">iter {iter.number}</span>
        {stopReason && <span className="iteration-stop">stop: {stopReason}</span>}
        <span className="iteration-tool-count">
          {iter.toolCalls.length} tool{iter.toolCalls.length === 1 ? "" : "s"}
        </span>
      </button>
      {open && (
        <div className="iteration-body">
          {iter.request && <RequestView ev={iter.request} />}
          {responseBlocks.length > 0 && (
            <div className="iteration-section">
              <div className="iteration-section-label">response</div>
              {responseBlocks.map((b, i) => {
                if (b.type === "text" && b.text) {
                  return (
                    <div key={i} className="response-text">
                      {b.text}
                    </div>
                  );
                }
                if (b.type === "tool_use") {
                  return (
                    <div key={i} className="response-tool-use">
                      <span className="response-tool-name">{b.tool_name}</span>
                      <JsonView value={b.tool_input} collapseAt={200} />
                    </div>
                  );
                }
                return null;
              })}
            </div>
          )}
          {iter.toolCalls.length > 0 && (
            <div className="iteration-section">
              <div className="iteration-section-label">tool dispatches</div>
              {iter.toolCalls.map((tc, i) => (
                <ToolCallView key={i} call={tc} stem={stem} />
              ))}
            </div>
          )}
          {iter.sideEvents.length > 0 && (
            <div className="iteration-section">
              <div className="iteration-section-label">events</div>
              {iter.sideEvents.map((ev, i) => (
                <SideEventRow key={i} ev={ev} stem={stem} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RequestView({ ev }: { ev: TraceEvent }) {
  const data = ev.data as {
    iteration?: number;
    message_count?: number;
    game_state?: string;
    status?: unknown;
    queue?: unknown;
  };
  return (
    <div className="iteration-section">
      <div className="iteration-section-label">request</div>
      <div className="request-meta">
        message_count: {data.message_count ?? "—"}
      </div>
      {data.game_state && (
        <JsonView value={data.game_state} label="gameState" collapseAt={400} defaultOpen={false} />
      )}
      {data.status && (
        <JsonView value={data.status} label="status (raw)" collapseAt={400} defaultOpen={false} />
      )}
    </div>
  );
}

function ToolCallView({ call, stem }: { call: ToolCall; stem: string }) {
  const [open, setOpen] = useState(false);
  const failed =
    typeof call.result === "string" &&
    /^\[(error|status=error)/i.test(call.result.trim());
  return (
    <div className={`tool-call ${failed ? "tool-call-failed" : ""}`}>
      <button type="button" className="tool-call-header" onClick={() => setOpen((v) => !v)}>
        <span className="tool-call-chevron">{open ? "▾" : "▸"}</span>
        <span className="tool-call-name">{call.name}</span>
        {call.elapsedMs !== null && (
          <span className="tool-call-elapsed">{call.elapsedMs}ms</span>
        )}
        {failed && <span className="tool-call-badge tool-call-badge-error">error</span>}
        {call.imagePath && <span className="tool-call-badge tool-call-badge-image">image</span>}
      </button>
      {open && (
        <div className="tool-call-body">
          <JsonView value={call.input} label="input" collapseAt={300} />
          {call.imagePath ? (
            <div className="tool-call-image">
              <img
                src={`/api/sessions/${encodeURIComponent(stem)}/images/${encodeURIComponent(
                  call.imagePath.split("/").pop() ?? "",
                )}`}
                alt={`${call.name} screenshot`}
                loading="lazy"
              />
              {typeof (call.result as { text?: string })?.text === "string" && (
                <div className="tool-call-image-caption">
                  {(call.result as { text?: string }).text}
                </div>
              )}
            </div>
          ) : (
            <JsonView value={call.result} label="result" collapseAt={300} />
          )}
        </div>
      )}
    </div>
  );
}

function SideEventRow({ ev, stem }: { ev: TraceEvent; stem: string }) {
  if (ev.event === "belief_mismatch") {
    return <BeliefMismatchRow ev={ev} />;
  }
  if (ev.event === "exception") {
    const d = ev.data as { stage?: string; tool?: string; exc?: string; message?: string };
    return (
      <div className="side-event side-event-error">
        <span className="side-event-label">exception</span>
        <span className="side-event-text">
          {d.stage}{d.tool ? ` (${d.tool})` : ""}: {d.exc} — {d.message}
        </span>
      </div>
    );
  }
  if (ev.event === "subaction") {
    const d = ev.data as { name?: string; status?: string; error?: string | null };
    return (
      <div className="side-event side-event-sub">
        <span className="side-event-label">subaction</span>
        <span className="side-event-text">
          {d.name} · <em>{d.status}</em>
          {d.error ? <span className="trace-error-text"> — {d.error}</span> : null}
        </span>
      </div>
    );
  }
  if (ev.event === "chat_out") {
    return (
      <div className="side-event side-event-chat">
        <span className="side-event-label">chat_out</span>
        <span className="side-event-text">{(ev.data as { text?: string }).text}</span>
      </div>
    );
  }
  // Fallback: render as raw json
  return (
    <div className="side-event">
      <span className="side-event-label">{ev.event}</span>
      <JsonView value={ev.data} collapseAt={200} />
    </div>
  );
}

function BeliefMismatchRow({ ev }: { ev: TraceEvent }) {
  const [open, setOpen] = useState(false);
  const data = ev.data as { mismatches?: unknown[]; belief?: unknown; actual?: unknown };
  const mismatches = (data.mismatches ?? []) as Array<{
    field?: string;
    delta?: unknown;
    belief?: unknown;
    actual?: unknown;
    changes?: Array<{ item: string; belief: number; actual: number }>;
  }>;
  return (
    <div className="side-event side-event-mismatch">
      <button type="button" className="mismatch-toggle" onClick={() => setOpen((v) => !v)}>
        <span className="json-chevron">{open ? "▾" : "▸"}</span>
        <span className="side-event-label">belief mismatch</span>
        <span className="side-event-text">
          {mismatches.map((m, i) => (
            <span key={i} className="mismatch-chip">
              {m.field}
              {typeof m.delta === "number" ? ` Δ${(m.delta as number).toFixed(2)}` : ""}
              {m.changes ? ` (${m.changes.length})` : ""}
            </span>
          ))}
        </span>
      </button>
      {open && (
        <div className="mismatch-body">
          {mismatches.map((m, i) => (
            <div key={i} className="mismatch-detail">
              <div className="mismatch-detail-field">{m.field}</div>
              {m.changes ? (
                <table className="mismatch-table">
                  <tbody>
                    {m.changes.map((c, j) => (
                      <tr key={j}>
                        <td>{c.item}</td>
                        <td className="trace-dim">belief {c.belief}</td>
                        <td className="trace-warn">actual {c.actual}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="mismatch-deltas">
                  <JsonView value={m.belief} label="belief" collapseAt={200} />
                  <JsonView value={m.actual} label="actual" collapseAt={200} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
