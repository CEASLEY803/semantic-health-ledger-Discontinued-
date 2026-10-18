'use client';

import { useEffect, useMemo, useState } from 'react';
import RefRangeBar from '@/components/ui/RefRangeBar';
import StatusBadge from '@/components/ui/StatusBadge';
import { deleteEntry } from '@/lib/api';
import type { LabResult } from '@/lib/types';

// ── Types ─────────────────────────────────────────────────────────────────────

type BadgeStatus = 'flagged' | 'high' | 'low' | 'normal' | 'no-range';

interface LabsPanelProps {
  labs: LabResult[];
  isLoading: boolean;
  onRefresh: () => void;
  /** When set, overrides the internal search box (used by the AI navigate_ui tool). */
  externalSearch?: string;
}

type ProcessedLab = LabResult & {
  _value: number | null;
  _low:   number | null;
  _high:  number | null;
  _status: BadgeStatus;
};

type LabGroup = {
  dateKey:      string;   // YYYY-MM-DD
  displayDate:  string;   // e.g. "May 19, 2026"
  labs:         ProcessedLab[];
  flaggedCount: number;
  hasPanels:    boolean;
  hasRefRanges: boolean;
};

type SortField = 'marker_name' | 'status';
type SortDir   = 'asc' | 'desc';

