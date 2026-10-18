'use client';

import { useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { sendCognitionFeedback } from '@/lib/api';
import type { ChatMessage, CommittedCounts } from '@/lib/types';

// ── Timestamp helper ──────────────────────────────────────────────────────────
// Today → "02:47 PM"   |   Previous day → "May 21, 02:47 PM"
//
// SQLite's datetime('now') returns "YYYY-MM-DD HH:MM:SS" (space-separated, no
// timezone suffix).  JS Date treats that format as LOCAL time, but the value is
// stored as UTC, causing an offset equal to the user's UTC timezone difference.
// Fix: detect the SQLite format and append 'Z' so JS parses it correctly as UTC.

function formatTimestamp(iso: string): string {
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(iso)
    ? iso.replace(' ', 'T') + 'Z'
    : iso;
  const d = new Date(normalized);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === new Date().toDateString()) return time;
  const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  return `${date}, ${time}`;
}

interface ChatBubbleProps {
  message: ChatMessage;
  onEdit?: (messageId: string, newContent: string) => void;
  /** While a rewind is in-flight, disable edit buttons everywhere. */
  rewindPending?: boolean;
}

function CommittedPill({ committed }: { committed: CommittedCounts }) {
  const parts: string[] = [];
  if (committed.supplement_logs) parts.push(`${committed.supplement_logs} supplement${committed.supplement_logs !== 1 ? 's' : ''}`);
  if (committed.biometric_logs) parts.push(`${committed.biometric_logs} biometric${committed.biometric_logs !== 1 ? 's' : ''}`);
  if (committed.lab_results) parts.push(`${committed.lab_results} lab result${committed.lab_results !== 1 ? 's' : ''}`);
  if (committed.daily_journals) parts.push(`${committed.daily_journals} journal${committed.daily_journals !== 1 ? 's' : ''}`);
  if (!parts.length) return null;

  return (
    <div className="mt-2 flex items-center gap-1.5">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 shadow-[0_0_4px_rgba(52,211,153,0.8)]" />
      <span className="font-mono text-[0.625rem] tracking-widest text-emerald-500/80 uppercase">
        logged · {parts.join(' · ')}
      </span>
    </div>
  );
}

// ── Markdown prose renderer ───────────────────────────────────────────────────
// Paragraphs / blockquotes use Geist Sans for readable flow text.
// Code, strong, headings, and lists stay mono to preserve the terminal aesthetic.

export function MarkdownContent({ text, className }: { text: string; className?: string }) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Prose: elegant sans-serif
          p:          ({ children }) => <p          className="mb-2 last:mb-0 font-sans text-sm leading-relaxed">{children}</p>,
          em:         ({ children }) => <em         className="font-sans italic text-zinc-300">{children}</em>,
          blockquote: ({ children }) => <blockquote className="my-2 border-l-2 border-zinc-600 pl-3 font-sans text-sm text-zinc-400">{children}</blockquote>,

          // Terminal: mono, structured
          strong: ({ children }) => <strong className="font-mono font-bold text-zinc-50">{children}</strong>,
          ul:     ({ children }) => <ul     className="mb-2 ml-4 list-disc space-y-0.5 font-mono text-sm">{children}</ul>,
          ol:     ({ children }) => <ol     className="mb-2 ml-4 list-decimal space-y-0.5 font-mono text-sm">{children}</ol>,
          li:     ({ children }) => <li     className="leading-relaxed">{children}</li>,
          h1:     ({ children }) => <h1     className="mb-2 mt-3 font-mono text-base font-bold tracking-wide text-zinc-100 first:mt-0">{children}</h1>,
          h2:     ({ children }) => <h2     className="mb-1.5 mt-3 font-mono text-sm font-bold tracking-wide text-zinc-100 first:mt-0">{children}</h2>,
          h3:     ({ children }) => <h3     className="mb-1 mt-2 font-mono text-sm font-semibold text-zinc-200 first:mt-0">{children}</h3>,

          // Code: always emerald mono on zinc-800
          code: ({ children }) => <code className="bg-zinc-800 px-1 py-0.5 font-mono text-xs text-emerald-400">{children}</code>,
          pre:  ({ children }) => <pre  className="mb-2 overflow-x-auto bg-zinc-800 p-3 font-mono text-xs text-emerald-300">{children}</pre>,

          hr: () => <hr className="my-3 border-zinc-700" />,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

