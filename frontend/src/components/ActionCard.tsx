import { useState, useEffect } from "react";
import type { ActionItem, SubActionItem } from "../types";

interface Props {
  action: ActionItem;
  position?: number;
}

export function ActionCard({ action, position }: Props) {
  const statusClass = `action-${action.status}`;
  const isRunning = action.status === "running";
  const [expanded, setExpanded] = useState(false);
  const subs = action.subactions || [];
  const showSubs = subs.length > 0 && (isRunning || expanded);

  return (
    <div
      className={`action-card ${statusClass}`}
      onClick={() => setExpanded(!expanded)}
    >
      <div className="action-card-header">
        <div className="action-card-left">
          <StatusIcon status={action.status} />
          <span className="action-id">{action.id}</span>
        </div>
        <span className="action-timing">
          {position != null ? (
            `#${position}`
          ) : isRunning ? (
            <ElapsedTime since={action.started_at!} />
          ) : action.started_at && action.finished_at ? (
            `${(action.finished_at - action.started_at).toFixed(1)}s`
          ) : null}
        </span>
      </div>
      {showSubs && (
        <div className="subaction-list">
          {subs.map((sub) => (
            <SubActionRow key={sub.id} sub={sub} />
          ))}
        </div>
      )}
      {!showSubs && (
        <pre className={`action-code ${expanded ? "" : "truncated"}`}>
          {action.code}
        </pre>
      )}
      {expanded && action.result && (
        <div className="action-result success">{action.result}</div>
      )}
      {expanded && action.error && (
        <div className="action-result error">{action.error}</div>
      )}
      {!expanded && action.error && (
        <div className="action-result error truncated">{action.error}</div>
      )}
    </div>
  );
}

function SubActionRow({ sub }: { sub: SubActionItem }) {
  const argsStr = sub.args
    ? Object.values(sub.args)
        .map((v) => (typeof v === "string" ? `'${v}'` : String(v)))
        .join(", ")
    : "";

  return (
    <div className={`subaction-row subaction-${sub.status}`}>
      <SubStatusIcon status={sub.status} />
      <span className="subaction-call">
        {sub.name}({argsStr})
      </span>
      <span className="subaction-timing">
        {sub.status === "started" ? (
          <ElapsedTime since={sub.started_at} />
        ) : sub.started_at && sub.finished_at ? (
          `${(sub.finished_at - sub.started_at).toFixed(1)}s`
        ) : null}
      </span>
    </div>
  );
}

function SubStatusIcon({ status }: { status: string }) {
  switch (status) {
    case "started":
      return <span className="status-icon sub-running running-pulse" />;
    case "completed":
      return (
        <span className="status-icon sub-completed">{"\u2714"}</span>
      );
    case "failed":
      return <span className="status-icon sub-failed">{"\u2718"}</span>;
    default:
      return null;
  }
}

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case "running":
      return <span className="status-icon running-pulse" />;
    case "completed":
      return <span className="status-icon completed">{"\u2714"}</span>;
    case "failed":
      return <span className="status-icon failed">{"\u2718"}</span>;
    case "cancelled":
      return <span className="status-icon cancelled">{"\u2014"}</span>;
    default:
      return <span className="status-icon pending">{"\u25CB"}</span>;
  }
}

function ElapsedTime({ since }: { since: number }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setElapsed(Math.floor(Date.now() / 1000 - since));
    }, 1000);
    return () => clearInterval(interval);
  }, [since]);

  return <>{elapsed}s</>;
}
