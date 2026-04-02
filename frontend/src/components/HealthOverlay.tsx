interface Props {
  health: number;
  hunger: number;
}

export function HealthOverlay({ health, hunger }: Props) {
  return (
    <div className="health-overlay">
      <div className="health-bar-row">
        <div className="health-bar-track">
          <div
            className="health-bar-fill hp"
            style={{ width: `${(health / 20) * 100}%` }}
          />
        </div>
        <span className="health-bar-label">{health}/20</span>
      </div>
      <div className="health-bar-row">
        <div className="health-bar-track">
          <div
            className="health-bar-fill hunger"
            style={{ width: `${(hunger / 20) * 100}%` }}
          />
        </div>
        <span className="health-bar-label">{hunger}/20</span>
      </div>
    </div>
  );
}
