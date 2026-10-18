'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { useChatIngest } from '@/hooks/useChatIngest';
import { useCheckIn } from '@/hooks/useCheckIn';
import { usePdfIngest } from '@/hooks/usePdfIngest';
import { useTelemetry } from '@/hooks/useTelemetry';
import type { TelemetryData } from '@/hooks/useTelemetry';
import { useEventBus } from '@/hooks/useEventBus';
import MessageList from './MessageList';
import ChatInput from './ChatInput';
import { MarkdownContent } from './ChatBubble';
import { ProposalCard } from './ProposalCard';
import { rewindChat } from '@/lib/api';
import { notify, userIsAway } from '@/lib/notify';
import type { ChatHistoryRow, ChatIngestResponse, ChatMessage, RegimenProposal } from '@/lib/types';

interface ChatPanelProps {
  onIngestSuccess: (response: ChatIngestResponse) => void;
  droppedPath?: string | null;
  onPathConsumed?: () => void;
  droppedFile?: File | null;
  onFileConsumed?: () => void;
}

// ── Telemetry footer helpers ───────────────────────────────────────────────────

function formatUptime(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function MetricPill({
  label,
  value,
  alert = false,
  warn = false,
}: {
  label: string;
  value: string;
  alert?: boolean;
  warn?: boolean;
}) {
  const valueColor = alert
    ? 'text-rose-500'
    : warn
      ? 'text-amber-400'
      : 'text-zinc-500';
  return (
    <span className="font-mono text-[0.6875rem] tracking-widest">
      <span className="text-zinc-600">{label} </span>
      <span className={valueColor}>{value}</span>
    </span>
  );
}

function TelemetryFooter({ data, online }: { data: TelemetryData | null; online: boolean }) {
  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between border-t border-zinc-800/60 bg-zinc-950/40 px-4 py-2">
        <div className="flex items-center gap-2">
          <div
            className={`h-1.5 w-1.5 rounded-full transition-colors ${
              online
                ? 'bg-emerald-400 shadow-[0_0_5px_rgba(52,211,153,0.7)]'
                : 'bg-rose-500'
            }`}
          />
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
            ENGINE {online ? 'ONLINE' : 'OFFLINE'}
            {data ? ` · UP ${formatUptime(data.uptime_s)}` : ''}
          </span>
        </div>
        {data ? (
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
            {data.records_today} LOG{data.records_today !== 1 ? 'S' : ''} ·{' '}
            {data.api_calls_today} CALL{data.api_calls_today !== 1 ? 'S' : ''} TODAY
          </span>
        ) : (
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-700">--</span>
        )}
      </div>
      <div className="flex items-center gap-5 border-t border-zinc-800/30 bg-zinc-950/70 px-4 py-1.5">
        {data ? (
          <>
            <MetricPill label="CPU" value={`${data.cpu_pct.toFixed(1)}%`} alert={data.cpu_pct > 5} />
            <MetricPill label="MEM" value={`${data.mem_mb} MB`} warn={data.mem_mb > 300} alert={data.mem_mb > 500} />
            <MetricPill label="DB" value={`${data.db_mb} MB`} />
          </>
        ) : (
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-700">
            {online ? 'POLLING…' : 'CONNECT BACKEND TO START'}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Leaked-thinking detector ──────────────────────────────────────────────────
// When the model ignores the <think> tag instruction and outputs its internal
// reasoning as plain paragraphs before the actual response, this function
// detects the leading "wall of planning text" and separates it from the reply.
// Only fires when there are no <think> tags in the text at all.

// Gate patterns: if the VERY FIRST paragraph matches any of these, we enter
// strip mode.  Conservative: only unambiguous meta-commentary phrases.
const LEAKED_THINKING_PREFIXES = [
  /^the user (is|wants|has|needs|asked|said|expressed|mentioned|provided)\b/i,
  /^my plan\b/i,
  /^i (need|should|will|must|plan) to\b/i,
  /^let me (think|analyze|consider|address|break|reconsider)\b/i,
  /^(analyzing|considering|thinking about|evaluating) (this|the)\b/i,
  /^final\s+(check|question|note|step|thought|answer)\b/i,
];

// Scan-phase extras: used AFTER the gate fires to skip additional meta paragraphs.
// Multi-word action-plan labels: "Acknowledge and Validate: Start...", "Re-evaluate the Compromise: The..."
// Requires 2+ words before the colon AND a capital letter after ": " to avoid matching
// single-word labels ("Note:", "LEDGER:") or data lines ("Heart Rate: 67bpm").
// Hyphens allowed in words so "Re-evaluate the Compromise: ..." matches
const PLAN_LABEL_STEP = /^[A-Z][a-z-]+(?:\s+[A-Za-z-]+){1,5}:\s+[A-Z]/;
const NUMBERED_STEP    = /^\d+\.\s+/;

function detectLeakedThinking(text: string): { thinking: string; response: string } | null {
  const trimmed = text.trim();
  const firstLine = trimmed.split('\n')[0].trim();

  // Only trigger when the very first line matches a known thinking-leak pattern
  if (!LEAKED_THINKING_PREFIXES.some((p) => p.test(firstLine))) return null;

  // Primary pass: split by double-newlines (paragraph blocks) and look for the
  // first block whose FIRST LINE is not meta-commentary.
  const blocks = trimmed.split(/\n\n+/);
  if (blocks.length < 2) {
    // Single-block message: fall through to line-level extraction below
  } else {
    let splitAt = -1;
    for (let i = 1; i < blocks.length; i++) {
      const bl = blocks[i].trim();
      const fl = bl.split('\n')[0].trim();
      const isStillMeta =
        LEAKED_THINKING_PREFIXES.some((p) => p.test(fl)) ||
        NUMBERED_STEP.test(fl) ||
        PLAN_LABEL_STEP.test(fl);   // catches "Acknowledge and Validate: ..." etc.
      if (!isStillMeta) {
        splitAt = i;
        break;
      }
    }

    if (splitAt !== -1) {
      return {
        thinking: blocks.slice(0, splitAt).join('\n\n'),
        response:  blocks.slice(splitAt).join('\n\n'),
      };
    }
  }

  // Fallback: paragraph-level detection failed; the model may have put all plan
  // steps on single newlines (no double-newline between them).  Walk every line
  // and find the first line that is NOT meta.  Everything before it is thinking;
  // everything from it onward is the response.
  const allLines = trimmed.split('\n');
  for (let i = 1; i < allLines.length; i++) {
    const line = allLines[i].trim();
    if (!line) continue; // skip blank lines
    const isMeta =
      LEAKED_THINKING_PREFIXES.some((p) => p.test(line)) ||
      NUMBERED_STEP.test(line) ||
      PLAN_LABEL_STEP.test(line);
    if (!isMeta) {
      return {
        thinking: allLines.slice(0, i).join('\n').trim(),
        response:  allLines.slice(i).join('\n').trim(),
      };
    }
  }

  return null; // couldn't find a clean transition; don't guess
}

// ── <think> parser ────────────────────────────────────────────────────────────

interface ParsedResponse {
  thinking: string;
  response: string;
}

function parseThinkTags(text: string): ParsedResponse {
  // Strip <tool_code> artifacts (Gemini 2.5 Pro echoes function calls as pseudo-code)
  const stripped = text.replace(/<tool_code[\s\S]*?<\/tool_code>/g, '').trim();

  // Normalize all variant tag names → lowercase <think> (case-insensitive to handle
  // <THINK>, <Think>, <thinking>, <thought> etc.)
  const normalized = stripped
    .replace(/<thinking>/gi, '<think>').replace(/<\/thinking>/gi, '</think>')
    .replace(/<thought>/gi,  '<think>').replace(/<\/thought>/gi,  '</think>')
    .replace(/<think>/gi,    '<think>').replace(/<\/think>/gi,    '</think>');

  // Collect ALL <think>...</think> blocks (Phase 1 streaming and Phase 2 post-tool)
  // each produce their own block; we need to handle both.
  const thinkParts: string[] = [];
  const responseParts: string[] = [];
  let remaining = normalized;
  // Track whether we have passed at least one complete </think> close tag.
  // • Before the first </think>: any text outside a tag is leaked reasoning
  //   (the system prompt mandates the visible reply starts AFTER </think>).
  // • After the first </think>: text between blocks is legitimate response content.
  let pastFirstClose = false;

  while (remaining.length > 0) {
    const openIdx = remaining.indexOf('<think>');
    if (openIdx === -1) {
      // No more think tags: everything remaining is response text.
      const tail = remaining.trim();
      if (tail) responseParts.push(tail);
      break;
    }

    const before = remaining.slice(0, openIdx).trim();
    if (before) {
      if (pastFirstClose) {
        // Text between a </think> and the next <think>: this IS the response.
        responseParts.push(before);
      } else {
        // Text before the very first <think>: the model wrote reasoning outside
        // a think block: move it to thinkParts so it stays hidden.
        thinkParts.push(before);
      }
    }

    const closeIdx = remaining.indexOf('</think>', openIdx + 7);
    if (closeIdx === -1) {
      // Unclosed tag: still streaming; treat the rest as in-flight thinking.
      thinkParts.push(remaining.slice(openIdx + 7).trim());
      break;
    }
    thinkParts.push(remaining.slice(openIdx + 7, closeIdx).trim());
    pastFirstClose = true;
    remaining = remaining.slice(closeIdx + 8);
  }

  return {
    thinking: thinkParts.filter(Boolean).join('\n\n---\n\n'),
    response: responseParts.filter(Boolean).join('\n\n'),
  };
}

// Resolves a completed (non-streaming) model response into { text, thinkingText }.
// When the model puts its entire reply inside <think>...</think> with nothing after,
// we surface the thinking content as the main response rather than falling back to
// raw markup or showing "No response received."
function resolveCommittedMessage(raw: string): { text: string; thinkingText?: string } {
  // Fast path: if the raw text has no think-tag variants, check for leaked
  // plain-text reasoning BEFORE running the full parser. This handles the case
  // where the model ignores the <think> instruction entirely and dumps its
  // planning as visible paragraphs before the real response.
  const hasThinkTag = /<(think|thinking|thought)[\s>]/i.test(raw);
  if (!hasThinkTag) {
    const leaked = detectLeakedThinking(raw);
    if (leaked?.response) {
      return { text: leaked.response, thinkingText: leaked.thinking || undefined };
    }
  }

  const { thinking, response } = parseThinkTags(raw);
  if (response) {
    return { text: response, thinkingText: thinking || undefined };
  }
  // No visible response extracted: strip all think-tag markup from the raw text
  // and use whatever remains. This handles the edge case where the model wrapped
  // its entire reply inside <think> tags without writing a visible response.
  // We do NOT surface raw thinking content as the reply because that would be a leak.
  const stripped = raw
    .replace(/<tool_code[\s\S]*?<\/tool_code>/gi, '')
    // Remove complete <think>...</think> blocks
    .replace(/<(think|thinking|thought)[^>]*>[\s\S]*?<\/(think|thinking|thought)>/gi, '')
    // Remove any UNCLOSED think tag (stream cut off before </think>) and everything after it
    .replace(/<(think|thinking|thought)[^>]*>[\s\S]*/gi, '')
    // Strip any orphaned closing tags left behind
    .replace(/<\/(think|thinking|thought)>/gi, '')
    .trim();
  // Final guard: if the stripped result still starts with a raw tag marker, don't surface it
  if (stripped && !stripped.startsWith('<')) {
    return { text: stripped, thinkingText: thinking || undefined };
  }
  // Absolute last resort: nothing survived stripping.
  return { text: 'No response received.', thinkingText: thinking || undefined };
}

// ── Pipeline status indicator ─────────────────────────────────────────────────

function PipelineStatus({ msg }: { msg: string | null }) {
  return (
    <div className="mt-3 flex justify-start">
      <div className="border-l-2 border-cyan-500/50 bg-zinc-900/80 py-2.5 pl-3.5 pr-5 ring-1 ring-zinc-800/60">
        <p className="mb-1.5 font-mono text-[0.6875rem] tracking-widest text-cyan-500/60">LEDGER</p>
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-cyan-400">{'>'}</span>
          <span className="font-mono text-xs text-zinc-400">{msg ?? 'Initializing...'}</span>
          <span className="animate-pulse font-mono text-sm text-emerald-400">_</span>
        </div>
      </div>
    </div>
  );
}

// ── Streaming response bubble ─────────────────────────────────────────────────

function StreamingBubble({ text }: { text: string }) {
  const { thinking, response } = parseThinkTags(text);
  return (
    <div className="mt-3 flex justify-start">
      <div className="max-w-[85%] border-l-2 border-emerald-500/40 bg-zinc-900/80 py-2.5 pl-3.5 pr-5 ring-1 ring-zinc-800/60">
        <p className="mb-2 font-mono text-[0.6875rem] tracking-widest text-emerald-500/60">LEDGER</p>
        {thinking && (
          <div className="mb-3 border-l border-zinc-700/60 pl-3">
            <p className="mb-1 font-mono text-[0.625rem] tracking-widest text-zinc-600">
              REASONING
            </p>
            <p className="whitespace-pre-wrap text-xs italic text-zinc-600">{thinking}</p>
          </div>
        )}
        {response && (
          <MarkdownContent text={response} className="text-zinc-100" />
        )}
        {!response && !thinking && (
          <span className="animate-pulse font-mono text-sm text-emerald-400">_</span>
        )}
      </div>
    </div>
  );
}

// ── Logging toggle ────────────────────────────────────────────────────────────

function LoggingToggle({
  enabled,
  onToggle,
}: {
  enabled: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      title={
        enabled
          ? 'Logging ON: entries will be committed to the ledger. Click to switch to brainstorm mode.'
          : 'Brainstorm mode: nothing will be logged. Click to re-enable logging.'
      }
      className="flex items-center gap-2"
    >
      <div
        className={`relative h-3.5 w-7 rounded-full transition-colors duration-200 ${
          enabled ? 'bg-emerald-500/30' : 'bg-zinc-700'
        }`}
      >
        <div
          className={`absolute top-0.5 h-2.5 w-2.5 rounded-full transition-all duration-200 ${
            enabled
              ? 'left-[calc(100%-0.75rem)] bg-emerald-400 shadow-[0_0_4px_rgba(52,211,153,0.6)]'
              : 'left-0.5 bg-zinc-500'
          }`}
        />
      </div>
      <span
        className={`font-mono text-[0.6875rem] tracking-widest transition-colors ${
          enabled ? 'text-emerald-400' : 'text-zinc-500'
        }`}
      >
        {enabled ? 'LOGGING' : 'BRAINSTORM'}
      </span>
    </button>
  );
}

// ── Session ID persistence ────────────────────────────────────────────────────
// Backed by localStorage so page refreshes don't give the AI amnesia.
// Each app launch continues the same session; "New Session" generates a fresh UUID.

function getOrCreateSessionId(): string {
  if (typeof window === 'undefined') return 'default';
  const stored = localStorage.getItem('ledger_session_id');
  if (stored) return stored;
  const id = crypto.randomUUID();
  localStorage.setItem('ledger_session_id', id);
  return id;
}

// ── Panel ─────────────────────────────────────────────────────────────────────

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8787';


const CHECK_IN_DAYS_OPTIONS = [7, 14, 30, 90] as const;

export default function ChatPanel({ onIngestSuccess, droppedPath, onPathConsumed, droppedFile, onFileConsumed }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Always-current ref so callbacks can read latest messages without stale-closure issues.
  const messagesRef = useRef<ChatMessage[]>([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [activeProposal, setActiveProposal] = useState<RegimenProposal | null>(null);
  const [sessionId, setSessionId] = useState<string>(() => getOrCreateSessionId());
  const [loggingEnabled, setLoggingEnabled] = useState(true);
  const [rewindPending, setRewindPending] = useState(false);
  const [rewindStatus, setRewindStatus] = useState<string | null>(null);
  const [rewindStreamingText, setRewindStreamingText] = useState<string | null>(null);
  const [showCheckIn, setShowCheckIn] = useState(false);
  const [checkInDays, setCheckInDays] = useState<number>(14);
  const [checkInFocus, setCheckInFocus] = useState('');
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const { data: telemetry, online } = useTelemetry();
  const { submit, isPending, statusMsg, toolStatus, streamingText, error } = useChatIngest(sessionId, onIngestSuccess);
  const { run: runCheckIn, isPending: checkInPending, statusMsg: checkInStatus,
          streamingText: checkInStreaming, error: checkInError } = useCheckIn();
  const { uploadPath, uploadFile, isPending: pdfPending, statusMsg: pdfStatus,
          streamingText: pdfStreaming, error: pdfError } = usePdfIngest(onIngestSuccess);

  useEffect(() => {
    if (!online || historyLoaded) return;

    fetch(`${API_URL}/api/v1/chat/history?session_id=${sessionId}`)
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then((rows: ChatHistoryRow[]) => {
        if (rows.length === 0) return;
        const loaded: ChatMessage[] = rows.map((row) => {
          if (row.role === 'user') {
            return { id: row.id, role: 'user' as const, text: row.content, timestamp: row.created_at };
          }
          if (row.content === '[SILENT_EXECUTE]') {
            return { id: row.id, role: 'assistant' as const, text: '', isSilent: true, timestamp: row.created_at };
          }
          const { text, thinkingText } = resolveCommittedMessage(row.content);
          return {
            id: row.id,
            role: 'assistant' as const,
            text,
            thinkingText,
            timestamp: row.created_at,
          };
        });
        setMessages(loaded);
      })
      .catch(() => {})
      .finally(() => setHistoryLoaded(true));
  }, [online, historyLoaded, sessionId]);

  // Trigger PDF/CSV processing when a file path arrives from Tauri's drag-drop event
  useEffect(() => {
    if (!droppedPath) return;
    onPathConsumed?.(); // clear immediately so a re-drop of the same file works

    // Sentinel: Tauri got a drop but Chrome used OLE virtual-file format (no real path)
    if (droppedPath === '__NO_PATH__') {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'assistant' as const,
          text: 'Could not read the dropped file: Chrome\'s downloads bar uses a virtual format that prevents direct file access. Try right-clicking the file in Explorer and dragging from there, or use the file upload button.',
          timestamp: new Date().toISOString(),
        },
      ]);
      return;
    }

    const name = droppedPath.split(/[\\/]/).pop() ?? 'document';
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      text: `📄 ${name}`,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    uploadPath(droppedPath)
      .then((response) => {
        const { text: msgText, thinkingText } = resolveCommittedMessage(response.reply ?? '');
        const assistantMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: 'assistant',
          text: msgText,
          thinkingText,
          committed: response.committed ?? null,
          timestamp: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, assistantMsg]);
      })
      .catch(() => {
        // error surfaced via usePdfIngest
      });
  // droppedPath is the only trigger we want; other deps are stable refs
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [droppedPath]);

  // Trigger PDF processing when a File object arrives from HTML5 drag-drop
  useEffect(() => {
    if (!droppedFile) return;
    onFileConsumed?.(); // clear immediately so re-drop of the same file works

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      text: `📄 ${droppedFile.name}`,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);

    uploadFile(droppedFile)
      .then((response) => {
        const { text: msgText, thinkingText } = resolveCommittedMessage(response.reply ?? '');
        const assistantMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: 'assistant',
          text: msgText,
          thinkingText,
          committed: response.committed ?? null,
          timestamp: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, assistantMsg]);
      })
      .catch(() => {
        // error surfaced via usePdfIngest
      });
  // droppedFile is the only trigger we want; other deps are stable refs
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [droppedFile]);

  const handleClear = useCallback(() => {
    if (!confirmClear) {
      setConfirmClear(true);
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
      confirmTimer.current = setTimeout(() => setConfirmClear(false), 3000);
      return;
    }
    if (confirmTimer.current) clearTimeout(confirmTimer.current);
    setConfirmClear(false);
    const newId = crypto.randomUUID();
    localStorage.setItem('ledger_session_id', newId);
    setSessionId(newId);
    setMessages([]);
  }, [confirmClear]);

  useEffect(() => () => { if (confirmTimer.current) clearTimeout(confirmTimer.current); }, []);

  // ── History re-fetch (used after a rewind completes or to sync message IDs) ──
  // Pulls the authoritative session history from the DB and replaces local state.
  // Returns the fresh message list so callers can retry operations with real DB IDs.
  const refetchHistory = useCallback(async (): Promise<ChatMessage[]> => {
    try {
      const res = await fetch(`${API_URL}/api/v1/chat/history?session_id=${sessionId}`);
      if (!res.ok) return [];
      const rows: ChatHistoryRow[] = await res.json();
      const loaded: ChatMessage[] = rows.map((row) => {
        if (row.role === 'user') {
          return { id: row.id, role: 'user' as const, text: row.content, timestamp: row.created_at };
        }
        if (row.content === '[SILENT_EXECUTE]') {
          return { id: row.id, role: 'assistant' as const, text: '', isSilent: true, timestamp: row.created_at };
        }
        const { text, thinkingText } = resolveCommittedMessage(row.content);
        return { id: row.id, role: 'assistant' as const, text, thinkingText, timestamp: row.created_at };
      });
      setMessages(loaded);
      return loaded;
    } catch {
      // non-fatal: the messages state is still coherent from optimistic update
      return [];
    }
  }, [sessionId]);

  // ── Event bus: receive HITL proposals ────────────────────────────────────────
  // Note: rewind history re-fetch is handled by the `done` SSE event in
  // handleRewind (line ~584), not here, to avoid a double refetchHistory()
  // since the backend emits DATA_LOGGED { source: 'chat_rewind' } immediately
  // before yielding `done`, causing both to fire in rapid succession.
  useEventBus(useCallback((event) => {
    if (event.type === 'PROPOSAL_CREATED') {
      setActiveProposal(event.payload as unknown as RegimenProposal);
      return;
    }
    // LEDGER reached out on its own (cognition loop). Append it as a proactive
    // assistant turn, deduped by the DB-assigned id so a later history reload
    // (which loads the same persisted row) doesn't double it up.
    if (event.type === 'LEDGER_THOUGHT') {
      const p = event.payload as { id?: string; content?: string; created_at?: string; signature?: string };
      if (!p.content) return;
      const id = p.id ?? crypto.randomUUID();
      setMessages((prev) =>
        prev.some((m) => m.id === id)
          ? prev
          : [
              ...prev,
              {
                id,
                role: 'assistant' as const,
                text: p.content as string,
                proactive: true,
                signature: p.signature,
                timestamp: p.created_at ?? new Date().toISOString(),
              },
            ],
      );
      if (userIsAway()) void notify('LEDGER', p.content);
    }
  }, []));

  // ── Rewind & Edit ────────────────────────────────────────────────────────────
  // Shared SSE-streaming logic for a rewind attempt with a known DB message ID.
  const _streamRewind = useCallback(async (dbMessageId: string, newContent: string) => {
    const res = await rewindChat(sessionId, dbMessageId, newContent);
    if (!res.body) throw new Error('No response body');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let accText = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith('data: ')) continue;
        let event: Record<string, unknown>;
        try { event = JSON.parse(line.slice(6)); } catch { continue; }

        if (event.type === 'status') {
          setRewindStatus(event.msg as string);
        } else if (event.type === 'chunk') {
          accText += event.text as string;
          setRewindStreamingText(accText);
          setRewindStatus(null);
        } else if (event.type === 'done') {
          // Server has saved both the updated user message and the new model reply.
          // Re-fetch the authoritative history so UUIDs and timestamps are accurate.
          await refetchHistory();
        } else if (event.type === 'error') {
          throw new Error(event.msg as string);
        }
      }
    }
  }, [sessionId, refetchHistory]);

  const handleRewind = useCallback(async (messageId: string, newContent: string) => {
    // Capture the list position of the edited message now, before any async work,
    // so we can locate it in a freshly-fetched list if the ID turns out to be stale.
    const msgIdx = messagesRef.current.findIndex((m) => m.id === messageId);

    // Optimistic update: truncate local state immediately so the UI reflects the
    // rewind without waiting for the server.  Find the edited message, update its
    // text, and remove everything that came after it.
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.id === messageId);
      if (idx === -1) return prev;
      return [
        ...prev.slice(0, idx),
        { ...prev[idx], text: newContent },
        // everything after index idx is pruned; the new response will appear via stream
      ];
    });

    setRewindPending(true);
    setRewindStatus(null);
    setRewindStreamingText(null);

    try {
      await _streamRewind(messageId, newContent);
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);

      if (errMsg === 'Message not found in this session') {
        // The in-memory message ID was a frontend-generated UUID that doesn't match
        // the DB row.  Sync IDs by fetching fresh history, then retry once with the
        // real DB-assigned UUID found at the same list position.
        const fresh = await refetchHistory();
        const realId = (msgIdx !== -1 && msgIdx < fresh.length)
          ? fresh[msgIdx].id
          : null;

        if (realId && realId !== messageId) {
          // Re-apply the optimistic update with the correct ID so streaming renders correctly.
          setMessages((prev) => {
            const idx = prev.findIndex((m) => m.id === realId);
            if (idx === -1) return prev;
            return [
              ...prev.slice(0, idx),
              { ...prev[idx], text: newContent },
            ];
          });
          try {
            await _streamRewind(realId, newContent);
            return; // success: skip the outer finally restore
          } catch (retryErr) {
            console.error('[rewind] retry failed:', retryErr);
            await refetchHistory();
          }
        } else {
          // Couldn't resolve the ID; history is already synced via refetchHistory above.
          console.error('[rewind] could not resolve message ID for retry');
        }
      } else {
        // Generic failure: restore the message list.
        await refetchHistory();
        console.error('[rewind] error:', err);
      }
    } finally {
      setRewindPending(false);
      setRewindStatus(null);
      setRewindStreamingText(null);
    }
  }, [sessionId, refetchHistory, _streamRewind]);

  const handleSubmit = async (text: string) => {
    const localId = crypto.randomUUID();
    const userMsg: ChatMessage = {
      id: localId,
      role: 'user',
      text,
      timestamp: new Date().toISOString(),
      notLogged: !loggingEnabled,
    };
    setMessages((prev) => [...prev, userMsg]);

    try {
      const response = await submit(text, loggingEnabled);

      // Replace the optimistic local UUID with the real DB-assigned ID so that
      // rewind (which looks up the message by ID in the DB) works correctly.
      if (response.userMessageId) {
        const dbId = response.userMessageId;
        setMessages((prev) =>
          prev.map((m) => (m.id === localId ? { ...m, id: dbId } : m)),
        );
      }

      let assistantMsg: ChatMessage;
      if (response.isSilent) {
        assistantMsg = {
          id: crypto.randomUUID(),
          role: 'assistant',
          text: '',
          isSilent: true,
          committed: response.committed ?? null,
          timestamp: new Date().toISOString(),
        };
      } else {
        const { text: msgText, thinkingText } = resolveCommittedMessage(response.reply ?? '');
        assistantMsg = {
          id: crypto.randomUUID(),
          role: 'assistant',
          text: msgText,
          thinkingText,
          committed: response.committed ?? null,
          timestamp: new Date().toISOString(),
        };
      }
      setMessages((prev) => [...prev, assistantMsg]);
    } catch {
      // error state is surfaced via useChatIngest
    }
  };

  // ── HITL proposal handlers ────────────────────────────────────────────────────

  const handleApproveProposal = () => {
    setActiveProposal(null);
    void handleSubmit('I approved and applied the regimen change.');
  };

  const handleRejectProposal = () => {
    setActiveProposal(null);
    void handleSubmit("I rejected the proposed regimen change. Let's adjust it.");
  };

  const handleCheckIn = async () => {
    setShowCheckIn(false);
    const label = checkInFocus.trim()
      ? `Check-in (${checkInDays}d): ${checkInFocus.trim()}`
      : `Check-in (past ${checkInDays} days)`;
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      text: label,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    try {
      const response = await runCheckIn({ days: checkInDays, focus: checkInFocus.trim() || undefined });
      const { text: msgText, thinkingText } = resolveCommittedMessage(response.reply ?? '');
      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        text: msgText,
        thinkingText,
        committed: null,
        timestamp: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, assistantMsg]);
      onIngestSuccess({ status: 'success', reply: response.reply ?? '' });
    } catch {
      // error surfaced via useCheckIn
    }
  };

  const anyPending = isPending || checkInPending || pdfPending || rewindPending;
  const activeStatus = checkInPending ? checkInStatus : pdfPending ? pdfStatus : rewindPending ? rewindStatus : statusMsg;
  const activeStream = checkInPending ? checkInStreaming : pdfPending ? pdfStreaming : rewindPending ? rewindStreamingText : streamingText;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, anyPending, activeStream]);

  // Track scroll position to show/hide the scroll-to-bottom FAB
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const onScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = el;
      setShowScrollBtn(scrollHeight - scrollTop - clientHeight > 150);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  // Keep the ref in sync on every render so callbacks always see the latest messages.
  messagesRef.current = messages;

  return (
    <div className="flex h-full flex-col">
      <header className="shrink-0 flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <h1 className="font-mono text-xs font-semibold tracking-widest text-zinc-200">
          LEDGER
        </h1>
        <div className="flex items-center gap-4">
          <button
            onClick={() => setShowCheckIn((v) => !v)}
            disabled={anyPending || !online}
            className={`font-mono text-[0.6875rem] tracking-widest transition-colors disabled:opacity-30 ${
              showCheckIn ? 'text-cyan-300' : 'text-cyan-500 hover:text-cyan-300'
            }`}
            title="Open check-in panel"
          >
            CHECK IN
          </button>
          <button
            onClick={handleClear}
            disabled={anyPending}
            className={`font-mono text-[0.6875rem] tracking-widest transition-colors disabled:opacity-40 ${
              confirmClear
                ? 'text-rose-400 hover:text-rose-300'
                : 'text-zinc-600 hover:text-zinc-400'
            }`}
            title="Start a new session (preserves all database records)"
          >
            {confirmClear ? 'CONFIRM?' : 'NEW SESSION'}
          </button>
        </div>
      </header>

      {/* Check-in options panel */}
      {showCheckIn && (
        <div className="shrink-0 border-b border-zinc-800 bg-zinc-900/80 px-4 py-3">
          <p className="mb-2 font-mono text-[0.625rem] tracking-widest text-cyan-500/60">CHECK-IN OPTIONS</p>
          <div className="mb-3 flex items-center gap-1">
            {CHECK_IN_DAYS_OPTIONS.map((d) => (
              <button
                key={d}
                onClick={() => setCheckInDays(d)}
                className={`px-2.5 py-1 font-mono text-[0.6875rem] tracking-widest transition-colors ${
                  checkInDays === d
                    ? 'bg-cyan-500/20 text-cyan-300 ring-1 ring-cyan-500/40'
                    : 'text-zinc-500 hover:text-zinc-300'
                }`}
              >
                {d}D
              </button>
            ))}
            <span className="ml-2 font-mono text-[0.6875rem] text-zinc-600">window</span>
          </div>
          <input
            type="text"
            value={checkInFocus}
            onChange={(e) => setCheckInFocus(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCheckIn()}
            placeholder="Focus (optional), e.g. sleep quality, HRV trend..."
            className="mb-3 w-full bg-transparent font-mono text-xs text-zinc-300 placeholder-zinc-600 outline-none"
          />
          <button
            onClick={handleCheckIn}
            disabled={anyPending || !online}
            className="font-mono text-[0.6875rem] tracking-widest text-cyan-400 hover:text-cyan-200 transition-colors disabled:opacity-30"
          >
            RUN CHECK-IN &gt;
          </button>
        </div>
      )}

      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto px-4 py-3">
        <MessageList
          messages={messages}
          onEdit={handleRewind}
          rewindPending={rewindPending}
        />
        {anyPending && activeStream && <StreamingBubble text={activeStream} />}
        {anyPending && !activeStream && <PipelineStatus msg={activeStatus} />}
        <div ref={bottomRef} />

        {/* Scroll-to-bottom FAB: sticky within the scroll container, only visible when scrolled up */}
        {showScrollBtn && (
          <div className="sticky bottom-3 flex justify-end pointer-events-none mt-2">
            <button
              onClick={() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })}
              className="pointer-events-auto flex h-7 w-7 items-center justify-center border border-zinc-700 bg-zinc-900/95 text-zinc-400 shadow-lg transition-colors hover:bg-zinc-800 hover:text-zinc-200"
              title="Scroll to bottom"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
              </svg>
            </button>
          </div>
        )}
      </div>

      {(error || checkInError || pdfError) && (
        <p className="shrink-0 px-4 py-1 font-mono text-xs text-red-400">{error || checkInError || pdfError}</p>
      )}

      <div className="shrink-0">
        <div className="border-t border-zinc-800 px-3 pt-2 pb-0">
          <div className="mb-2 flex items-center justify-end">
            <LoggingToggle
              enabled={loggingEnabled}
              onToggle={() => setLoggingEnabled((v) => !v)}
            />
          </div>
        </div>
        {toolStatus && (
          <div className="px-4 pb-2">
            <div className="animate-pulse font-mono text-[0.75rem] tracking-widest text-cyan-500">
              {'>'} Executing: {toolStatus}...
            </div>
          </div>
        )}
        {activeProposal && (
          <ProposalCard
            proposal={activeProposal}
            onApprove={handleApproveProposal}
            onReject={handleRejectProposal}
          />
        )}
        <div className="px-3 pb-3">
          <ChatInput onSubmit={handleSubmit} disabled={anyPending} />
        </div>
        <TelemetryFooter data={telemetry} online={online} />
      </div>
    </div>
  );
}
