import type { QueueState, GameState } from "../types";
import { ActionQueue } from "./ActionQueue";
import { ConsolePanel } from "./ConsolePanel";
import { GameInfo } from "./GameInfo";
import { InventoryList } from "./InventoryList";
import { PlanCard } from "./PlanCard";

interface Props {
  queue: QueueState;
  gameState: GameState | null;
  plan: string;
}

export function SidePanel({ queue, gameState, plan }: Props) {
  return (
    <div className="side-panel">
      <div className="side-section">
        <div className="side-section-label">Plan</div>
        <PlanCard plan={plan} />
      </div>
      <div className="side-section">
        <div className="side-section-label">Action Queue</div>
        <ActionQueue queue={queue} />
      </div>
      <div className="side-section">
        <ConsolePanel />
      </div>
      {gameState && (
        <>
          <div className="side-section">
            <div className="side-section-label">Position</div>
            <GameInfo
              position={gameState.position}
              biome={gameState.biome}
              time={gameState.time}
            />
          </div>
          <div className="side-section">
            <div className="side-section-label">Inventory</div>
            <InventoryList inventory={gameState.inventory || []} />
          </div>
        </>
      )}
    </div>
  );
}
