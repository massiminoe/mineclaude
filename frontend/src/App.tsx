import { useSocket } from "./hooks/useSocket";
import { TopBar } from "./components/TopBar";
import { VideoPane } from "./components/VideoPane";
import { SidePanel } from "./components/SidePanel";
import "./App.css";

export default function App() {
  const { conversation, queue, gameState, plan, connected } = useSocket();

  return (
    <div className="app">
      <TopBar connected={connected} />
      <div className="main-content">
        <VideoPane conversation={conversation} />
        <SidePanel queue={queue} gameState={gameState} plan={plan} />
      </div>
    </div>
  );
}
