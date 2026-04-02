import type { QueueState, GameState } from "../types";
import { ActionQueue } from "./ActionQueue";
import { GameInfo } from "./GameInfo";
import { InventoryList } from "./InventoryList";

interface Props {
  queue: QueueState;
  gameState: GameState | null;
}

export function SidePanel({ queue, gameState }: Props) {
  return (
    <div className="side-panel">
      <div className="side-section">
        <div className="side-section-label">Action Queue</div>
        <ActionQueue queue={queue} />
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
            <InventoryList inventory={gameState.inventory} />
          </div>
        </>
      )}
    </div>
  );
}
