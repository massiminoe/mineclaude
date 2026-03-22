import type { GameState } from "../types";

interface Props {
  gameState: GameState | null;
  connected: boolean;
}

function tickToTime(tick: number): string {
  // MC day starts at 6:00 AM (tick 0)
  const hours = Math.floor(((tick + 6000) % 24000) / 1000);
  const minutes = Math.floor((((tick + 6000) % 24000) % 1000) / 1000 * 60);
  const h = hours % 12 || 12;
  const ampm = hours < 12 ? "AM" : "PM";
  return `${h}:${minutes.toString().padStart(2, "0")} ${ampm}`;
}

function isDaytime(tick: number): boolean {
  return tick >= 0 && tick < 12000;
}

function healthColor(value: number): string {
  if (value > 10) return "var(--color-good)";
  if (value > 5) return "var(--color-warn)";
  return "var(--color-error)";
}

export function StatsBar({ gameState, connected }: Props) {
  if (!gameState) {
    return (
      <div className="stats-bar">
        <div className="stat">
          <span
            className={`connection-dot ${connected ? "connected" : "disconnected"}`}
          />
          <span className="stat-value">
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>
    );
  }

  const { position, health, hunger, time } = gameState;
  const day = Math.floor(time / 24000) + 1;

  return (
    <div className="stats-bar">
      <div className="stat">
        <span className="stat-icon" style={{ color: "var(--color-error)" }}>
          &#9829;
        </span>
        <span className="stat-value" style={{ color: healthColor(health) }}>
          {health}
        </span>
        <span className="stat-max">/20</span>
      </div>
      <div className="stat">
        <span className="stat-icon" style={{ color: "var(--color-warn)" }}>
          &#9733;
        </span>
        <span
          className="stat-value"
          style={{ color: healthColor(hunger) }}
        >
          {hunger}
        </span>
        <span className="stat-max">/20</span>
      </div>
      <div className="stat">
        <span className="stat-value stat-position">
          {Math.floor(position.x)}, {Math.floor(position.y)},{" "}
          {Math.floor(position.z)}
        </span>
      </div>
      <div className="stat">
        <span className="stat-icon">
          {isDaytime(time) ? "\u2600" : "\u263D"}
        </span>
        <span className="stat-value">
          Day {day} — {tickToTime(time)}
        </span>
      </div>
      <div className="stat stat-connection">
        <span
          className={`connection-dot ${connected ? "connected" : "disconnected"}`}
        />
        <span className="stat-value">
          {connected ? "Connected" : "Disconnected"}
        </span>
      </div>
    </div>
  );
}
