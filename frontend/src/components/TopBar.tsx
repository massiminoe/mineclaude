interface Props {
  connected: boolean;
}

export function TopBar({ connected }: Props) {
  return (
    <div className="top-bar">
      <span className="top-bar-title">mineclaude</span>
      <div className="top-bar-status">
        <span className={`connection-pill ${connected ? "connected" : "disconnected"}`}>
          <span className="connection-dot" />
          {connected ? "connected" : "disconnected"}
        </span>
      </div>
    </div>
  );
}
