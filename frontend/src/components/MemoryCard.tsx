import ReactMarkdown from "react-markdown";

interface Props {
  memory: string;
}

// Memory is plain markdown — the agent structures it however it likes.
// We render it as-is; no schema to parse.
export function MemoryCard({ memory }: Props) {
  if (!memory.trim()) {
    return <div className="memory-empty">No memories saved</div>;
  }

  return (
    <div className="memory-card">
      <div className="memory-md">
        <ReactMarkdown>{memory}</ReactMarkdown>
      </div>
    </div>
  );
}
