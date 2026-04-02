import { useEffect, useRef } from "react";
import type { ConversationMessage, ContentBlock } from "../types";

interface Props {
  messages: ConversationMessage[];
}

export function ChatOverlay({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="chat-overlay">
      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="chat-empty">Waiting for conversation...</div>
        )}
        {messages.map((msg, i) => (
          <ChatMessage key={i} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
      <div className="chat-input">send message...</div>
    </div>
  );
}

function ChatMessage({ message }: { message: ConversationMessage }) {
  if (typeof message.content === "string") {
    const match = message.content.match(/^(\w+):\s*(.*)/s);
    const name = match ? match[1] : "player";
    const text = match ? match[2] : message.content;
    return (
      <div className="chat-message">
        <span className="chat-name player">{name}</span>
        <span className="chat-text">{text}</span>
      </div>
    );
  }

  const blocks = message.content as ContentBlock[];

  if (message.role === "user") {
    // Tool results — skip in chat overlay (too noisy)
    return null;
  }

  return (
    <>
      {blocks.map((block, i) => {
        if (block.type === "text" && block.text) {
          return (
            <div key={i} className="chat-message">
              <span className="chat-name agent">agent</span>
              <span className="chat-text">{block.text}</span>
            </div>
          );
        }
        if (block.type === "tool_use") {
          const summary = block.name === "newAction"
            ? `${block.name} {code: "${((block.input?.code as string) || "").slice(0, 40)}..."}`
            : `${block.name} ${JSON.stringify(block.input || {}).slice(0, 50)}`;
          return (
            <div key={i} className="chat-tool">
              {"\u25B8"} <span className="chat-tool-name">{summary}</span>
            </div>
          );
        }
        return null;
      })}
    </>
  );
}
