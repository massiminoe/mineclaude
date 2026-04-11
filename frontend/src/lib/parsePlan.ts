// Pure parser for the agent's plan.md document.
//
// The agent writes plans as freeform Markdown — no schema is enforced by
// `writePlan` or `agent/plan.py`. In practice plans follow a familiar shape:
//
//   # Goal: Iron Pickaxe
//
//   ## Steps
//   1. [ ] Get wood
//   2. [x] Craft wooden pickaxe
//   - [ ] Bullet-style steps also work
//
// We extract the goal line and the checkbox items. If no structured steps are
// found, callers should render `raw` as a Markdown/preformatted fallback.

export interface PlanStep {
  text: string;
  done: boolean;
}

export interface PlanView {
  goal: string | null;
  steps: PlanStep[];
  raw: string;
}

const GOAL_RE = /^#\s*Goal:\s*(.+)$/m;
const STEP_RE = /^\s*(?:[-*]|\d+\.)\s*\[([ xX])\]\s*(.+?)\s*$/gm;

export function parsePlan(raw: string): PlanView {
  const trimmed = raw.trim();
  if (!trimmed) return { goal: null, steps: [], raw: "" };

  const goalMatch = trimmed.match(GOAL_RE);
  const goal = goalMatch ? goalMatch[1].trim() : null;

  const steps: PlanStep[] = [];
  for (const m of trimmed.matchAll(STEP_RE)) {
    steps.push({ done: m[1].toLowerCase() === "x", text: m[2].trim() });
  }

  return { goal, steps, raw: trimmed };
}
