// Types & helpers for the session trace viewer (mirrors agent/session_log.py).

export interface SessionSummary {
  stem: string;
  size: number;
  mtime: number;
  started_at: number | null;
  ended_at: number | null;
  turn_count: number;
  iteration_count: number;
  tool_call_count: number;
  screenshot_count: number;
  belief_mismatch_count: number;
  exception_count: number;
  first_user_message: string | null;
  session_id: string | null;
  usage?: SessionUsage;
}

export interface SessionUsage {
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  cost_usd: number;
  calls: number;
}

export interface TraceEvent {
  ts: number;
  session_id: string;
  event: string;
  data: Record<string, unknown>;
}

export interface SessionDetail {
  stem: string;
  summary: SessionSummary;
  events: TraceEvent[];
}

// A "turn" groups every event between a chat_in and the next chat_in (or EOF).
// Within a turn, claude_request/response/tool_dispatch are grouped by iteration.
export interface ToolCall {
  event: TraceEvent;
  iteration: number;
  name: string;
  toolUseId: string | null;
  input: unknown;
  result: unknown;
  elapsedMs: number | null;
  imagePath: string | null;
}

export interface Iteration {
  number: number;
  request: TraceEvent | null;
  response: TraceEvent | null;
  toolCalls: ToolCall[];
  // Belief mismatches / exceptions / subactions that happened during this iteration.
  sideEvents: TraceEvent[];
  usage: TraceEvent | null;
}

export interface Turn {
  startTs: number;
  endTs: number;
  user: TraceEvent | null;     // the chat_in
  agentReplies: TraceEvent[];  // chat_out events
  iterations: Iteration[];
  // Anything that landed before the first iteration of the turn or didn't fit.
  loose: TraceEvent[];
}

export interface SessionTimeline {
  turns: Turn[];
  // Events outside any turn (session_open, pre-first-chat events).
  preamble: TraceEvent[];
}

export function buildTimeline(events: TraceEvent[]): SessionTimeline {
  const turns: Turn[] = [];
  let preamble: TraceEvent[] = [];
  let current: Turn | null = null;
  let currentIter: Iteration | null = null;

  const newIter = (n: number): Iteration => ({
    number: n,
    request: null,
    response: null,
    toolCalls: [],
    sideEvents: [],
    usage: null,
  });

  for (const ev of events) {
    if (ev.event === "chat_in") {
      if (current) turns.push(current);
      current = {
        startTs: ev.ts,
        endTs: ev.ts,
        user: ev,
        agentReplies: [],
        iterations: [],
        loose: [],
      };
      currentIter = null;
      continue;
    }

    if (!current) {
      preamble.push(ev);
      continue;
    }

    current.endTs = ev.ts;

    const iterNum = (ev.data as { iteration?: number })?.iteration;

    if (ev.event === "claude_request") {
      currentIter = newIter(typeof iterNum === "number" ? iterNum : current.iterations.length + 1);
      currentIter.request = ev;
      current.iterations.push(currentIter);
      continue;
    }
    if (ev.event === "claude_response") {
      if (currentIter) {
        currentIter.response = ev;
      } else {
        current.loose.push(ev);
      }
      continue;
    }
    if (ev.event === "claude_usage") {
      // claude_usage fires immediately after claude_response for the
      // main-loop call; also fires during compaction (no surrounding
      // request/response, so it falls through to loose).
      if (currentIter) {
        currentIter.usage = ev;
      } else if (current) {
        current.loose.push(ev);
      } else {
        preamble.push(ev);
      }
      continue;
    }
    if (ev.event === "tool_dispatch") {
      const data = ev.data as Record<string, unknown>;
      const result = data.result;
      const imgPath =
        result && typeof result === "object" && (result as Record<string, unknown>).type === "image"
          ? ((result as Record<string, unknown>).image_path as string | null) ?? null
          : null;
      const tc: ToolCall = {
        event: ev,
        iteration: typeof iterNum === "number" ? iterNum : currentIter?.number ?? 0,
        name: (data.name as string) ?? "(unknown)",
        toolUseId: (data.tool_use_id as string) ?? null,
        input: data.input,
        result,
        elapsedMs: typeof data.elapsed_ms === "number" ? (data.elapsed_ms as number) : null,
        imagePath: imgPath,
      };
      if (currentIter) {
        currentIter.toolCalls.push(tc);
      } else {
        current.loose.push(ev);
      }
      continue;
    }
    if (ev.event === "chat_out") {
      current.agentReplies.push(ev);
      if (currentIter) currentIter.sideEvents.push(ev);
      continue;
    }
    // belief_mismatch, exception, subaction, anything else
    if (currentIter) {
      currentIter.sideEvents.push(ev);
    } else {
      current.loose.push(ev);
    }
  }
  if (current) turns.push(current);
  return { turns, preamble };
}

export function formatTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m ${s}s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}
