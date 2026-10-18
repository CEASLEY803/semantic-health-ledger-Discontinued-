'use client';

import { useState } from 'react';
import { deleteEntry, deleteRegimenItem } from '@/lib/api';
import type { AdministrationRoute, SupplementLog, RegimenItem } from '@/lib/types';

interface SupplementsPanelProps {
  regimen: RegimenItem[];
  supplementLogs: SupplementLog[];
  isLoading: boolean;
  onRefresh: () => void;
}

// ── Route badge ───────────────────────────────────────────────────────────────

const ROUTE_ABBR: Record<AdministrationRoute, string> = {
  oral:          'PO',
  sublingual:    'SL',
  transdermal:   'TD',
  intranasal:    'IN',
  other:         'OTHER',
};

const ROUTE_COLOR: Record<AdministrationRoute, string> = {
  oral:          'bg-emerald-400/10 text-emerald-400 ring-emerald-400/30',
  sublingual:    'bg-cyan-400/10 text-cyan-400 ring-cyan-400/30',
  transdermal:   'bg-amber-400/10 text-amber-400 ring-amber-400/30',
  intranasal:    'bg-sky-400/10 text-sky-400 ring-sky-400/30',
  other:         'bg-zinc-800/60 text-zinc-400 ring-zinc-700/50',
};

