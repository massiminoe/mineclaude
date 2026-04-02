import type { QueueState } from "../types";
import { ActionCard } from "./ActionCard";

interface Props {
  queue: QueueState;
}

export function ActionQueue({ queue }: Props) {
  const hasContent =
    queue.running || queue.pending.length > 0 || queue.recent.length > 0;

  return (
    <div className="action-queue">
      {!hasContent && (
        <div className="empty-state">No actions yet</div>
      )}

      {queue.running && (
        <div className="queue-section">
          <div className="queue-section-label">Running</div>
          <ActionCard action={queue.running} />
        </div>
      )}

      {queue.pending.length > 0 && (
        <div className="queue-section">
          <div className="queue-section-label">
            Pending ({queue.pending.length})
          </div>
          {queue.pending.map((action, i) => (
            <ActionCard key={action.id} action={action} position={i + 1} />
          ))}
        </div>
      )}

      {queue.recent.length > 0 && (
        <div className="queue-section">
          <div className="queue-section-label">Recent</div>
          {queue.recent.map((action) => (
            <ActionCard key={action.id} action={action} />
          ))}
        </div>
      )}
    </div>
  );
}
