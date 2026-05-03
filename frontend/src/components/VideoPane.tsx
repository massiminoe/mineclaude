import { useState, useRef, useEffect } from "react";
import type { ConversationMessage } from "../types";
import { ChatOverlay } from "./ChatOverlay";

const BRIDGE_URL = "http://localhost:8081";
const STREAM_URL = `${BRIDGE_URL}/video/stream?fps=10&quality=50`;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 10000;

interface Props {
  conversation: ConversationMessage[];
  connected: boolean;
}

export function VideoPane({ conversation, connected }: Props) {
  const [streamStatus, setStreamStatus] = useState<
    "loading" | "connected" | "error"
  >("loading");
  const [streamSrc, setStreamSrc] = useState(STREAM_URL);
  const [chatOpen, setChatOpen] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);
  const retryTimer = useRef<number>(0);
  const retryDelay = useRef(RETRY_BASE_MS);
  const wasConnected = useRef(connected);

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

  // The MJPEG stream lives on a separate channel from the monitor WS, so an
  // <img> hooked to a multipart response will silently freeze on its last
  // frame when the underlying TCP connection dies — the browser only fires
  // `error` on the *initial* fetch failure, not mid-stream. Force a reload
  // whenever the monitor WS recovers from a disconnect.
  useEffect(() => {
    if (connected && !wasConnected.current) {
      if (retryTimer.current) {
        clearTimeout(retryTimer.current);
        retryTimer.current = 0;
      }
      retryDelay.current = RETRY_BASE_MS;
      setStreamStatus("loading");
      setStreamSrc(`${STREAM_URL}&_=${Date.now()}`);
    }
    wasConnected.current = connected;
  }, [connected]);

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
