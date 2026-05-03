import { useSocket } from "./hooks/useSocket";
import { parseRoute, useHashRoute } from "./hooks/useHashRoute";
import { TopBar } from "./components/TopBar";
import { VideoPane } from "./components/VideoPane";
import { SidePanel } from "./components/SidePanel";
import { SessionList } from "./components/SessionList";
import { SessionDetailView } from "./components/SessionDetail";
import "./App.css";

export default function App() {
  const { conversation, queue, gameState, plan, memory, connected } = useSocket();
  const [hash, navigate] = useHashRoute();
  const route = parseRoute(hash);

  return (
    <div className="app">
      <TopBar connected={connected} view={route.view} onNavigate={navigate} />
      {route.view === "monitor" && (
        <div className="main-content">
          <VideoPane conversation={conversation} connected={connected} />
          <SidePanel queue={queue} gameState={gameState} plan={plan} memory={memory} />
        </div>
      )}
      {route.view === "sessions-list" && (
        <div className="trace-content">
          <SessionList onOpen={(stem) => navigate(`/sessions/${encodeURIComponent(stem)}`)} />
        </div>
      )}
      {route.view === "session-detail" && route.stem && (
        <div className="trace-content">
          <SessionDetailView stem={route.stem} onBack={() => navigate("/sessions")} />
        </div>
      )}
    </div>
  );
}
