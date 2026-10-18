'use client';

import ChatBubble from './ChatBubble';
import type { ChatMessage } from '@/lib/types';

interface MessageListProps {
  messages: ChatMessage[];
  onEdit?: (messageId: string, newContent: string) => void;
  rewindPending?: boolean;
}

export default function MessageList({ messages, onEdit, rewindPending }: MessageListProps) {
  if (messages.length === 0) {
    return (
      <div className="mt-10 flex flex-col items-center gap-3 text-center">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_5px_rgba(52,211,153,0.7)]" />
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">LEDGER ONLINE</span>
        </div>
        <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
          LOG VITALS · ASK QUESTIONS · REVIEW TRENDS
        </p>
      </div>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {messages.map((msg) => (
        <li key={msg.id}>
          <ChatBubble
            message={msg}
            onEdit={onEdit}
            rewindPending={rewindPending}
          />
        </li>
      ))}
    </ul>
  );
}