// ── Pencil icon ───────────────────────────────────────────────────────────────

function PencilIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      fill="currentColor"
      className="h-3 w-3"
    >
      <path d="M13.488 2.513a1.75 1.75 0 0 0-2.475 0L3.875 9.652a.75.75 0 0 0-.196.37l-.72 3.2a.75.75 0 0 0 .896.896l3.2-.72a.75.75 0 0 0 .37-.196l7.138-7.138a1.75 1.75 0 0 0 0-2.475l-.075-.076Z" />
    </svg>
  );
}

// ── Silent-receipt bubble ─────────────────────────────────────────────────────
// Rendered when the AI executed an administrative task silently ([SILENT_EXECUTE]).
// Appears as a muted inline confirmation rather than a full chat bubble.

function SilentReceiptBubble({ timestamp }: { timestamp: string }) {
  return (
    <div className="flex justify-start">
      <div
        className="flex items-center gap-1.5 px-3 py-1.5 opacity-60"
        title={`Silent execute at ${formatTimestamp(timestamp)}`}
      >
        {/* Thin checkmark icon */}
        <svg
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-2.5 w-2.5 text-zinc-500"
        >
          <path d="M2 6.5l2.5 2.5L10 3.5" />
        </svg>
        <span className="font-mono text-[0.625rem] tracking-widest text-zinc-600">
          System updated.
        </span>
      </div>
    </div>
  );
}

// ── Proactive-message feedback (cognition loop attunement) ─────────────────────
// 👍 lowers the speak bar, 👎 raises it + dismisses the thread, snooze silences the
// loop for a while. Only rendered under unsolicited (proactive) LEDGER messages.

function ProactiveFeedback({ signature }: { signature?: string }) {
  const [sent, setSent] = useState<null | 'up' | 'down' | 'snooze'>(null);

  const send = (rating: 'up' | 'down' | 'snooze') => {
    setSent(rating);
    void sendCognitionFeedback(rating, signature).catch(() => setSent(null));
  };

  if (sent) {
    const label = sent === 'snooze' ? 'snoozed; i\'ll hold off' : sent === 'up' ? 'noted, thanks' : 'understood; i\'ll be more selective';
    return (
      <p className="mt-2 font-mono text-[0.5625rem] tracking-widest text-violet-400/50 uppercase">{label}</p>
    );
  }

  return (
    <div className="mt-2 flex items-center gap-3">
      <button
        onClick={() => send('up')}
        title="Useful: send more like this"
        className="font-mono text-[0.625rem] tracking-widest text-zinc-600 transition-colors hover:text-emerald-400"
      >
        ▲ USEFUL
      </button>
      <button
        onClick={() => send('down')}
        title="Not useful: be more selective"
        className="font-mono text-[0.625rem] tracking-widest text-zinc-600 transition-colors hover:text-rose-400"
      >
        ▼ NOT NOW
      </button>
      <button
        onClick={() => send('snooze')}
        title="Pause proactive messages for a while"
        className="font-mono text-[0.625rem] tracking-widest text-zinc-600 transition-colors hover:text-amber-400"
      >
        ⏸ SNOOZE
      </button>
    </div>
  );
}

