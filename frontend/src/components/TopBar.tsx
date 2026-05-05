import type { UsageTotals } from "../types";

interface Props {
  connected: boolean;
  view: "monitor" | "sessions-list" | "session-detail";
  onNavigate: (path: string) => void;
  usage: UsageTotals | null;
}

export function TopBar({ connected, view, onNavigate, usage }: Props) {
  const onSessions = view === "sessions-list" || view === "session-detail";
  return (
    <div className="top-bar">
      <div className="top-bar-left">
        <span className="top-bar-title">mineclaude</span>
        <nav className="top-bar-nav">
          <button
            type="button"
            className={`top-bar-link ${view === "monitor" ? "active" : ""}`}
            onClick={() => onNavigate("/")}
          >
            Monitor
          </button>
          <button
            type="button"
            className={`top-bar-link ${onSessions ? "active" : ""}`}
            onClick={() => onNavigate("/sessions")}
          >
            Sessions
          </button>
        </nav>
      </div>
      <div className="top-bar-status">
        <UsagePill usage={usage} />
        <span className={`connection-pill ${connected ? "connected" : "disconnected"}`}>
          <span className="connection-dot" />
          {connected ? "connected" : "disconnected"}
        </span>
      </div>
    </div>
  );
}

function UsagePill({ usage }: { usage: UsageTotals | null }) {
  if (!usage || usage.calls === 0) return null;
  const cacheTotal =
    usage.input_tokens +
    usage.cache_creation_input_tokens +
    usage.cache_read_input_tokens;
  const cacheHitPct =
    cacheTotal > 0
      ? Math.round((usage.cache_read_input_tokens / cacheTotal) * 100)
      : 0;
  const title = [
    `${usage.calls} API calls`,
    `input ${formatTokens(usage.input_tokens)}`,
    `output ${formatTokens(usage.output_tokens)}`,
    `cache write ${formatTokens(usage.cache_creation_input_tokens)}`,
    `cache read ${formatTokens(usage.cache_read_input_tokens)}`,
    `cost ${formatCost(usage.cost_usd)}`,
  ].join(" · ");
  return (
    <span className="usage-pill" title={title}>
      <span className="usage-pill-cost">{formatCost(usage.cost_usd)}</span>
      <span className="usage-pill-sep">·</span>
      <span className="usage-pill-tokens">
        {formatTokens(usage.output_tokens)} out
      </span>
      <span className="usage-pill-sep">·</span>
      <span className="usage-pill-cache">{cacheHitPct}% cached</span>
    </span>
  );
}

export function formatCost(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(4)}`;
}

export function formatTokens(n: number): string {
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}
