import { useEffect, useRef } from "react";
import type { ConversationMessage } from "../types";
import { MessageBubble } from "./MessageBubble";

interface Props {
  messages: ConversationMessage[];
}

export function ConversationPanel({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="conversation-panel">
      <div className="panel-header">Claude Conversation</div>
      <div className="conversation-messages">
        {messages.length === 0 && (
          <div className="empty-state">Waiting for conversation...</div>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={i} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
