import { useEffect, useRef, useState, useCallback } from "react";
import type {
  ConversationMessage,
  QueueState,
  GameState,
  ActionItem,
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

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/ws`;

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      backoff.current = RECONNECT_BASE;
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
  }, []);

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
        case "game:state":
          setGameState(data as unknown as GameState);
          break;
      }
    },
    []
  );

  const updateQueueFromEvent = useCallback((action: ActionItem) => {
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

  // Fetch initial state and connect WebSocket
  useEffect(() => {
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
