import { useState, useRef, useEffect } from "react";

// Same-origin path proxied by the monitor (see monitor.py `_handle_video`).
// Must stay relative so the feed loads on whatever host is serving this page
// (Tailscale, LAN, tunnel) — an absolute localhost URL resolves to the
// *viewer's* machine, not the bot host. The monitor also sends this exact
// path as `video_url`; the fallback covers the pre-first-fetch render.
const FALLBACK_STREAM_URL = "/video/stream?fps=10&quality=50";
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 10000;

interface Props {
  videoUrl: string | null;
  connected: boolean;
}

export function Feed({ videoUrl, connected }: Props) {
  const streamUrl = videoUrl ?? FALLBACK_STREAM_URL;
  const [streamStatus, setStreamStatus] = useState<
    "loading" | "connected" | "error"
  >("loading");
  const [streamSrc, setStreamSrc] = useState(streamUrl);
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
        setStreamSrc(`${streamUrl}&_=${Date.now()}`);
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
  }, [streamUrl]);

  // The MJPEG stream lives on a separate channel from the monitor WS, so an
  // <img> hooked to a multipart response will silently freeze on its last
  // frame when the underlying TCP connection dies — the browser only fires
  // `error` on the *initial* fetch failure, not mid-stream. Force a reload
  // whenever the monitor WS recovers from a disconnect. The setState here is
  // deliberate: the effect synchronizes with that external stream lifecycle.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (connected && !wasConnected.current) {
      if (retryTimer.current) {
        clearTimeout(retryTimer.current);
        retryTimer.current = 0;
      }
      retryDelay.current = RETRY_BASE_MS;
      setStreamStatus("loading");
      setStreamSrc(`${streamUrl}&_=${Date.now()}`);
    }
    wasConnected.current = connected;
  }, [connected, streamUrl]);
  /* eslint-enable react-hooks/set-state-in-effect */

  return (
    <div className="feed">
      {streamStatus !== "connected" && (
        <div className="feed-placeholder">
          {streamStatus === "loading" ? "acquiring signal" : "no signal"}
        </div>
      )}
      <img
        ref={imgRef}
        className="feed-stream"
        src={streamSrc}
        alt="Minecraft POV"
        style={{ display: streamStatus === "connected" ? "block" : "none" }}
      />
      <div className="feed-tick tl" />
      <div className="feed-tick tr" />
      <div className="feed-tick bl" />
      <div className="feed-tick br" />
      <div className="feed-cap">cam 01 / mjpeg 10fps</div>
    </div>
  );
}
