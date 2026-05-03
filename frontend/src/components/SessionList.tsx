import { useEffect, useState } from "react";
import type { SessionSummary } from "../trace";
import { formatBytes, formatDuration, formatTs } from "../trace";

interface Props {
  onOpen: (stem: string) => void;
}

export function SessionList({ onOpen }: Props) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/sessions")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j) => {
        if (!cancelled) setSessions(j.sessions ?? []);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <div className="trace-error">Failed to load sessions: {error}</div>;
  }
  if (sessions === null) {
    return <div className="trace-loading">Loading sessions…</div>;
  }
  if (sessions.length === 0) {
    return <div className="trace-empty">No sessions yet. Run the agent and the first chat will start a session.</div>;
  }

  return (
    <div className="session-list">
      <div className="session-list-header">
        <span className="session-list-title">Sessions</span>
        <span className="session-list-count">{sessions.length}</span>
      </div>
      <div className="session-list-rows">
        {sessions.map((s) => {
          const duration =
            s.started_at && s.ended_at ? formatDuration(s.ended_at - s.started_at) : "—";
          return (
            <button
              key={s.stem}
              className="session-row"
              onClick={() => onOpen(s.stem)}
              type="button"
            >
              <div className="session-row-line1">
                <span className="session-row-time">{formatTs(s.started_at)}</span>
                <span className="session-row-id">{s.session_id?.slice(0, 8) ?? s.stem}</span>
                <span className="session-row-duration">{duration}</span>
              </div>
              <div className="session-row-prompt">
                {s.first_user_message ?? <span className="trace-dim">(no user message)</span>}
              </div>
              <div className="session-row-stats">
                <span>{s.turn_count} turn{s.turn_count === 1 ? "" : "s"}</span>
                <span>{s.iteration_count} iter</span>
                <span>{s.tool_call_count} tools</span>
                {s.screenshot_count > 0 && <span>{s.screenshot_count} img</span>}
                {s.belief_mismatch_count > 0 && (
                  <span className="trace-warn">{s.belief_mismatch_count} mismatch</span>
                )}
                {s.exception_count > 0 && (
                  <span className="trace-error-text">{s.exception_count} err</span>
                )}
                <span className="session-row-size">{formatBytes(s.size)}</span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
