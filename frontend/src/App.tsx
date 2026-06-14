import { useEffect, useState } from "react";
import { useSocket } from "./hooks/useSocket";
import { Feed } from "./components/Feed";
import { Actions } from "./components/Actions";
import { Reflexes } from "./components/Reflexes";
import { InventoryModal } from "./components/InventoryModal";
import { ItemIcon, DurabilityBar } from "./components/ItemIcon";
import { useItemIcons } from "./icons";
import { usedMainSlots } from "./types";
import type { GameState, InventoryItem } from "./types";
import "./App.css";

function tickToClock(tick: number): string {
  const dayTick = (tick + 6000) % 24000;
  const hours = Math.floor(dayTick / 1000);
  const minutes = Math.floor(((dayTick % 1000) / 1000) * 60);
  return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}`;
}

function Meter({ value, max }: { value: number; max: number }) {
  const segments = 10;
  const filled = Math.round((value / max) * segments);
  return (
    <div className="meter">
      {Array.from({ length: segments }, (_, i) => (
        <i key={i} className={i < filled ? "" : "off"} />
      ))}
    </div>
  );
}

function Header({ game, connected }: { game: GameState | null; connected: boolean }) {
  const day = game ? Math.floor(game.time / 24000) + 1 : null;
  return (
    <header>
      <div className="wordmark">
        mineclaude<span> / monitor</span>
      </div>
      <div className="hgroup">
        <div className="field">
          <span className="lbl">Mission time</span>
          <span className="val">{game ? `D${day} ${tickToClock(game.time)}` : "—"}</span>
        </div>
        <div className="field">
          <span className="lbl">Biome</span>
          <span className="val">{game?.biome ?? "—"}</span>
        </div>
      </div>
      <div className={`live${connected ? "" : " offline"}`}>
        <i />
        {connected ? "LIVE" : "OFFLINE"}
      </div>
    </header>
  );
}

function Hotbar({ game, onExpand }: { game: GameState | null; onExpand: () => void }) {
  const lookup = useItemIcons();
  const inv = game?.inventory ?? [];
  const bySlot = new Map<number, InventoryItem>();
  for (const it of inv) bySlot.set(it.slot, it);
  const held = game?.held_slot ?? -1;
  return (
    <button className="hotbar" onClick={onExpand} title="Open inventory" disabled={!game}>
      <div className="hotbar-cells">
        {Array.from({ length: 9 }, (_, k) => {
          const item = bySlot.get(k);
          return (
            <span
              key={k}
              className={`hbc${k === held ? " held" : ""}${item ? "" : " empty"}`}
              title={item ? `${item.name}${item.count > 1 ? ` ×${item.count}` : ""}` : undefined}
            >
              {item && <ItemIcon name={item.name} size={44} lookup={lookup} />}
              {item && item.count > 1 && <span className="ct">{item.count}</span>}
              {item?.durability && <DurabilityBar {...item.durability} />}
            </span>
          );
        })}
      </div>
      <span className="hotbar-meta">
        <span className="lbl">Inventory</span>
        <span className="n">{usedMainSlots(inv)}/36 ⤢</span>
      </span>
    </button>
  );
}

function Footer({ game, onExpand }: { game: GameState | null; onExpand: () => void }) {
  return (
    <footer>
      <div className="fcell">
        <span className="lbl">Position</span>
        <div className="coords">
          {(["x", "y", "z"] as const).map((axis) => (
            <span key={axis}>
              <i>{axis}</i>
              {game ? Math.floor(game.position[axis]) : "—"}
            </span>
          ))}
        </div>
      </div>
      <div className="fcell">
        <span className="lbl">Vitals</span>
        <div className="vitals">
          <div className="field">
            <span className="val">
              {game ? game.health.toFixed(1) : "—"}
              <span> /20 hp</span>
            </span>
            <Meter value={game?.health ?? 0} max={20} />
          </div>
          <div className="field">
            <span className="val">
              {game ? game.hunger : "—"}
              <span> /20 food</span>
            </span>
            <Meter value={game?.hunger ?? 0} max={20} />
          </div>
        </div>
      </div>
      <Hotbar game={game} onExpand={onExpand} />
    </footer>
  );
}

export default function App() {
  const { queue, gameState, reflexes, videoUrl, connected } = useSocket();
  const [invOpen, setInvOpen] = useState(false);

  // Shared 1 Hz clock for running-action elapsed time and reflex ages.
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="app">
      <Header game={gameState} connected={connected} />
      <main>
        <Feed videoUrl={videoUrl} connected={connected} />
        <aside>
          <Actions queue={queue} now={now} />
          <Reflexes reflexes={reflexes} now={now} />
        </aside>
      </main>
      <Footer game={gameState} onExpand={() => setInvOpen(true)} />
      {invOpen && gameState && (
        <InventoryModal game={gameState} onClose={() => setInvOpen(false)} />
      )}
    </div>
  );
}
