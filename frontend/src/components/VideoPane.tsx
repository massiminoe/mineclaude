import { useState, useRef, useEffect } from "react";
import type { ConversationMessage, GameState } from "../types";
import { ChatOverlay } from "./ChatOverlay";

const BRIDGE_URL = "http://localhost:8080";
const STREAM_URL = `${BRIDGE_URL}/video/stream?fps=10&quality=50`;

interface Props {
  gameState: GameState | null;
  conversation: ConversationMessage[];
}

export function VideoPane({ gameState, conversation }: Props) {
  const [streamStatus, setStreamStatus] = useState<
    "loading" | "connected" | "error"
  >("loading");
  const imgRef = useRef<HTMLImageElement>(null);

  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;

    const onLoad = () => setStreamStatus("connected");
    const onError = () => setStreamStatus("error");

    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);

    return () => {
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
    };
  }, []);

  return (
    <div className="video-pane">
      {streamStatus !== "connected" && (
        <div className="video-placeholder">
          <div className="video-placeholder-icon">{"\u25B7"}</div>
          <div className="video-placeholder-label">
            {streamStatus === "loading" ? "connecting..." : "video feed"}
          </div>
        </div>
      )}
      <img
        ref={imgRef}
        className="video-stream"
        src={STREAM_URL}
        alt="Minecraft POV"
        style={{ display: streamStatus === "connected" ? "block" : "none" }}
      />
      <ChatOverlay messages={conversation} />
    </div>
  );
}
