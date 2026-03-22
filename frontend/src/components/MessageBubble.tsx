import type { ConversationMessage, ContentBlock } from "../types";

interface Props {
  message: ConversationMessage;
}

export function MessageBubble({ message }: Props) {
  if (typeof message.content === "string") {
    return <UserTextBubble text={message.content} />;
  }

  const blocks = message.content as ContentBlock[];

  if (message.role === "user") {
    // Tool results
    const toolResults = blocks.filter((b) => b.type === "tool_result");
    if (toolResults.length > 0) {
      return (
        <>
          {toolResults.map((tr, i) => (
            <ToolResult key={i} block={tr} />
          ))}
        </>
      );
    }
    return null;
  }

  // Assistant blocks
  return (
    <>
      {blocks.map((block, i) => {
        if (block.type === "text") {
          return <AssistantTextBubble key={i} text={block.text || ""} />;
        }
        if (block.type === "tool_use") {
          return <ToolCall key={i} block={block} />;
        }
        return null;
      })}
    </>
  );
}

function UserTextBubble({ text }: { text: string }) {
  const match = text.match(/^(\w+):\s*(.*)/s);
  const username = match ? match[1] : "Player";
  const body = match ? match[2] : text;

  return (
    <div className="bubble-row bubble-left">
      <div className="avatar">{username[0].toUpperCase()}</div>
      <div className="bubble bubble-user">
        <div className="bubble-sender">{username}</div>
        {body}
      </div>
    </div>
  );
}

function AssistantTextBubble({ text }: { text: string }) {
  return (
    <div className="bubble-row bubble-right">
      <div className="bubble bubble-assistant">
        <div className="bubble-sender">Claude</div>
        {text}
      </div>
    </div>
  );
}


function ToolCall({ block }: { block: ContentBlock }) {
  const isNewAction = block.name === "newAction";
  const code = isNewAction
    ? (block.input?.code as string) || ""
    : JSON.stringify(block.input, null, 2);

  return (
    <div className="tool-call">
      <div className="tool-call-header">
        <span className="tool-badge">{block.name}</span>
      </div>
      <pre className="tool-code">{code}</pre>
    </div>
  );
}

function ToolResult({ block }: { block: ContentBlock }) {
  const content = block.content || "";
  const isError = content.toLowerCase().startsWith("error");

  return (
    <div className="tool-result">
      <span className={`tool-result-icon ${isError ? "error" : "success"}`}>
        {isError ? "\u2718" : "\u2714"}
      </span>
      <span className="tool-result-label">Result:</span>
      <span className="tool-result-content">{content}</span>
    </div>
  );
}