function RouteBadge({ route }: { route: AdministrationRoute }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 font-mono text-[0.6875rem] font-bold tracking-widest ring-1 ring-inset ${ROUTE_COLOR[route] ?? ROUTE_COLOR.other}`}>
      {ROUTE_ABBR[route] ?? route.toUpperCase()}
    </span>
  );
}

// ── Frequency / days label ────────────────────────────────────────────────────

function formatSchedule(item: RegimenItem): string {
  if (item.frequency === 'daily')       return 'DAILY';
  if (item.frequency === 'twice_daily') return '2×/DAY';
  if (item.frequency === 'biweekly')    return 'BIWEEKLY';
  if (item.frequency === 'monthly')     return 'MONTHLY';
  if (item.frequency === 'as_needed')   return 'AS NEEDED';
  if (item.frequency === 'weekly' && item.days_of_week) {
    return item.days_of_week
      .split(',')
      .map((d) => d.trim().slice(0, 3).toUpperCase())
      .join('/');
  }
  return item.frequency.replace(/_/g, ' ').toUpperCase();
}

// ── Time-of-day grouping ──────────────────────────────────────────────────────

const TIME_ORDER = ['morning', 'midday', 'afternoon', 'evening', 'night', 'as_needed'] as const;

const TIME_LABELS: Record<string, string> = {
  morning:   'MORNING',
  midday:    'MIDDAY',
  afternoon: 'AFTERNOON',
  evening:   'EVENING',
  night:     'NIGHT',
  as_needed: 'AS NEEDED',
};

function groupByTime(items: RegimenItem[]): { key: string; label: string; items: RegimenItem[] }[] {
  const map = new Map<string, RegimenItem[]>();
  for (const item of items) {
    const key = item.time_of_day;
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(item);
  }
  // Ordered known keys first, then any unknown time_of_day values
  const orderedKeys = [
    ...TIME_ORDER.filter((k) => map.has(k)),
    ...[...map.keys()].filter((k) => !TIME_ORDER.includes(k as typeof TIME_ORDER[number])),
  ];
  return orderedKeys.map((key) => ({
    key,
    label: TIME_LABELS[key] ?? key.toUpperCase(),
    items: map.get(key)!,
  }));
}

// ── Single regimen row ────────────────────────────────────────────────────────

function RegimenRow({ item, onRefresh }: { item: RegimenItem; onRefresh: () => void }) {
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleting(true);
    try {
      await deleteRegimenItem(item.id);
      onRefresh();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="group flex items-center gap-3 border-b border-zinc-800/60 px-4 py-3 last:border-0 hover:bg-white/[0.02] transition-colors">
      {/* Supplement name */}
      <span className="min-w-0 flex-1 font-mono text-[0.875rem] font-semibold text-zinc-100 truncate">
        {item.supplement_name}
      </span>

      {/* Dose */}
      <span className="shrink-0 font-mono text-[0.875rem] font-bold tabular-nums text-white">
        {item.dose_value}&thinsp;<span className="text-zinc-400 font-normal">{item.dose_unit}</span>
      </span>

      {/* Route badge */}
      <RouteBadge route={item.route} />

      {/* Schedule */}
      <span className="shrink-0 font-mono text-[0.6875rem] tracking-widest text-zinc-500 min-w-[60px] text-right">
        {formatSchedule(item)}
      </span>

      {/* Site (if present) */}
      {item.site && (
        <span className="shrink-0 font-mono text-[0.6875rem] text-zinc-600">
          {item.site}
        </span>
      )}

      {/* Delete */}
      <button
        onClick={handleDelete}
        disabled={deleting}
        className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.75rem] text-zinc-600 hover:text-rose-400 disabled:text-zinc-700"
        title="Remove from regimen"
      >
        {deleting ? '…' : '×'}
      </button>
    </div>
  );
}

// ── Dose log row ──────────────────────────────────────────────────────────────

function DoseLogRow({ log, onRefresh }: { log: SupplementLog; onRefresh: () => void }) {
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleting(true);
    try {
      await deleteEntry('supplement', log.id);
      onRefresh();
    } finally {
      setDeleting(false);
    }
  };

  const date = log.recorded_at.slice(0, 10);
  const time = log.recorded_at.slice(11, 16);

  return (
    <div className="group flex items-center gap-3 border-b border-zinc-800/60 px-4 py-2.5 last:border-0 hover:bg-white/[0.02] transition-colors">
      {/* Timestamp */}
      <span className="shrink-0 font-mono text-[0.6875rem] text-zinc-600 w-[88px]">
        {date}<span className="text-zinc-700"> {time}</span>
      </span>

      {/* Supplement name */}
      <span className="min-w-0 flex-1 font-mono text-[0.8125rem] font-semibold text-zinc-200 truncate">
        {log.supplement_name}
      </span>

      {/* Dose */}
      <span className="shrink-0 font-mono text-[0.8125rem] tabular-nums text-white">
        {log.dose_value}&thinsp;<span className="text-zinc-500 font-normal text-[0.6875rem]">{log.dose_unit}</span>
      </span>

      {/* Route badge */}
      <RouteBadge route={log.route} />

      {/* Notes */}
      {log.notes && (
        <span className="shrink-0 font-mono text-[0.6875rem] text-zinc-600 max-w-[120px] truncate" title={log.notes}>
          {log.notes}
        </span>
      )}

      {/* Delete */}
      <button
        onClick={handleDelete}
        disabled={deleting}
        className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.75rem] text-zinc-600 hover:text-rose-400 disabled:text-zinc-700"
        title="Delete log entry"
      >
        {deleting ? '…' : '×'}
      </button>
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export default function SupplementsPanel({ regimen, supplementLogs, isLoading, onRefresh }: SupplementsPanelProps) {
  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center font-mono text-xs tracking-widest text-zinc-500">
        LOADING…
      </div>
    );
  }

  const groups = groupByTime(regimen);
  const [logOpen, setLogOpen] = useState(false);

  return (
    <div className="flex flex-col gap-1">
      {/* ── CURRENT STACK ─────────────────────────────────────────────────── */}
      {regimen.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 py-10 border border-zinc-800 bg-zinc-900">
          <p className="font-mono text-xs font-semibold tracking-widest text-zinc-500">NO REGIMEN DEFINED</p>
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600 text-center max-w-[240px]">
            Tell LEDGER what you take and when, for example &ldquo;I take 400mg Magnesium Glycinate every evening&rdquo;
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between border border-zinc-800 bg-zinc-900 px-5 py-3 mb-1">
            <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">CURRENT STACK</span>
            <span className="font-mono text-xl font-bold tabular-nums text-white">{regimen.length}</span>
          </div>
          {groups.map(({ key, label, items }) => (
            <section key={key} className="border border-zinc-800 bg-zinc-900">
              <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-2">
                <span className="font-mono text-[0.6875rem] font-semibold tracking-[0.18em] text-cyan-500/70">{label}</span>
                <div className="flex-1 h-px bg-zinc-800" />
                <span className="font-mono text-[0.6875rem] text-zinc-600">{items.length}</span>
              </div>
              <div>
                {items.map((item) => (
                  <RegimenRow key={item.id} item={item} onRefresh={onRefresh} />
                ))}
              </div>
            </section>
          ))}
        </>
      )}

      {/* ── DOSE LOG ──────────────────────────────────────────────────────── */}
      {supplementLogs.length > 0 && (
        <section className="mt-3 border border-zinc-800 bg-zinc-900">
          <button
            onClick={() => setLogOpen((o) => !o)}
            className="flex w-full items-center gap-3 px-4 py-2 hover:bg-white/[0.02] transition-colors"
          >
            <span className="font-mono text-[0.6875rem] font-semibold tracking-[0.18em] text-zinc-500">DOSE LOG</span>
            <div className="flex-1 h-px bg-zinc-800" />
            <span className="font-mono text-[0.6875rem] text-zinc-600">{supplementLogs.length}</span>
            <span className="font-mono text-[0.625rem] text-zinc-700 ml-1">{logOpen ? '▲' : '▼'}</span>
          </button>
          {logOpen && (
            <div className="border-t border-zinc-800">
              {supplementLogs.map((log) => (
                <DoseLogRow key={log.id} log={log} onRefresh={onRefresh} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
