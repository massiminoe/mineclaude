import { useEffect, useRef, useState, useCallback } from "react";
import type {
  ConversationMessage,
  QueueState,
  GameState,
  ActionItem,
  SubActionItem,
  ReflexEvent,
} from "../types";

const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 15000;
// Cap the in-memory reflex log; matches the agent's RECENT_MAXLEN.
const REFLEX_LOG_MAX = 10;

export function useSocket() {
  const [conversation, setConversation] = useState<ConversationMessage[]>([]);
  const [queue, setQueue] = useState<QueueState>({
    running: null,
    pending: [],
    recent: [],
  });
  const [gameState, setGameState] = useState<GameState | null>(null);
  const [plan, setPlan] = useState<string>("");
  const [memory, setMemory] = useState<string>("");
  const [reflexes, setReflexes] = useState<ReflexEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<number>(0);
  const backoff = useRef(RECONNECT_BASE);

  const fetchState = useCallback((attempt = 0) => {
    fetch("/api/state")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (data.conversation) setConversation(data.conversation);
        if (data.queue) setQueue(data.queue);
        if (data.game) setGameState(data.game);
        if (data.plan !== undefined) setPlan(data.plan ?? "");
        if (data.memory !== undefined) setMemory(data.memory ?? "");
        if (Array.isArray(data.reflexes)) setReflexes(data.reflexes);
      })
      .catch(() => {
        // Likely a race with the monitor coming up — retry a few times
        // so a stale UI recovers without the user refreshing the page.
        if (attempt < 3) {
          const delay = 500 * Math.pow(2, attempt);
          window.setTimeout(() => fetchState(attempt + 1), delay);
        }
      });
  }, []);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/ws`;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoff.current = RECONNECT_BASE;
      // Re-sync full state on every (re)connect so a stale page recovers
      // automatically after the agent restarts.
      fetchState();
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      const jitter = Math.random() * backoff.current * 0.5;
      reconnectTimeout.current = window.setTimeout(() => {
        connect();
      }, backoff.current + jitter);
      backoff.current = Math.min(backoff.current * 2, RECONNECT_MAX);
    };

    ws.onerror = () => {
      // Funnel all failure paths through onclose so backoff is unambiguous.
      // Some browsers fire onerror without a prompt onclose on cold restarts.
      if (ws.readyState !== WebSocket.CLOSED) {
        try {
          ws.close();
        } catch {
          // ignore
        }
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleMessage(msg.type, msg.data);
      } catch {
        // ignore malformed messages
      }
    };
  }, [fetchState]);

  const handleMessage = useCallback(
    (type: string, data: Record<string, unknown>) => {
      switch (type) {
        case "conversation:update":
          setConversation(data.messages as ConversationMessage[]);
          break;
        case "action_enqueued":
        case "action_started":
        case "action_completed":
          updateQueueFromEvent(data.action as ActionItem);
          break;
        case "subaction_started":
        case "subaction_completed":
          updateSubAction(
            data.action_id as string,
            data.subaction as SubActionItem
          );
          break;
        case "game:state":
          setGameState(data as unknown as GameState);
          break;
        case "plan:update":
          setPlan((data.plan as string) ?? "");
          break;
        case "memory:update":
          setMemory((data.memory as string) ?? "");
          break;
        case "reflex:fired":
          // Dedupe against the REST snapshot fetched on (re)connect: if the
          // event fires while fetchState is in flight, the snapshot already
          // contains it and the WS push would otherwise add a second copy.
          // `ts` is set once at dispatch time and is identical on both paths.
          setReflexes((prev) => {
            const incoming = data as unknown as ReflexEvent;
            if (prev.some((r) => r.ts === incoming.ts && r.type === incoming.type)) {
              return prev;
            }
            return [incoming, ...prev].slice(0, REFLEX_LOG_MAX);
          });
          break;
      }
    },
    []
  );

  const updateQueueFromEvent = useCallback((action: ActionItem) => {
    // Ensure subactions array exists
    if (!action.subactions) action.subactions = [];

    setQueue((prev) => {
      const newQueue = { ...prev };

      // Remove from pending if present
      newQueue.pending = prev.pending.filter((a) => a.id !== action.id);

      if (action.status === "running") {
        newQueue.running = action;
      } else if (
        action.status === "completed" ||
        action.status === "failed" ||
        action.status === "cancelled"
      ) {
        if (prev.running?.id === action.id) {
          newQueue.running = null;
        }
        newQueue.recent = [action, ...prev.recent.filter((a) => a.id !== action.id)].slice(0, 10);
      } else if (action.status === "pending") {
        newQueue.pending = [...prev.pending.filter((a) => a.id !== action.id), action];
      }

      return newQueue;
    });
  }, []);

  const updateSubAction = useCallback(
    (actionId: string, sub: SubActionItem) => {
      setQueue((prev) => {
        const update = (action: ActionItem): ActionItem => {
          const subs = action.subactions ? [...action.subactions] : [];
          const idx = subs.findIndex((s) => s.id === sub.id);
          if (idx >= 0) {
            subs[idx] = sub;
          } else {
            subs.push(sub);
          }
          return { ...action, subactions: subs };
        };

        const newQueue = { ...prev };

        if (prev.running?.id === actionId) {
          newQueue.running = update(prev.running);
        } else {
          newQueue.recent = prev.recent.map((a) =>
            a.id === actionId ? update(a) : a
          );
        }

        return newQueue;
      });
    },
    []
  );

  // Connect WebSocket — initial state is fetched in onopen via fetchState()
  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimeout.current) {
        clearTimeout(reconnectTimeout.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect]);

  return { conversation, queue, gameState, plan, memory, reflexes, connected };
}
