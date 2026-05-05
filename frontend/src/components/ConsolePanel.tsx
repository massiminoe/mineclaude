import { useEffect, useRef, useState } from "react";

const HISTORY_KEY = "mineclaude.console.history";
const COLLAPSED_KEY = "mineclaude.console.collapsed";
const HISTORY_LIMIT = 50;

const PRIMITIVE_GROUPS: { label: string; items: string[] }[] = [
  {
    label: "movement",
    items: [
      "await goToPosition(x, y, z)",
      "await goToPlayer(player, distance=3)",
      "await followPlayer(player, distance=3)",
      "await stop()",
    ],
  },
  {
    label: "world",
    items: [
      "await placeBlock(block_type, x, y, z, face='top')",
      "await breakBlockAt(x, y, z)",
      "await collectItems(radius=6)",
      "await attack(entity_id)",
    ],
  },
  {
    label: "inventory",
    items: [
      "await craft(item, count=1)",
      "await furnaceLoad(input_item, input_count, fuel_item, fuel_count)",
      "await furnaceInspect()",
      "await furnaceExtract()",
      "await equip(item, slot='hand')",
      "await discard(item, count=1)",
    ],
  },
  {
    label: "observation",
    items: [
      "await getStats()",
      "await getInventory()",
      "await getNearbyBlocks(range_=16)",
      "await getNearbyEntities(range_=32)",
      "await findBlocks(block_type, range_=32, count=10)",
      "await findEntities(entity_type, range_=32)",
    ],
  },
  {
    label: "misc",
    items: ["await sleep(seconds)", "log(message)"],
  },
];

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((s) => typeof s === "string") : [];
  } catch {
    return [];
  }
}

function saveHistory(history: string[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-HISTORY_LIMIT)));
  } catch {
    // ignore
  }
}

export function ConsolePanel() {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    return localStorage.getItem(COLLAPSED_KEY) !== "false";
  });
  const [code, setCode] = useState("");
  const [history, setHistory] = useState<string[]>(() => loadHistory());
  // -1 = not navigating; otherwise an index into `history`
  const [historyIdx, setHistoryIdx] = useState<number>(-1);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, String(collapsed));
  }, [collapsed]);

  async function run() {
    const trimmed = code.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setStatus("");
    try {
      const resp = await fetch("/api/console/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: trimmed }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        setStatus(`error: ${body.error ?? resp.statusText}`);
        return;
      }
      const body = await resp.json();
      setStatus(`enqueued ${body.action_id} — see Action Queue for trace`);
      const next = [...history.filter((h) => h !== trimmed), trimmed];
      setHistory(next);
      saveHistory(next);
      setHistoryIdx(-1);
      setCode("");
    } catch (e) {
      setStatus(`error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setStatus("");
    try {
      const resp = await fetch("/api/console/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (resp.ok) {
        setStatus("queue interrupted");
      } else {
        setStatus(`cancel error: ${resp.statusText}`);
      }
    } catch (e) {
      setStatus(`cancel error: ${(e as Error).message}`);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Ctrl/Cmd-Enter runs
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      run();
      return;
    }
    // Ctrl/Alt-ArrowUp/Down navigates history (avoids fighting normal multi-line editing)
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && (e.ctrlKey || e.altKey)) {
      e.preventDefault();
      if (history.length === 0) return;
      let next = historyIdx;
      if (e.key === "ArrowUp") {
        next = next === -1 ? history.length - 1 : Math.max(0, next - 1);
      } else {
        next = next === -1 ? -1 : next + 1;
        if (next >= history.length) next = -1;
      }
      setHistoryIdx(next);
      setCode(next === -1 ? "" : history[next]);
    }
  }

  if (collapsed) {
    return (
      <div className="console-panel collapsed">
        <button
          type="button"
          className="console-header"
          onClick={() => setCollapsed(false)}
          aria-label="Expand console"
        >
          <span className="console-chevron">▸</span>
          <span>Console</span>
          <span className="console-hint">click to open</span>
        </button>
      </div>
    );
  }

  return (
    <div className="console-panel">
      <button
        type="button"
        className="console-header"
        onClick={() => setCollapsed(true)}
        aria-label="Collapse console"
      >
        <span className="console-chevron">▾</span>
        <span>Console</span>
        <span className="console-hint">human primitive runner</span>
      </button>

      <textarea
        ref={textareaRef}
        className="console-textarea"
        value={code}
        onChange={(e) => {
          setCode(e.target.value);
          setHistoryIdx(-1);
        }}
        onKeyDown={onKeyDown}
        placeholder="await goToPosition(0, 64, 0)"
        rows={6}
        spellCheck={false}
      />

      <div className="console-actions">
        <button
          type="button"
          className="console-btn console-btn-primary"
          onClick={run}
          disabled={busy || !code.trim()}
          title="Ctrl/Cmd-Enter"
        >
          Run
        </button>
        <button
          type="button"
          className="console-btn"
          onClick={cancel}
          title="Interrupt the running action and clear pending"
        >
          Cancel
        </button>
        <span className="console-status">{status}</span>
      </div>

      <details className="console-cheatsheet">
        <summary>Primitives</summary>
        {PRIMITIVE_GROUPS.map((group) => (
          <div key={group.label} className="console-cheatsheet-group">
            <div className="console-cheatsheet-label">{group.label}</div>
            {group.items.map((sig) => (
              <button
                key={sig}
                type="button"
                className="console-cheatsheet-item"
                onClick={() => {
                  setCode((prev) => (prev ? prev + "\n" : "") + sig);
                  textareaRef.current?.focus();
                }}
                title="click to append"
              >
                {sig}
              </button>
            ))}
          </div>
        ))}
      </details>

      <div className="console-footer">
        Ctrl/Cmd-Enter to run · Ctrl/Alt-↑/↓ for history ({history.length})
      </div>
    </div>
  );
}
