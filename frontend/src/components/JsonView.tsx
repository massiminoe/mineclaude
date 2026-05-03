import { useState } from "react";

interface Props {
  value: unknown;
  // Initial open/closed when the value is large.
  defaultOpen?: boolean;
  // Threshold above which the value collapses by default.
  collapseAt?: number;
  label?: string;
}

// Pretty-printed JSON with click-to-expand for large values.
// Strings get rendered as preformatted text (no JSON quotes) so newlines
// in things like writePlan content / sandbox stdout stay readable.
export function JsonView({ value, defaultOpen, collapseAt = 600, label }: Props) {
  const isString = typeof value === "string";
  const formatted = isString ? (value as string) : safeStringify(value);
  const long = formatted.length > collapseAt;
  const [open, setOpen] = useState(defaultOpen ?? !long);

  if (value === null || value === undefined || formatted === "") {
    return <div className="json-empty">{label ? `${label}: —` : "—"}</div>;
  }

  if (!long) {
    return (
      <pre className={`json-block ${isString ? "json-string" : ""}`}>
        {label && <span className="json-label">{label}: </span>}
        {formatted}
      </pre>
    );
  }

  return (
    <div className="json-collapsible">
      <button
        type="button"
        className="json-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="json-chevron">{open ? "▾" : "▸"}</span>
        {label ? <span className="json-label">{label}</span> : <span className="json-label">expand</span>}
        <span className="json-meta">{formatted.length.toLocaleString()} chars</span>
      </button>
      {open && (
        <pre className={`json-block ${isString ? "json-string" : ""}`}>{formatted}</pre>
      )}
    </div>
  );
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
