export interface ContentBlock {
  type: "text" | "tool_use" | "tool_result";
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string;
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string | ContentBlock[];
}

export interface SubActionItem {
  id: string;
  name: string;
  args: Record<string, unknown> | null;
  status: "started" | "completed" | "failed";
  started_at: number;
  finished_at: number | null;
  result: unknown;
  error: string | null;
}

export interface ActionItem {
  id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  code: string;
  enqueued_at: number;
  started_at: number | null;
  finished_at: number | null;
  result: string | null;
  error: string | null;
  subactions: SubActionItem[];
}

export interface QueueState {
  running: ActionItem | null;
  pending: ActionItem[];
  recent: ActionItem[];
}

export interface GameState {
  position: { x: number; y: number; z: number };
  health: number;
  hunger: number;
  biome: string;
  time: number;
  inventory: { name: string; count: number; slot: number }[];
}

export interface WSMessage {
  type: string;
  data: Record<string, unknown>;
  ts: number;
}

export interface ReflexEvent {
  type: string;
  data: Record<string, unknown>;
  ts: number;
}
