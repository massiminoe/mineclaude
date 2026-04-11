import { parsePlan } from "../lib/parsePlan";

interface Props {
  plan: string;
}

export function PlanCard({ plan }: Props) {
  const view = parsePlan(plan);

  if (!view.raw) {
    return <div className="plan-empty">No active plan</div>;
  }

  if (view.steps.length > 0) {
    // "current" = first undone step (may be -1 if all done)
    const currentIdx = view.steps.findIndex((s) => !s.done);
    return (
      <div className="plan-card">
        {view.goal && <div className="plan-goal">{view.goal}</div>}
        <ul className="plan-steps">
          {view.steps.map((step, i) => {
            const isCurrent = i === currentIdx;
            const cls = step.done
              ? "plan-step plan-step-done"
              : isCurrent
                ? "plan-step plan-step-current"
                : "plan-step plan-step-pending";
            const glyph = step.done ? "\u2713" : isCurrent ? "\u2192" : "\u25CB";
            return (
              <li key={i} className={cls}>
                <span className="plan-step-glyph">{glyph}</span>
                <span className="plan-step-text">{step.text}</span>
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  // Unstructured plan (pure prose, or structured with a shape we don't
  // recognise) — preserve the author's text verbatim.
  return (
    <div className="plan-card">
      <pre className="plan-raw">{view.raw}</pre>
    </div>
  );
}
