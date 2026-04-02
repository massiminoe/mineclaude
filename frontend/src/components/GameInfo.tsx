interface Props {
  position: { x: number; y: number; z: number };
  biome: string;
  time: number;
}

function tickToTime(tick: number): string {
  const hours = Math.floor(((tick + 6000) % 24000) / 1000);
  const minutes = Math.floor((((tick + 6000) % 24000) % 1000) / 1000 * 60);
  const h = hours % 12 || 12;
  const ampm = hours < 12 ? "AM" : "PM";
  return `${h}:${minutes.toString().padStart(2, "0")} ${ampm}`;
}

export function GameInfo({ position, biome, time }: Props) {
  const day = Math.floor(time / 24000) + 1;

  return (
    <div>
      <div className="game-info-coords">
        {Math.floor(position.x)}, {Math.floor(position.y)}, {Math.floor(position.z)}
      </div>
      <div className="game-info-secondary">
        {biome} &middot; day {day} &middot; {tickToTime(time)}
      </div>
    </div>
  );
}
