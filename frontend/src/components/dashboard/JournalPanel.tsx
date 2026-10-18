'use client';

import { useMemo, useState } from 'react';
import { deleteEntry } from '@/lib/api';
import type { DailyJournal, Mood } from '@/lib/types';
import { ActiveTrackingBoard } from '@/components/journal/ActiveTrackingBoard';
import { ActiveInquiriesBoard } from '@/components/journal/ActiveInquiriesBoard';

// ── Bracket tag parser ────────────────────────────────────────────────────────
// The LLM sometimes annotates journal notes with [Tag text] prefixes. Strip
// them from the prose body and surface them as chips instead.

interface ParsedNotes {
  tags: string[];   // bracket-extracted labels, e.g. ["Morning Protocol", "HRV weekly avg: 51 ms"]
  body: string;     // prose with bracket annotations removed and whitespace normalized
}

function parseNotesWithTags(raw: string): ParsedNotes {
  const tags: string[] = [];
  // Match leading [Tag] blocks anywhere in the string, capturing their contents.
  const body = raw.replace(/\[([^\]]+)\]/g, (_match, inner: string) => {
    const trimmed = inner.trim();
    if (trimmed) tags.push(trimmed);
    return '';
  }).replace(/\s{2,}/g, ' ').trim();
  return { tags, body };
}

interface JournalPanelProps {
  journals: DailyJournal[];
  isLoading: boolean;
  onRefresh: () => void;
  refreshKey?: number;
}

// ── Schema coercion ───────────────────────────────────────────────────────────

function parseSymptoms(raw: string): string[] {
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function parseSleep(raw: string | null): number | null {
  if (raw === null || raw === '') return null;
  const n = Number(raw);
  return isNaN(n) ? null : n;
}

// ── Mood config ───────────────────────────────────────────────────────────────

const MOOD_CONFIG: Record<
  Mood,
  { label: string; score: number; bar: string; text: string; bg: string }
> = {
  very_low:  { label: 'Very Low',  score: 1, bar: 'bg-rose-500',    text: 'text-rose-400',    bg: 'bg-rose-500/[0.04]' },
  low:       { label: 'Low',       score: 2, bar: 'bg-amber-500',   text: 'text-amber-400',   bg: 'bg-amber-500/[0.04]' },
  neutral:   { label: 'Neutral',   score: 3, bar: 'bg-yellow-400',  text: 'text-yellow-400',  bg: 'bg-yellow-400/[0.03]' },
  good:      { label: 'Good',      score: 4, bar: 'bg-emerald-500', text: 'text-emerald-400', bg: 'bg-emerald-400/[0.04]' },
  very_good: { label: 'Very Good', score: 5, bar: 'bg-cyan-400',    text: 'text-cyan-400',    bg: 'bg-cyan-400/[0.04]' },
};

// ── Energy helpers ────────────────────────────────────────────────────────────

function energyColor(score: number): string {
  if (score <= 3) return 'text-rose-400';
  if (score <= 5) return 'text-amber-400';
  if (score <= 7) return 'text-yellow-400';
  return 'text-emerald-400';
}

function energyBarColor(score: number): string {
  if (score <= 3) return 'bg-rose-500';
  if (score <= 5) return 'bg-amber-500';
  if (score <= 7) return 'bg-yellow-400';
  return 'bg-emerald-400';
}

// ── Date helpers ──────────────────────────────────────────────────────────────

function formatCardDate(iso: string): { primary: string; secondary: string } {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);

  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();

  const secondary = d.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: d.getFullYear() !== today.getFullYear() ? 'numeric' : undefined,
  });

  if (sameDay(d, today)) return { primary: 'TODAY', secondary };
  if (sameDay(d, yesterday)) return { primary: 'YESTERDAY', secondary };
  return {
    primary: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }).toUpperCase(),
    secondary,
  };
}

// ── Derived journal entry ─────────────────────────────────────────────────────

interface JournalEntry {
  raw: DailyJournal;
  symptoms: string[];
  sleepHours: number | null;
}

function enrichJournals(journals: DailyJournal[]): JournalEntry[] {
  return [...journals]
    .sort(
      (a, b) =>
        new Date(b.journal_date).getTime() - new Date(a.journal_date).getTime(),
    )
    .map((j) => ({
      raw: j,
      symptoms: parseSymptoms(j.symptoms),
      sleepHours: parseSleep(j.sleep_hours),
    }));
}

