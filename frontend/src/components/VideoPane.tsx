import { useState, useRef, useEffect } from "react";
import type { ConversationMessage } from "../types";
import { ChatOverlay } from "./ChatOverlay";

const BRIDGE_URL = "http://localhost:8080";
const STREAM_URL = `${BRIDGE_URL}/video/stream?fps=10&quality=50`;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 10000;

interface Props {
  conversation: ConversationMessage[];
}

export function VideoPane({ conversation }: Props) {
  const [streamStatus, setStreamStatus] = useState<
    "loading" | "connected" | "error"
  >("loading");
  const [streamSrc, setStreamSrc] = useState(STREAM_URL);
  const [chatOpen, setChatOpen] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const retryTimer = useRef<number>(0);
  const retryDelay = useRef(RETRY_BASE_MS);

  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;

    const onLoad = () => {
      setStreamStatus("connected");
      retryDelay.current = RETRY_BASE_MS;
    };
    const onError = () => {
      setStreamStatus("error");
      if (retryTimer.current) return;
      retryTimer.current = window.setTimeout(() => {
        retryTimer.current = 0;
        setStreamStatus("loading");
        setStreamSrc(`${STREAM_URL}&_=${Date.now()}`);
        retryDelay.current = Math.min(retryDelay.current * 2, RETRY_MAX_MS);
      }, retryDelay.current);
    };

    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);

    return () => {
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
      if (retryTimer.current) {
        clearTimeout(retryTimer.current);
        retryTimer.current = 0;
      }
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
        src={streamSrc}
        alt="Minecraft POV"
        style={{ display: streamStatus === "connected" ? "block" : "none" }}
      />
      {chatOpen ? (
        <ChatOverlay messages={conversation} onClose={() => setChatOpen(false)} />
      ) : (
        <button className="chat-toggle-tab" onClick={() => setChatOpen(true)}>
          {"\u25B2"} Chat
        </button>
      )}
    </div>
  );
}