const STATUS_ORDER: Record<BadgeStatus, number> = {
  flagged: 0, high: 1, low: 2, normal: 3, 'no-range': 4,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseNum(v: string | number | null | undefined): number | null {
  if (v == null || v === '') return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

function deriveStatus(lab: LabResult): BadgeStatus {
  if (lab.flagged) return 'flagged';
  const value = parseNum(lab.value_numeric);
  const low   = parseNum(lab.reference_low);
  const high  = parseNum(lab.reference_high);
  if (value === null) return 'no-range';
  if (low === null && high === null) return 'no-range';
  if (high !== null && value > high) return 'high';
  if (low  !== null && value < low)  return 'low';
  return 'normal';
}

function formatGroupDate(dateKey: string): string {
  // Use noon to prevent timezone shifts on date-only strings
  return new Date(dateKey + 'T12:00:00').toLocaleDateString(undefined, {
    month: 'long', day: 'numeric', year: 'numeric',
  });
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Th({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <th
      className="cursor-pointer select-none px-4 py-2 text-left font-mono text-[0.6875rem] tracking-wide text-zinc-400 transition-colors hover:text-cyan-400"
      onClick={onClick}
    >
      {label}
    </th>
  );
}

function Stat({
  label, value, alert = false,
}: { label: string; value: number; alert?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">{label}</span>
      <span className={`font-mono text-xl font-bold leading-none tabular-nums ${alert ? 'text-rose-500' : 'text-white'}`}>
        {value}
      </span>
    </div>
  );
}

function LabRow({
  lab, onRefresh, hasPanels, hasRefRanges,
}: {
  lab: ProcessedLab;
  onRefresh: () => void;
  hasPanels: boolean;
  hasRefRanges: boolean;
}) {
  const [deleting, setDeleting] = useState(false);

  const refRange =
    lab._low !== null && lab._high !== null ? `${lab._low} – ${lab._high}` :
    lab._low  !== null                      ? `> ${lab._low}` :
    lab._high !== null                      ? `< ${lab._high}` :
    '--';

  const displayValue =
    lab._value !== null
      ? `${lab._value}${lab.unit ? ` ${lab.unit}` : ''}`
      : (lab.value_text ?? '--');

  const isAbnormal = lab._status === 'flagged' || lab._status === 'high' || lab._status === 'low';

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleting(true);
    try {
      await deleteEntry('lab', lab.id);
      onRefresh();
    } finally {
      setDeleting(false);
    }
  };

  const handleReference = () => {
    const dateStr = lab.collected_at.slice(0, 10);
    const displayDate = new Date(dateStr + 'T12:00:00').toLocaleDateString(undefined, {
      month: 'short', day: 'numeric',
    });
    window.dispatchEvent(
      new CustomEvent('health:reference', {
        detail: { label: `${displayDate} · ${lab.marker_name}`, value: displayValue },
      }),
    );
  };

  return (
    <tr
      onClick={handleReference}
      className={`group cursor-pointer transition-colors hover:bg-cyan-500/[0.04] active:bg-cyan-500/[0.08] ${isAbnormal ? 'bg-rose-500/[0.04]' : ''}`}
      title={`Pin to chat: ${lab.marker_name}`}
    >
      <td className="px-4 py-3">
        <StatusBadge status={lab._status} />
      </td>
      <td className="px-4 py-3 font-mono text-sm font-medium text-zinc-200">
        {lab.marker_name}
      </td>
      {hasPanels && (
        <td className="px-4 py-3 font-mono text-xs text-zinc-400">
          {lab.panel_name ?? '--'}
        </td>
      )}
      <td className="px-4 py-3 text-right font-mono text-sm tabular-nums text-white">
        {displayValue}
      </td>
      {hasRefRanges && (
        <>
          <td className="px-4 py-3 text-center font-mono text-xs tabular-nums text-zinc-400">
            {refRange}
          </td>
          <td className="px-4 py-3">
            {lab._value !== null ? (
              <RefRangeBar value={lab._value} low={lab._low} high={lab._high} />
            ) : (
              <span className="font-mono text-xs text-zinc-700">--</span>
            )}
          </td>
        </>
      )}
      <td className="px-2 py-3 text-center">
        <button
          onClick={handleDelete}
          disabled={deleting}
          className="opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.75rem] text-zinc-500 hover:text-rose-400 disabled:text-zinc-600"
          title="Delete this result"
        >
          {deleting ? '…' : '×'}
        </button>
      </td>
    </tr>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function LabsPanel({ labs, isLoading, onRefresh, externalSearch }: LabsPanelProps) {
  const [sortField, setSortField] = useState<SortField>('status');
  const [sortDir,   setSortDir]   = useState<SortDir>('asc');
  const [searchQuery, setSearchQuery] = useState('');

  // When the AI calls navigate_ui with a search_query, sync it into the local
  // search box so the panel filters immediately without user interaction.
  useEffect(() => {
    if (externalSearch !== undefined) setSearchQuery(externalSearch);
  }, [externalSearch]);

  // Per-group toggle state. Groups not in the map fall back to the default:
  // index 0 (most recent) is open, older collections are collapsed.
  const [userToggles, setUserToggles] = useState<Map<string, boolean>>(new Map());

  const processed = useMemo<ProcessedLab[]>(() =>
    labs.map((lab) => ({
      ...lab,
      _value:  parseNum(lab.value_numeric),
      _low:    parseNum(lab.reference_low),
      _high:   parseNum(lab.reference_high),
      _status: deriveStatus(lab),
    })),
    [labs],
  );

  const sorted = useMemo<ProcessedLab[]>(() =>
    [...processed].sort((a, b) => {
      const cmp = sortField === 'marker_name'
        ? a.marker_name.localeCompare(b.marker_name)
        : STATUS_ORDER[a._status] - STATUS_ORDER[b._status];
      return sortDir === 'asc' ? cmp : -cmp;
    }),
    [processed, sortField, sortDir],
  );

  const filtered = useMemo<ProcessedLab[]>(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return sorted;
    return sorted.filter(
      (l) =>
        l.marker_name.toLowerCase().includes(q) ||
        (l.panel_name?.toLowerCase().includes(q) ?? false),
    );
  }, [sorted, searchQuery]);

  const groups = useMemo<LabGroup[]>(() => {
    const map = new Map<string, ProcessedLab[]>();
    for (const lab of filtered) {
      const dk = lab.collected_at.slice(0, 10);
      if (!map.has(dk)) map.set(dk, []);
      map.get(dk)!.push(lab);
    }
    return [...map.entries()]
      .sort(([a], [b]) => b.localeCompare(a))   // newest date first
      .map(([dateKey, groupLabs]) => ({
        dateKey,
        displayDate:  formatGroupDate(dateKey),
        labs:         groupLabs,
        flaggedCount: groupLabs.filter(
          (l) => l._status === 'flagged' || l._status === 'high' || l._status === 'low',
        ).length,
        hasPanels:    groupLabs.some((l) => !!l.panel_name),
        hasRefRanges: groupLabs.some((l) => l._low !== null || l._high !== null),
      }));
  }, [sorted]);

  // Banner stats: always derived from the most recent collection, unaffected by search/sort.
  const latestCollectionStats = useMemo(() => {
    if (processed.length === 0) return null;
    const allDates = [...new Set(processed.map((l) => l.collected_at.slice(0, 10)))]
      .sort()
      .reverse();
    const latestDate = allDates[0];
    const latestLabs = processed.filter((l) => l.collected_at.startsWith(latestDate));
    const flagged = latestLabs.filter(
      (l) => l._status === 'flagged' || l._status === 'high' || l._status === 'low',
    ).length;
    return {
      date:             formatGroupDate(latestDate),
      markers:          latestLabs.length,
      flagged,
      totalCollections: allDates.length,
    };
  }, [processed]);

  const isExpanded = (dateKey: string, idx: number): boolean => {
    if (userToggles.has(dateKey)) return userToggles.get(dateKey)!;
    return idx === 0; // default: most recent open, older ones collapsed
  };

  const toggleGroup = (dateKey: string, currentlyExpanded: boolean) => {
    setUserToggles((prev) => new Map(prev).set(dateKey, !currentlyExpanded));
  };

  const handleSort = (field: SortField) => {
    if (sortField === field) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortField(field); setSortDir('asc'); }
  };

  const sortIndicator = (field: SortField) =>
    sortField === field ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '';

  // ── Empty / loading states ────────────────────────────────────────────────

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center font-mono text-xs tracking-widest text-zinc-500">
        LOADING BIOMARKER DATA…
      </div>
    );
  }

  if (labs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-7 py-20">
        <h1 className="font-mono text-lg font-semibold tracking-wide text-zinc-200">
          Awaiting Telemetry
        </h1>
        <div className="flex h-14 w-14 items-center justify-center bg-zinc-800 text-zinc-500">
          <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15M14.25 3.104c.251.023.501.05.75.082M19.8 15a2.25 2.25 0 0 1 .45 2.179l-.7 3.5a2.25 2.25 0 0 1-2.2 1.821H6.65a2.25 2.25 0 0 1-2.2-1.821l-.7-3.5a2.25 2.25 0 0 1 .45-2.179m13.6 0H4.2" />
          </svg>
        </div>
        <div className="flex flex-col items-center gap-1.5">
          <p className="font-mono text-xs font-semibold tracking-widest text-zinc-300">NO LAB RESULTS</p>
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">Ingest a lab report to populate this panel</p>
        </div>
        <div className="flex flex-wrap justify-center gap-2 pt-1">
          {['Log morning vitals', 'Ingest lab report', 'Record journal entry'].map((chip) => (
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

  // ── Main render ───────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-3">

      {/* Summary bar: scoped to the most recent collection */}
      <div className="flex flex-wrap items-center justify-between gap-3 border border-zinc-800 bg-zinc-900 px-5 py-3">
        <div className="flex flex-wrap gap-10">
          {latestCollectionStats && (
            <>
              <div className="flex flex-col gap-0.5">
                <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">LATEST COLLECTION</span>
                <span className="font-mono text-sm font-bold leading-none text-white">
                  {latestCollectionStats.date.toUpperCase()}
                </span>
              </div>
              <Stat label="MARKERS"      value={latestCollectionStats.markers} />
              <Stat
                label="OUT OF RANGE"
                value={latestCollectionStats.flagged}
                alert={latestCollectionStats.flagged > 0}
              />
              <Stat label="COLLECTIONS"  value={latestCollectionStats.totalCollections} />
            </>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Search */}
          <div className="flex items-center gap-1.5 border border-zinc-700 bg-zinc-800/60 px-3 py-1.5">
            <svg className="h-3 w-3 shrink-0 text-zinc-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
            </svg>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="SEARCH MARKERS…"
              className="w-32 bg-transparent font-mono text-[0.6875rem] tracking-widest text-zinc-300 placeholder-zinc-600 focus:outline-none"
            />
            {searchQuery && (
              <button onClick={() => setSearchQuery('')} className="text-zinc-600 transition-colors hover:text-zinc-400">×</button>
            )}
          </div>

          {/* Sort controls */}
          <div className="flex items-center gap-1.5">
            <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">SORT</span>
            {(['status', 'marker_name'] as SortField[]).map((f) => (
              <button
                key={f}
                onClick={() => handleSort(f)}
                className={`border px-2.5 py-1 font-mono text-[0.6875rem] tracking-widest transition-colors ${
                  sortField === f
                    ? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-400'
                    : 'border-zinc-700 text-zinc-500 hover:border-zinc-600 hover:text-zinc-300'
                }`}
              >
                {f === 'status' ? `Status${sortIndicator('status')}` : `Marker${sortIndicator('marker_name')}`}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Search result count */}
      {searchQuery.trim() && (
        <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
          {filtered.length} MARKER{filtered.length !== 1 ? 'S' : ''} MATCHING &ldquo;{searchQuery.trim()}&rdquo;
        </p>
      )}

      {/* Date-grouped collections */}
      <div className="flex flex-col gap-2">
        {groups.map((group, idx) => {
          const expanded = isExpanded(group.dateKey, idx);
          return (
            <div key={group.dateKey} className="border border-white/10">

              {/* Collection header: click to expand / collapse */}
              <button
                className="flex w-full items-center gap-4 bg-zinc-900 px-5 py-3 text-left transition-colors hover:bg-zinc-800/70"
                onClick={() => toggleGroup(group.dateKey, expanded)}
              >
                <span className="w-2.5 font-mono text-[0.625rem] text-zinc-600">
                  {expanded ? '▼' : '▶'}
                </span>
                <span className="font-mono text-xs font-semibold tracking-widest text-zinc-200">
                  {group.displayDate.toUpperCase()}
                </span>
                <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
                  {group.labs.length} {group.labs.length === 1 ? 'MARKER' : 'MARKERS'}
                </span>
                {group.flaggedCount > 0 && (
                  <span className="font-mono text-[0.6875rem] tracking-widest text-rose-400/80">
                    {group.flaggedCount} FLAGGED
                  </span>
                )}
                <span className="ml-auto font-mono text-[0.625rem] tracking-wide text-zinc-700">
                  {expanded ? 'Collapse' : 'Expand'}
                </span>
              </button>

              {/* Table: only rendered when expanded */}
              {expanded && (
                <div className="overflow-x-auto border-t border-white/[0.06]">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-zinc-800/80 bg-zinc-900/60">
                        <Th onClick={() => handleSort('status')}      label={`Status${sortIndicator('status')}`} />
                        <Th onClick={() => handleSort('marker_name')} label={`Marker${sortIndicator('marker_name')}`} />
                        {group.hasPanels && (
                          <th className="px-4 py-2 text-left font-mono text-[0.6875rem] tracking-wide text-zinc-400">
                            Panel
                          </th>
                        )}
                        <th className="px-4 py-2 text-right font-mono text-[0.6875rem] tracking-wide text-zinc-400">
                          Value
                        </th>
                        {group.hasRefRanges && (
                          <>
                            <th className="px-4 py-2 text-center font-mono text-[0.6875rem] tracking-wide text-zinc-400">
                              Ref range
                            </th>
                            <th className="min-w-[160px] px-4 py-2 font-mono text-[0.6875rem] tracking-wide text-zinc-400">
                              Distribution
                            </th>
                          </>
                        )}
                        <th className="w-8 px-2 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04]">
                      {group.labs.map((lab) => (
                        <LabRow
                          key={lab.id}
                          lab={lab}
                          onRefresh={onRefresh}
                          hasPanels={group.hasPanels}
                          hasRefRanges={group.hasRefRanges}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