// ── Sub-components ────────────────────────────────────────────────────────────

function MoodBar({ mood }: { mood: Mood }) {
  const cfg = MOOD_CONFIG[mood];
  return (
    <div className="flex items-center gap-2">
      <div className="flex gap-0.5">
        {([1, 2, 3, 4, 5] as const).map((i) => (
          <div
            key={i}
            className={`h-2 w-2 ${i <= cfg.score ? cfg.bar : 'bg-white/[0.06]'}`}
          />
        ))}
      </div>
      <span className={`font-mono text-[0.75rem] font-semibold tracking-wide ${cfg.text}`}>
        {cfg.label}
      </span>
    </div>
  );
}

function EnergyBar({ score }: { score: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1 w-24 overflow-hidden bg-white/[0.06]">
        <div
          className={`h-full transition-all ${energyBarColor(score)}`}
          style={{ width: `${score * 10}%` }}
        />
      </div>
      <span className={`font-mono text-xs font-semibold tabular-nums ${energyColor(score)}`}>
        {score}/10
      </span>
    </div>
  );
}

function SymptomChips({ symptoms }: { symptoms: string[] }) {
  if (symptoms.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {symptoms.map((s, i) => (
        <span
          key={i}
          className="border border-amber-500/20 bg-amber-500/[0.06] px-2 py-0.5 font-mono text-[0.6875rem] tracking-wide text-amber-400"
        >
          {s}
        </span>
      ))}
    </div>
  );
}

// ── Tag chips ─────────────────────────────────────────────────────────────────

function TagChips({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {tags.map((t, i) => (
        <span
          key={i}
          className="border border-cyan-500/20 bg-cyan-500/[0.06] px-2 py-0.5 font-mono text-[0.6875rem] tracking-wide text-cyan-400"
        >
          {t}
        </span>
      ))}
    </div>
  );
}

