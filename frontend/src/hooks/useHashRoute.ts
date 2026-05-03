import { useEffect, useState } from "react";

// Tiny hash router: returns the current hash (without the leading "#")
// and a setter that updates window.location.hash.
export function useHashRoute(): [string, (next: string) => void] {
  const [hash, setHash] = useState<string>(() =>
    window.location.hash.startsWith("#") ? window.location.hash.slice(1) : ""
  );

  useEffect(() => {
    const onChange = () => {
      setHash(window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "");
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = (next: string) => {
    const target = next.startsWith("#") ? next : `#${next}`;
    if (window.location.hash !== target) {
      window.location.hash = target;
    }
  };

  return [hash, navigate];
}

export interface ParsedRoute {
  view: "monitor" | "sessions-list" | "session-detail";
  stem?: string;
}

export function parseRoute(hash: string): ParsedRoute {
  const trimmed = hash.replace(/^\/+/, "");
  if (!trimmed) return { view: "monitor" };
  const parts = trimmed.split("/");
  if (parts[0] === "sessions") {
    if (parts.length === 1 || !parts[1]) return { view: "sessions-list" };
    return { view: "session-detail", stem: decodeURIComponent(parts[1]) };
  }
  return { view: "monitor" };
}
