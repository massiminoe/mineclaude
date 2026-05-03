interface Props {
  connected: boolean;
  view: "monitor" | "sessions-list" | "session-detail";
  onNavigate: (path: string) => void;
}

export function TopBar({ connected, view, onNavigate }: Props) {
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
        <span className={`connection-pill ${connected ? "connected" : "disconnected"}`}>
          <span className="connection-dot" />
          {connected ? "connected" : "disconnected"}
        </span>
      </div>
    </div>
  );
}
