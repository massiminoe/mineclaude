import { useEffect, useRef, useState, useCallback } from "react";
import type {
  ConversationMessage,
  QueueState,
  GameState,
  ActionItem,
  SubActionItem,
} from "../types";

const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 15000;

export function useSocket() {
  const [conversation, setConversation] = useState<ConversationMessage[]>([]);
  const [queue, setQueue] = useState<QueueState>({
    running: null,
    pending: [],
    recent: [],
  });
  const [gameState, setGameState] = useState<GameState | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<number>(0);
  const backoff = useRef(RECONNECT_BASE);

  const fetchState = useCallback(() => {
    fetch("/api/state")
      .then((r) => r.json())
      .then((data) => {
        if (data.conversation) setConversation(data.conversation);
        if (data.queue) setQueue(data.queue);
        if (data.game) setGameState(data.game);
      })
      .catch(() => {
        // Agent might not be running yet
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

  return { conversation, queue, gameState, connected };
}
