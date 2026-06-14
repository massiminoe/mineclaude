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

export interface InventoryItem {
  name: string;
  count: number;
  slot: number;
  /** Present for tools/armor/weapons; absent for stackables. */
  durability?: { remaining: number; max: number };
}

// The bridge's inventory array spans every player slot: 0-35 are the 36 main
// storage slots (0-8 = hotbar), 36-39 are armor, 40 is the offhand. Armor is
// also surfaced via `equipped`, so the grid + the "/36" count use only 0-35.
export const MAIN_SLOTS = 36;
export const OFFHAND_SLOT = 40;

export function usedMainSlots(inv: InventoryItem[]): number {
  return inv.filter((i) => i.slot >= 0 && i.slot < MAIN_SLOTS).length;
}

/** Mainhand + the four armor slots, each a `minecraft:`-stripped item id or
 *  null when empty. Sent verbatim from the bridge's equippedView. */
export interface Equipped {
  hand: string | null;
  head: string | null;
  chest: string | null;
  legs: string | null;
  feet: string | null;
}

export interface GameState {
  position: { x: number; y: number; z: number };
  health: number;
  hunger: number;
  biome: string;
  time: number;
  held_slot?: number;
  inventory: InventoryItem[];
  equipped?: Equipped;
}

// A point on the Events timeline. Both hazard reflex fires (damage_taken,
// hostile_nearby, …) and curated world events (chat, death, respawn,
// advancement) share this shape; the Events rail merges the two streams.
export interface TimelineEvent {
  type: string;
  data: Record<string, unknown>;
  ts: number;
}

/** @deprecated alias kept for callers — use TimelineEvent. */
export type ReflexEvent = TimelineEvent;
