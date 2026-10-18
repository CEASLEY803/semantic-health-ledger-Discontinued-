'use client';

import { useState, useRef, KeyboardEvent, useEffect, useCallback } from 'react';
import { syncGarmin } from '@/lib/api';
import { bus } from '@/lib/events';

// ── Reference chip type ───────────────────────────────────────────────────────

export interface HealthReference {
  label: string;
  value: string;
}

// ── Types ─────────────────────────────────────────────────────────────────────

type SyncState = 'idle' | 'syncing' | 'success' | 'error';

interface ChatInputProps {
  onSubmit: (text: string) => void;
  disabled?: boolean;
}

// ── Icons ─────────────────────────────────────────────────────────────────────

function SyncIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99" />
    </svg>
  );
}

function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
    </svg>
  );
}

function XIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
    </svg>
  );
}

// ── Garmin sync button ────────────────────────────────────────────────────────
//
// Behaviour:
//   • Auto-syncs on mount (2 s delay so the app is fully loaded first).
//     Only fetches dates that have no data yet; never re-downloads existing rows.
//     Silently ignores "not set up" errors so the button stays quiet if Garmin
//     hasn't been configured.
//   • Regular click   → same gap-fill sync (idempotent, fast when up-to-date).
//   • Shift+click     → force mode: re-touches all 30 days and adds any metrics
//     that were missing (e.g. HRV / sleep stages added after initial backfill).
//     Per-metric dedup still prevents actual duplicates.

