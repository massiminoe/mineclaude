import { useState, useRef, useEffect } from "react";

interface Props {
  bridgeUrl?: string;
}

export function VideoStream({ bridgeUrl = "http://localhost:8080" }: Props) {
  const [status, setStatus] = useState<"loading" | "connected" | "error">(
    "loading"
  );
  const imgRef = useRef<HTMLImageElement>(null);

  const streamUrl = `${bridgeUrl}/video/stream?fps=10&quality=50`;

  useEffect(() => {
    const img = imgRef.current;
    if (!img) return;

    const onLoad = () => setStatus("connected");
    const onError = () => setStatus("error");

    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);

    return () => {
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
    };
  }, []);

  return (
    <div className="video-container">
      {status === "error" && (
        <div className="video-placeholder">
          <div className="video-icon">&#9654;</div>
          <div className="video-label">Video Stream</div>
          <div className="video-sublabel">
            Waiting for bridge connection...
          </div>
        </div>
      )}
      <img
        ref={imgRef}
        className="video-stream"
        src={streamUrl}
        alt="Minecraft POV"
        style={{ display: status === "connected" ? "block" : "none" }}
      />
      {status === "loading" && (
        <div className="video-placeholder">
          <div className="video-icon">&#9654;</div>
          <div className="video-label">Video Stream</div>
          <div className="video-sublabel">Connecting...</div>
        </div>
      )}
    </div>
  );
}

// Keep backward-compatible export name
export function VideoPlaceholder() {
  return <VideoStream />;
}
