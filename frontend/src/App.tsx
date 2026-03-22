import { useSocket } from "./hooks/useSocket";
import { ConversationPanel } from "./components/ConversationPanel";
import { ActionQueue } from "./components/ActionQueue";
import { VideoPlaceholder } from "./components/VideoPlaceholder";
import { StatsBar } from "./components/StatsBar";
import "./App.css";

export default function App() {
  const { conversation, queue, gameState, connected } = useSocket();

  return (
    <div className="app">
      <div className="sidebar">
        <ConversationPanel messages={conversation} />
        <ActionQueue queue={queue} />
      </div>
      <div className="main-area">
        <VideoPlaceholder />
      </div>
      <StatsBar gameState={gameState} connected={connected} />
    </div>
  );
}
