import { parseMemory, type MemoryLocation } from "../lib/parseMemory";

interface Props {
  memory: string;
}

export function MemoryCard({ memory }: Props) {
  const view = parseMemory(memory);

  if (!view.raw) {
    return <div className="memory-empty">No memories saved</div>;
  }

  const hasStructured = view.locations.length > 0 || view.notes.length > 0;
  if (!hasStructured) {
    // Author wrote prose or a shape we don't recognise — preserve verbatim.
    return (
      <div className="memory-card">
        <pre className="memory-raw">{view.raw}</pre>
      </div>
    );
  }

  // Group locations by dimension so multi-dimension memory reads cleanly.
  const byDim = new Map<string, MemoryLocation[]>();
  for (const loc of view.locations) {
    const list = byDim.get(loc.dimension) ?? [];
    list.push(loc);
    byDim.set(loc.dimension, list);
  }

  return (
    <div className="memory-card">
      {view.locations.length > 0 && (
        <div className="memory-block">
          <div className="memory-section-label">Locations</div>
          {[...byDim.entries()].map(([dim, locs]) => (
            <div key={dim} className="memory-dim-group">
              {byDim.size > 1 && <div className="memory-dim-heading">{dim}</div>}
              <ul className="memory-locs">
                {locs.map((loc, i) => (
                  <li key={i} className="memory-loc">
                    <span className="memory-loc-name">{loc.name}</span>
                    {dim !== "overworld" && byDim.size === 1 && (
                      <span className="memory-dim-badge">{dim}</span>
                    )}
                    <span className="memory-loc-coords">
                      ({loc.x}, {loc.y}, {loc.z})
                    </span>
                    {loc.notes && (
                      <span className="memory-loc-notes"> — {loc.notes}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      {view.notes.length > 0 && (
        <div className="memory-block">
          <div className="memory-section-label">Notes</div>
          <ul className="memory-notes">
            {view.notes.map((n, i) => (
              <li key={i} className="memory-note">{n}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
