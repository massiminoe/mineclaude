import type { ConversationMessage, GameState } from "../types";
import { HealthOverlay } from "./HealthOverlay";
import { ChatOverlay } from "./ChatOverlay";

interface Props {
  gameState: GameState | null;
  conversation: ConversationMessage[];
}

export function VideoPane({ gameState, conversation }: Props) {
  return (
    <div className="video-pane">
      {gameState && (
        <HealthOverlay health={gameState.health} hunger={gameState.hunger} />
      )}
      <div className="video-placeholder">
        <div className="video-placeholder-icon">{"\u25B7"}</div>
        <div className="video-placeholder-label">video feed</div>
      </div>
      <ChatOverlay messages={conversation} />
    </div>
  );
}
