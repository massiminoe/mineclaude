import { Group, Panel, Separator } from "react-resizable-panels";
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
      <Group direction="horizontal">
        <Panel defaultSize={33} minSize={15}>
          <ConversationPanel messages={conversation} />
        </Panel>
        <Separator className="resize-handle" />
        <Panel defaultSize={33} minSize={15}>
          <ActionQueue queue={queue} />
        </Panel>
        <Separator className="resize-handle" />
        <Panel defaultSize={34} minSize={15}>
          <div className="main-area">
            <VideoPlaceholder />
          </div>
        </Panel>
      </Group>
      <StatsBar gameState={gameState} connected={connected} />
    </div>
  );
}