export default function ChatBubble({ message, onEdit, rewindPending }: ChatBubbleProps) {
  const isUser = message.role === 'user';
  // All hooks must be declared unconditionally (Rules of Hooks).
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState(message.text);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Short-circuit AFTER all hooks: silent administrative receipts render as a tiny UI chip.
  if (message.isSilent) {
    return <SilentReceiptBubble timestamp={message.timestamp} />;
  }

  const startEdit = () => {
    setEditText(message.text);
    setIsEditing(true);
    // Auto-focus the textarea after React paints it
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const cancelEdit = () => {
    setIsEditing(false);
    setEditText(message.text);
  };

  const submitEdit = () => {
    const trimmed = editText.trim();
    if (!trimmed) {
      cancelEdit();
      return;
    }
    setIsEditing(false);
    onEdit?.(message.id, trimmed);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submitEdit();
    }
    if (e.key === 'Escape') {
      cancelEdit();
    }
  };

  if (isUser) {
    const notLogged = message.notLogged;
    const canEdit = !!onEdit && !rewindPending;

    if (isEditing) {
      return (
        <div className="flex justify-end">
          <div className="w-[80%] border border-cyan-600/40 bg-zinc-900/80 px-3.5 py-2.5 ring-1 ring-cyan-700/30">
            <textarea
              ref={textareaRef}
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={3}
              className="w-full resize-none bg-transparent font-sans text-sm leading-relaxed text-cyan-50 outline-none placeholder-zinc-600"
              placeholder="Edit your message…"
            />
            <div className="mt-2 flex items-center justify-end gap-3">
              <span className="font-mono text-[0.5625rem] tracking-widest text-zinc-600">
                CTRL+ENTER TO SUBMIT
              </span>
              <button
                onClick={cancelEdit}
                className="font-mono text-[0.625rem] tracking-widest text-zinc-500 transition-colors hover:text-zinc-300"
              >
                CANCEL
              </button>
              <button
                onClick={submitEdit}
                disabled={!editText.trim()}
                className="font-mono text-[0.625rem] tracking-widest text-cyan-400 transition-colors hover:text-cyan-200 disabled:opacity-30"
              >
                {editText.trim() === message.text ? 'REGENERATE' : 'SAVE & SUBMIT'}
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="group flex justify-end">
        <div className="flex items-start gap-2">
          {/* Pencil: hover-reveal, aligned to top of bubble */}
          {canEdit && (
            <button
              onClick={startEdit}
              title="Edit and resend"
              className="mt-1 text-zinc-700 opacity-0 transition-all group-hover:opacity-100 hover:text-zinc-400"
            >
              <PencilIcon />
            </button>
          )}
          <div
            className={`max-w-[80%] px-3.5 py-2.5 ${
              notLogged
                ? 'border border-dashed border-zinc-600/60 bg-zinc-800/50'
                : 'bg-cyan-800/60 ring-1 ring-cyan-700/40'
            }`}
          >
            <p className={`font-sans text-sm leading-relaxed ${notLogged ? 'text-zinc-300' : 'text-cyan-50'}`}>
              {message.text}
            </p>
            <div className="mt-1.5 flex items-center justify-end gap-2">
              {notLogged && (
                <span className="font-mono text-[0.5625rem] tracking-widest text-zinc-600 uppercase">not logged</span>
              )}
              <span className={`font-mono text-[0.625rem] tracking-widest ${notLogged ? 'text-zinc-600' : 'text-cyan-400/50'}`}>
                {formatTimestamp(message.timestamp)}
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const proactive = message.proactive;
  return (
    <div className="flex justify-start">
      <div
        className={`max-w-[85%] py-2.5 pl-3.5 pr-4 ring-1 ${
          proactive
            ? 'border-l-2 border-violet-400/70 bg-violet-950/20 ring-violet-900/40'
            : 'border-l-2 border-emerald-500/40 bg-zinc-900/80 ring-zinc-800/60'
        }`}
      >
        <p
          className={`mb-1.5 flex items-center gap-1.5 font-mono text-[0.6875rem] tracking-widest ${
            proactive ? 'text-violet-300/80' : 'text-emerald-500/60'
          }`}
        >
          {proactive && <span className="text-violet-400">✦</span>}
          LEDGER{proactive && <span className="text-violet-400/50">· UNPROMPTED</span>}
        </p>
        {message.thinkingText && (
          <div className="mb-3">
            <button
              onClick={() => setThinkingOpen((o) => !o)}
              className="flex items-center gap-1.5 group"
            >
              <span className="font-mono text-[0.625rem] tracking-widest text-zinc-600 group-hover:text-zinc-400 transition-colors">
                REASONING
              </span>
              <span className="font-mono text-[0.625rem] text-zinc-700 group-hover:text-zinc-500 transition-colors">
                {thinkingOpen ? '▲' : '▼'}
              </span>
            </button>
            {thinkingOpen && (
              <div className="mt-1.5 border-l border-zinc-700 pl-3">
                <p className="text-xs italic leading-relaxed text-zinc-600">{message.thinkingText}</p>
              </div>
            )}
          </div>
        )}
        <MarkdownContent text={message.text} className="text-zinc-200" />
        {message.committed && <CommittedPill committed={message.committed} />}
        {proactive && <ProactiveFeedback signature={message.signature} />}
        <p className="mt-2 font-mono text-[0.625rem] tracking-widest text-zinc-600">
          {formatTimestamp(message.timestamp)}
        </p>
      </div>
    </div>
  );
}
