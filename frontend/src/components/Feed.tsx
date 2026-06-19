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
  const feedRef = useRef<HTMLDivElement>(null);
  const retryTimer = useRef<number>(0);
  const retryDelay = useRef(RETRY_BASE_MS);
  const wasConnected = useRef(connected);

  // Fullscreen: prefer the native Fullscreen API (hides browser chrome — best
  // for watching on a phone in landscape). It rejects on browsers that won't
  // fullscreen a non-<video> element (notably iOS Safari on our <img> feed), so
  // we fall back to a CSS fixed-overlay that works everywhere. `fs` reflects
  // either path; `manualFs` is the fallback's own state.
  const [manualFs, setManualFs] = useState(false);
  const [nativeFs, setNativeFs] = useState(false);
  const fs = nativeFs || manualFs;

  useEffect(() => {
    const sync = () => setNativeFs(document.fullscreenElement === feedRef.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  // Escape closes the manual (non-native) overlay; native fullscreen handles
  // Escape itself via the browser.
  useEffect(() => {
    if (!manualFs) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setManualFs(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [manualFs]);

  const toggleFullscreen = () => {
    const el = feedRef.current;
    if (!el) return;
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else if (manualFs) {
      setManualFs(false);
    } else if (el.requestFullscreen) {
      el.requestFullscreen().catch(() => setManualFs(true));
    } else {
      setManualFs(true);
    }
  };

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
    <div ref={feedRef} className={`feed${manualFs ? " feed-manual-fs" : ""}`}>
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
      <button
        className="feed-fs-btn"
        onClick={toggleFullscreen}
        title={fs ? "Exit fullscreen" : "Fullscreen"}
        aria-label={fs ? "Exit fullscreen" : "Enter fullscreen"}
      >
        {fs ? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
          </svg>
        )}
      </button>
    </div>
  );
}