function JournalCard({ entry, onRefresh }: { entry: JournalEntry; onRefresh: () => void }) {
  const { raw, symptoms, sleepHours } = entry;
  const { primary, secondary } = formatCardDate(raw.journal_date);
  const hasMeta = raw.mood || raw.energy_score !== null || sleepHours !== null;
  const moodCfg = raw.mood ? MOOD_CONFIG[raw.mood] : null;
  const [deleting, setDeleting] = useState(false);

  // Split multi-day segments (appended via the ---  divider) and parse each one.
  const segments = useMemo(() =>
    (raw.notes ?? '').split(/\n\n---\n\n/).map((seg) => parseNotesWithTags(seg.trim())).filter((s) => s.body),
    [raw.notes]
  );

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteEntry('journal', raw.id);
      onRefresh();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <article
      className={`group flex flex-col gap-4 border border-zinc-800 bg-zinc-900 p-5 ${moodCfg?.bg ?? ''}`}
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-mono text-sm font-bold text-zinc-100">{primary}</p>
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">{secondary}</p>
        </div>

        <div className="flex items-start gap-4">
          {hasMeta && (
            <div className="flex flex-col gap-2">
              {raw.mood && <MoodBar mood={raw.mood} />}
              {raw.energy_score !== null && <EnergyBar score={raw.energy_score} />}
              {sleepHours !== null && (
                <div className="flex items-center gap-1.5">
                  <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">SLEEP</span>
                  <span className="font-mono text-xs font-semibold tabular-nums text-zinc-300">
                    {sleepHours}h
                  </span>
                </div>
              )}
            </div>
          )}
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="opacity-0 group-hover:opacity-100 transition-opacity font-mono text-sm text-zinc-500 hover:text-rose-400 disabled:text-zinc-600 mt-0.5"
            title="Delete this journal entry"
          >
            {deleting ? '…' : '×'}
          </button>
        </div>
      </div>

      {/* Notes: rendered per segment, each with its own tag chips */}
      <div className="flex flex-col gap-4">
        {segments.map((seg, i) => (
          <div key={i} className={`flex flex-col gap-2 ${i > 0 ? 'border-t border-white/[0.05] pt-4' : ''}`}>
            {seg.tags.length > 0 && <TagChips tags={seg.tags} />}
            <p className="text-sm leading-relaxed text-zinc-300 whitespace-pre-wrap">{seg.body}</p>
          </div>
        ))}
      </div>

      {/* Symptoms */}
      <SymptomChips symptoms={symptoms} />

      {/* Training / nutrition */}
      {(raw.training || raw.nutrition) && (
        <div className="flex flex-col gap-1 border-t border-white/[0.06] pt-3">
          {raw.training && (
            <div className="flex gap-2">
              <span className="w-16 flex-none font-mono text-[0.6875rem] tracking-widest text-zinc-400">
                TRAINING
              </span>
              <span className="text-xs text-zinc-400">{raw.training}</span>
            </div>
          )}
          {raw.nutrition && (
            <div className="flex gap-2">
              <span className="w-16 flex-none font-mono text-[0.6875rem] tracking-widest text-zinc-400">
                NUTRITION
              </span>
              <span className="text-xs text-zinc-400">{raw.nutrition}</span>
            </div>
          )}
        </div>
      )}
    </article>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

type MoodFilter = Mood | 'all';

export default function JournalPanel({ journals, isLoading, onRefresh, refreshKey }: JournalPanelProps) {
  const [moodFilter, setMoodFilter] = useState<MoodFilter>('all');

  const entries = useMemo(() => enrichJournals(journals), [journals]);

  const filtered = useMemo(() => {
    if (moodFilter === 'all') return entries;
    return entries.filter((e) => e.raw.mood === moodFilter);
  }, [entries, moodFilter]);

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center font-mono text-xs tracking-widest text-zinc-500">
        LOADING JOURNAL ENTRIES…
      </div>
    );
  }

  if (journals.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-7 py-20">
        {/* Boards still render in the empty state so the user sees today's objectives */}
        <div className="w-full flex flex-col gap-4">
          <ActiveTrackingBoard refreshKey={refreshKey} />
          <ActiveInquiriesBoard />
        </div>
        <h1 className="font-mono text-lg font-semibold tracking-wide text-zinc-200">
          No Entries Yet
        </h1>
        <div className="flex h-14 w-14 items-center justify-center bg-zinc-800 text-zinc-500">
          <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 0 0 6 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 0 1 6 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 0 1 6-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0 0 18 18a8.967 8.967 0 0 0-6 2.292m0-14.25v14.25" />
          </svg>
        </div>
        <div className="flex flex-col items-center gap-1.5">
          <p className="font-mono text-xs font-semibold tracking-widest text-zinc-300">NO JOURNAL ENTRIES</p>
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">Log a daily check-in to begin tracking trends</p>
        </div>
        <div className="flex flex-wrap justify-center gap-2 pt-1">
          {["How am I feeling today?", "Log today's energy", "Note any symptoms"].map((chip) => (
            <button
              key={chip}
              onClick={() => window.dispatchEvent(new CustomEvent('health:suggest', { detail: chip }))}
              className="border border-zinc-700 bg-zinc-800 px-4 py-1.5 font-mono text-[0.6875rem] tracking-widest text-zinc-400 transition-colors hover:border-cyan-500/50 hover:bg-zinc-700/80 hover:text-zinc-200"
            >
              {chip}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Active PK clearance board: renders nothing when empty */}
      <ActiveTrackingBoard />

      {/* Active clinical inquiries: renders empty state when synthesis hasn't run */}
      <ActiveInquiriesBoard />

      {/* Mood filter + entry count */}
      <div className="flex items-center justify-between">
        <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">
          {filtered.length} OF {journals.length} ENTRIES
        </p>
        <select
          value={moodFilter}
          onChange={(e) => setMoodFilter(e.target.value as MoodFilter)}
          className="border border-zinc-700 bg-zinc-800 px-3 py-1.5 font-mono text-[0.75rem] tracking-widest text-zinc-300 transition-colors focus:border-cyan-400/50 focus:outline-none"
        >
          <option value="all" className="bg-black">ALL MOODS</option>
          {(Object.entries(MOOD_CONFIG) as [Mood, (typeof MOOD_CONFIG)[Mood]][]).map(
            ([key, cfg]) => (
              <option key={key} value={key} className="bg-black">
                {cfg.label.toUpperCase()}
              </option>
            ),
          )}
        </select>
      </div>

      {/* Entry feed */}
      {filtered.length === 0 ? (
        <p className="py-10 text-center font-mono text-xs tracking-widest text-zinc-500">
          NO ENTRIES MATCH CURRENT FILTER
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {filtered.map((entry) => (
            <li key={entry.raw.id}>
              <JournalCard entry={entry} onRefresh={onRefresh} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