function GarminSyncButton() {
  const [syncState, setSyncState]   = useState<SyncState>('idle');
  const [tooltip,   setTooltip]     = useState<string | null>(null);
  const [forceMode, setForceMode]   = useState(false);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Guard: only allow one auto-sync per mount.
  const autoSyncFired = useRef(false);

  const scheduleReset = useCallback(() => {
    if (resetTimer.current) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => {
      setSyncState('idle');
      setTooltip(null);
      setForceMode(false);
    }, 3500);
  }, []);

  useEffect(() => {
    return () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    };
  }, []);

  // ── Core sync logic ───────────────────────────────────────────────────────

  const runSync = useCallback(async (opts: { force?: boolean; silent?: boolean } = {}) => {
    if (syncState === 'syncing') return;
    if (resetTimer.current) clearTimeout(resetTimer.current);

    if (!opts.silent) {
      setSyncState('syncing');
      setTooltip(null);
    } else {
      // Show a subtle activity indicator even for silent/background syncs
      setSyncState('syncing');
    }

    try {
      const result = await syncGarmin({ force: opts.force });
      const n       = result.synced.length;
      const days    = result.dates_synced ?? 0;
      const skipped = result.skipped.length;

      setSyncState('success');

      if (n > 0) {
        setTooltip(
          opts.force
            ? `Force-synced ${n} reading${n !== 1 ? 's' : ''} across ${days} day${days !== 1 ? 's' : ''}${skipped > 0 ? ` · ${skipped} skipped` : ''}`
            : `Synced ${n} reading${n !== 1 ? 's' : ''} across ${days} day${days !== 1 ? 's' : ''}${skipped > 0 ? ` · ${skipped} skipped` : ''}`,
        );
      } else if (opts.silent) {
        // Nothing new on startup: reset quietly without showing a tooltip
        setSyncState('idle');
        return;
      } else {
        setTooltip('Already up-to-date');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Garmin sync failed';
      const isNotConfigured = msg.includes('not set up') || msg.includes('401') || msg.includes('422');

      if (opts.silent && isNotConfigured) {
        // Garmin hasn't been set up yet: stay quiet, don't alarm the user
        setSyncState('idle');
        return;
      }

      setSyncState('error');
      setTooltip(msg.replace(/^API \d+: /, '').slice(0, 80));
    }

    scheduleReset();
  }, [syncState, scheduleReset]);

  // ── Auto-sync on mount ────────────────────────────────────────────────────

  useEffect(() => {
    if (autoSyncFired.current) return;
    autoSyncFired.current = true;

    const timer = setTimeout(() => {
      runSync({ silent: true });
    }, 2000);  // 2 s delay lets the rest of the UI settle first

    return () => clearTimeout(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // intentionally empty: fire once on mount only

  // ── Click handler ─────────────────────────────────────────────────────────

  const handleClick = (e: React.MouseEvent) => {
    const force = e.shiftKey;
    setForceMode(force);
    runSync({ force });
  };

  // ── Render ────────────────────────────────────────────────────────────────

  const iconClass = 'h-3.5 w-3.5';

  const colorClass: Record<SyncState, string> = {
    idle:    'text-zinc-600 hover:text-zinc-300',
    syncing: 'text-zinc-500 cursor-not-allowed',
    success: 'text-emerald-400',
    error:   'text-rose-500',
  };

  const hoverLabel = forceMode ? 'FORCE SYNC' : 'GARMIN SYNC';

  return (
    <div className="group relative">
      <button
        onClick={handleClick}
        disabled={syncState === 'syncing'}
        aria-label={forceMode ? 'Force Garmin sync (all 30 days)' : 'Sync Garmin data'}
        title="Click to sync · Shift+click to force-refresh all 30 days"
        className={`relative flex h-8 w-8 shrink-0 items-center justify-center transition-all ${colorClass[syncState]}`}
      >
        {/* Pulsing halo while syncing */}
        {syncState === 'syncing' && (
          <span className="absolute inset-0 animate-ping rounded-full bg-zinc-500/15" />
        )}

        {syncState === 'idle'    && <SyncIcon  className={iconClass} />}
        {syncState === 'syncing' && <SyncIcon  className={`${iconClass} animate-spin`} />}
        {syncState === 'success' && <CheckIcon className={iconClass} />}
        {syncState === 'error'   && <XIcon     className={iconClass} />}
      </button>

      {/* Persistent tooltip after success/error */}
      {tooltip && (
        <div className="absolute bottom-full right-0 z-10 mb-2 w-max max-w-[240px] border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 font-mono text-[0.6875rem] leading-relaxed tracking-widest text-zinc-300 shadow-xl">
          {tooltip}
        </div>
      )}

      {/* Hover hint when idle */}
      {syncState === 'idle' && !tooltip && (
        <div className="pointer-events-none absolute bottom-full right-0 z-10 mb-2 hidden w-max border border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-[0.6875rem] tracking-widest text-zinc-500 shadow-lg group-hover:block">
          {hoverLabel}
          <span className="ml-2 text-zinc-700">SHIFT=FORCE</span>
        </div>
      )}
    </div>
  );
}

// ── Chat input ────────────────────────────────────────────────────────────────

const MAX_TEXTAREA_HEIGHT = 200; // px (beyond this the box scrolls instead of growing)

export default function ChatInput({ onSubmit, disabled = false }: ChatInputProps) {
  const [value, setValue] = useState('');
  const [refs, setRefs] = useState<HealthReference[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-grow up to MAX_TEXTAREA_HEIGHT, then scroll
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [value]);

  // ── Click-to-Reply: receive a journal question and pre-fill the textarea ──────
  useEffect(() => {
    return bus.on('INITIATE_JOURNAL_REPLY', ({ question }) => {
      setValue(`> Answering: ${question}\n\n`);
      // Defer focus so the textarea has time to paint with the new value
      setTimeout(() => textareaRef.current?.focus(), 0);
    });
  }, []);

  useEffect(() => {
    const handleSuggest = (e: Event) => {
      const text = (e as CustomEvent<string>).detail;
      setValue(text);
      textareaRef.current?.focus();
    };
    const handleReference = (e: Event) => {
      const ref = (e as CustomEvent<HealthReference>).detail;
      setRefs((prev) => {
        // Deduplicate by label+value
        if (prev.some((r) => r.label === ref.label && r.value === ref.value)) return prev;
        return [...prev, ref];
      });
      textareaRef.current?.focus();
    };
    window.addEventListener('health:suggest', handleSuggest);
    window.addEventListener('health:reference', handleReference);
    return () => {
      window.removeEventListener('health:suggest', handleSuggest);
      window.removeEventListener('health:reference', handleReference);
    };
  }, []);

  const handleSubmit = () => {
    const trimmed = value.trim();
    if ((!trimmed && refs.length === 0) || disabled) return;
    const refLine = refs.map((r) => `[${r.label}: ${r.value}]`).join(' ');
    const fullText = refLine && trimmed ? `${refLine}\n${trimmed}` : refLine || trimmed;
    onSubmit(fullText);
    setValue('');
    setRefs([]);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const removeRef = (i: number) => setRefs((prev) => prev.filter((_, j) => j !== i));

  const canSend = !disabled && (!!value.trim() || refs.length > 0);

  return (
    <div className="flex flex-col gap-2">
      {/* Reference chips */}
      {refs.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {refs.map((ref, i) => (
            <span
              key={i}
              className="flex items-center gap-1.5 border border-cyan-500/40 bg-cyan-500/[0.07] px-2.5 py-1 font-mono text-[0.6875rem] tracking-widest shadow-[0_0_8px_rgba(34,211,238,0.15)] ring-1 ring-cyan-500/10"
            >
              <span className="text-zinc-400">{ref.label}:</span>
              <span className="font-semibold text-cyan-300">{ref.value}</span>
              <button
                onClick={() => removeRef(i)}
                className="ml-0.5 text-zinc-600 transition-colors hover:text-rose-400"
                aria-label="Remove reference"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <textarea
        ref={textareaRef}
        rows={2}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        placeholder={refs.length > 0 ? 'Add a question, or just send the reference…' : 'Log vitals, labs, supplements, or a journal entry…'}
        style={{ maxHeight: MAX_TEXTAREA_HEIGHT }}
        className="w-full resize-none overflow-y-auto border border-zinc-700 bg-zinc-900 px-4 py-3 text-sm text-zinc-100 placeholder-zinc-500 shadow-[inset_0_2px_8px_rgba(0,0,0,0.5)] transition-colors focus:border-cyan-500/60 focus:outline-none focus:ring-1 focus:ring-cyan-500/20 disabled:opacity-50"
      />
      <div className="flex items-center justify-between">
        <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-700">
          ↵ SEND · SHIFT+↵ NEWLINE
        </span>
        <div className="flex items-center gap-1">
          <GarminSyncButton />
          <button
            onClick={handleSubmit}
            disabled={!canSend}
            className="h-8 shrink-0 bg-cyan-600 px-4 font-mono text-xs font-semibold tracking-widest text-white transition-colors hover:bg-cyan-500 hover:shadow-[0_0_10px_rgba(34,211,238,0.25)] disabled:opacity-40"
          >
            {disabled ? '···' : 'SEND'}
          </button>
        </div>
      </div>
    </div>
  );
}
