'use client';

import { useMemo, useState } from 'react';
import { deleteEntry } from '@/lib/api';
import type { BiometricLog } from '@/lib/types';

// ── Metric classification sets ────────────────────────────────────────────────

const SLEEP_METRIC_NAMES = new Set([
  'sleep_duration', 'sleep_deep', 'sleep_light', 'sleep_rem', 'sleep_awake',
  'sleep_spo2_avg', 'sleep_spo2_low', 'sleep_respiration', 'sleep_stress',
  'sleep_start', 'sleep_end',
]);

const WORKOUT_METRICS = new Set(['workout_session', 'workout_duration']);

// Metrics where falling value is an improvement (trend ▼ = green, ▲ = red)
const LOWER_IS_BETTER = new Set([
  'resting_heart_rate',
  'sleep_stress',
  'sleep_awake',
  'body_mass_index_(bmi)',
  'weight',
]);

// ── Display name overrides ────────────────────────────────────────────────────

const METRIC_LABELS: Record<string, string> = {
  'body_battery':          'Body Battery',
  'body_mass_index_(bmi)': 'BMI',
  'hrv_last_night':        'HRV (Last Night)',
  'hrv_weekly_avg':        'HRV (Weekly Avg)',
  'resting_heart_rate':    'Resting HR',
  'sleep_duration':        'Duration',
  'sleep_deep':            'Deep Sleep',
  'sleep_light':           'Light Sleep',
  'sleep_rem':             'REM Sleep',
  'sleep_awake':           'Awake Time',
  'sleep_start':           'Bedtime',
  'sleep_end':             'Wake Time',
  'sleep_spo2_avg':        'SpO₂ Avg',
  'sleep_spo2_low':        'SpO₂ Low',
  'sleep_respiration':     'Respiration Rate',
  'sleep_stress':          'Sleep Stress',
  'weight':                'Weight',
};

function metricLabel(name: string): string {
  return METRIC_LABELS[name] ?? name.replace(/_/g, ' ');
}

// ── Category buckets (order determines render order) ─────────────────────────

type Category = 'sleep' | 'cardiovascular' | 'body' | 'other';

const METRIC_CATEGORY: Record<string, Category> = {
  'body_battery':          'cardiovascular',
  'hrv_last_night':        'cardiovascular',
  'hrv_weekly_avg':        'cardiovascular',
  'resting_heart_rate':    'cardiovascular',
  'body_mass_index_(bmi)': 'body',
  'weight':                'body',
};

function metricCategory(name: string): Category {
  if (SLEEP_METRIC_NAMES.has(name)) return 'sleep';
  return METRIC_CATEGORY[name] ?? 'other';
}

// ── BiometricsPanelProps ──────────────────────────────────────────────────────

interface BiometricsPanelProps {
  biometrics: BiometricLog[];
  isLoading: boolean;
  onRefresh: () => void;
}

// ── Sparkline ─────────────────────────────────────────────────────────────────

interface SparkPoint { ts: number; value: number; label: string }

function Sparkline({ points }: { points: SparkPoint[] }) {
  const W = 300; const H = 56;
  const vals = points.map((p) => p.value);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const range = hi - lo || 1;
  const pad = 4;
  const xs = points.map((_, i) => pad + (i / Math.max(points.length - 1, 1)) * (W - pad * 2));
  const ys = vals.map((v) => H - pad - ((v - lo) / range) * (H - pad * 2));
  const line = xs.map((x, i) => `${i === 0 ? 'M' : 'L'}${x},${ys[i]}`).join(' ');
  const area = `${line} L${xs[xs.length - 1]},${H} L${xs[0]},${H} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-14 w-full" preserveAspectRatio="none">
      <defs>
        <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgb(34,211,238)" stopOpacity="0.25" />
          <stop offset="100%" stopColor="rgb(34,211,238)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#sg)" />
      <path d={line} fill="none" stroke="rgb(34,211,238)" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

// ── MetricGroup ───────────────────────────────────────────────────────────────

interface MetricGroup {
  metric_name: string;
  unit: string;
  points: SparkPoint[];
  entries: BiometricLog[];
  latest: number;
  min: number;
  max: number;
  trend: 'up' | 'down' | 'flat';
  inverted: boolean;
}

function groupByMetric(logs: BiometricLog[]): MetricGroup[] {
  const map = new Map<string, BiometricLog[]>();
  for (const log of logs) {
    if (WORKOUT_METRICS.has(log.metric_name)) continue;
    if (SLEEP_METRIC_NAMES.has(log.metric_name)) continue;
    if (!map.has(log.metric_name)) map.set(log.metric_name, []);
    map.get(log.metric_name)!.push(log);
  }

  return [...map.entries()].map(([metric_name, entries]) => {
    const sorted = [...entries].sort(
      (a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime(),
    );
    const points: SparkPoint[] = sorted.map((e) => ({
      ts:    new Date(e.recorded_at).getTime(),
      value: Number(e.value),
      label: new Date(e.recorded_at).toLocaleDateString(undefined, {
        month: 'short', day: 'numeric',
        year: sorted.length > 30 ? '2-digit' : undefined,
      }),
    }));
    const values = points.map((p) => p.value);
    const latest = values[values.length - 1];

    const now = Date.now();
    const WEEK = 7 * 86_400_000;
    const thisWeek = points.filter((p) => now - p.ts <= WEEK).map((p) => p.value);
    const prevWeek = points.filter((p) => now - p.ts > WEEK && now - p.ts <= 2 * WEEK).map((p) => p.value);
    let trend: MetricGroup['trend'] = 'flat';
    if (thisWeek.length > 0 && prevWeek.length > 0) {
      const pct = (thisWeek.reduce((a, b) => a + b, 0) / thisWeek.length
                 - prevWeek.reduce((a, b) => a + b, 0) / prevWeek.length)
                / Math.max(Math.abs(prevWeek.reduce((a, b) => a + b, 0) / prevWeek.length), 0.001);
      if (pct > 0.03) trend = 'up';
      else if (pct < -0.03) trend = 'down';
    }

    return {
      metric_name,
      unit:     sorted[sorted.length - 1].unit,
      points,
      entries:  [...sorted].reverse(),
      latest,
      min:      Math.min(...values),
      max:      Math.max(...values),
      trend,
      inverted: LOWER_IS_BETTER.has(metric_name),
    };
  }).sort((a, b) => a.metric_name.localeCompare(b.metric_name));
}

// ── Trend indicator ───────────────────────────────────────────────────────────

function TrendIndicator({ trend, inverted = false }: { trend: MetricGroup['trend']; inverted?: boolean }) {
  if (trend === 'up') {
    const cls = inverted ? 'text-rose-500' : 'text-emerald-400';
    return <span className={`font-mono text-xs ${cls}`}>▲</span>;
  }
  if (trend === 'down') {
    const cls = inverted ? 'text-emerald-400' : 'text-rose-500';
    return <span className={`font-mono text-xs ${cls}`}>▼</span>;
  }
  return <span className="font-mono text-xs text-zinc-500">--</span>;
}

// ── Sleep card ────────────────────────────────────────────────────────────────

interface SleepNight {
  date: string;             // YYYY-MM-DD
  duration:    number | null;
  deep:        number | null;
  light:       number | null;
  rem:         number | null;
  awake:       number | null;
  spo2Avg:     number | null;
  spo2Low:     number | null;
  respiration: number | null;
  stress:      number | null;
  startTime:   string | null;  // "HH:MM" from notes
  endTime:     string | null;
  ids:         string[];        // all log IDs for this night (for deletion)
}

function groupSleepByNight(logs: BiometricLog[]): SleepNight[] {
  const byDate = new Map<string, BiometricLog[]>();
  for (const log of logs) {
    if (!SLEEP_METRIC_NAMES.has(log.metric_name)) continue;
    const day = log.recorded_at.slice(0, 10);
    if (!byDate.has(day)) byDate.set(day, []);
    byDate.get(day)!.push(log);
  }

  const nights: SleepNight[] = [];
  for (const [date, entries] of byDate) {
    const get = (name: string) => entries.find((e) => e.metric_name === name);
    const val = (name: string) => {
      const e = get(name);
      return e ? Number(e.value) : null;
    };
    const notes = (name: string) => get(name)?.notes ?? null;

    nights.push({
      date,
      duration:    val('sleep_duration'),
      deep:        val('sleep_deep'),
      light:       val('sleep_light'),
      rem:         val('sleep_rem'),
      awake:       val('sleep_awake'),
      spo2Avg:     val('sleep_spo2_avg'),
      spo2Low:     val('sleep_spo2_low'),
      respiration: val('sleep_respiration'),
      stress:      val('sleep_stress'),
      startTime:   notes('sleep_start'),
      endTime:     notes('sleep_end'),
      ids:         entries.map((e) => e.id),
    });
  }

  return nights.sort((a, b) => b.date.localeCompare(a.date)); // newest first
}

function SleepStageBar({ night }: { night: SleepNight }) {
  const { deep, light, rem, awake, duration } = night;
  const total = (duration ?? ((deep ?? 0) + (light ?? 0) + (rem ?? 0) + (awake ?? 0))) || 1;

  const segments = [
    { label: 'DEEP',  value: deep,  color: 'bg-indigo-500' },
    { label: 'REM',   value: rem,   color: 'bg-violet-500' },
    { label: 'LIGHT', value: light, color: 'bg-sky-400'    },
    { label: 'AWAKE', value: awake, color: 'bg-zinc-600'   },
  ].filter((s) => s.value !== null && s.value > 0) as { label: string; value: number; color: string }[];

  if (segments.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5">
      {/* Stage bar */}
      <div className="flex h-2 w-full overflow-hidden rounded-none">
        {segments.map((s) => (
          <div
            key={s.label}
            className={s.color}
            style={{ width: `${(s.value / total) * 100}%` }}
          />
        ))}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {segments.map((s) => (
          <span key={s.label} className="flex items-center gap-1 font-mono text-[0.6875rem] text-zinc-500">
            <span className={`inline-block h-1.5 w-3 ${s.color}`} />
            {s.label} <span className="text-zinc-400">{s.value.toFixed(1)}h</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function SleepCard({
  nights,
  onRefresh,
}: {
  nights: SleepNight[];
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null); // date string

  const latest = nights[0];

  // 7-day average duration
  const sevenDaysAgo = Date.now() - 7 * 86_400_000;
  const recentNights = nights.filter(
    (n) => new Date(n.date).getTime() >= sevenDaysAgo && n.duration !== null,
  );
  const avgDuration = recentNights.length
    ? recentNights.reduce((s, n) => s + (n.duration ?? 0), 0) / recentNights.length
    : null;

  const formatDate = (iso: string) => {
    const d = new Date(iso + 'T12:00:00'); // noon avoids date-shift
    const today = new Date(); today.setHours(12, 0, 0, 0);
    const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return 'LAST NIGHT';
    if (d.toDateString() === yesterday.toDateString()) return 'TWO NIGHTS AGO';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }).toUpperCase();
  };

  const handleDeleteNight = async (night: SleepNight) => {
    setDeleting(night.date);
    try {
      await Promise.all(night.ids.map((id) => deleteEntry('biometric', id).catch(() => {})));
      onRefresh();
    } finally {
      setDeleting(null);
    }
  };

  if (!latest) return null;

  return (
    <div className="flex flex-col gap-4 border border-zinc-800 bg-zinc-900 p-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="text-left font-mono text-[0.6875rem] tracking-widest text-zinc-500 hover:text-cyan-400 transition-colors"
        >
          {nights.length} NIGHT{nights.length !== 1 ? 'S' : ''} LOGGED {expanded ? '▲' : '▼'}
        </button>
        {avgDuration !== null && (
          <div className="flex flex-col items-end gap-0.5">
            <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">7-DAY AVG</span>
            <div className="flex items-baseline gap-1">
              <span className="font-mono text-2xl font-bold tabular-nums text-white">
                {avgDuration.toFixed(1)}
              </span>
              <span className="self-end pb-0.5 font-mono text-xs text-zinc-400">h</span>
            </div>
          </div>
        )}
      </div>

      {/* Latest night detail */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
            {formatDate(latest.date)}
            {latest.startTime && latest.endTime && (
              <span className="ml-2 text-zinc-600">{latest.startTime} → {latest.endTime}</span>
            )}
          </span>
          {latest.duration !== null && (
            <div className="flex items-baseline gap-1">
              <span className="font-mono text-xl font-bold tabular-nums text-white">
                {latest.duration.toFixed(1)}
              </span>
              <span className="font-mono text-xs text-zinc-400">h total</span>
            </div>
          )}
        </div>

        <SleepStageBar night={latest} />

        {/* Secondary stats: SpO₂ / respiration / stress */}
        {(latest.spo2Avg !== null || latest.respiration !== null || latest.stress !== null) && (
          <div className="grid grid-cols-3 gap-2 border-t border-white/[0.05] pt-3">
            {latest.spo2Avg !== null && (
              <div className="flex flex-col gap-0.5">
                <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">SPO₂ AVG</span>
                <span className="font-mono text-sm font-semibold text-zinc-200">
                  {latest.spo2Avg}%
                  {latest.spo2Low !== null && (
                    <span className="ml-1.5 text-[0.6875rem] font-normal text-zinc-500">
                      lo {latest.spo2Low}%
                    </span>
                  )}
                </span>
              </div>
            )}
            {latest.respiration !== null && (
              <div className="flex flex-col gap-0.5">
                <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">RESPIRATION</span>
                <span className="font-mono text-sm font-semibold text-zinc-200">
                  {latest.respiration.toFixed(1)} <span className="text-[0.6875rem] font-normal text-zinc-500">br/min</span>
                </span>
              </div>
            )}
            {latest.stress !== null && (
              <div className="flex flex-col gap-0.5">
                <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">STRESS</span>
                <span className={`font-mono text-sm font-semibold ${
                  latest.stress < 15 ? 'text-emerald-400' :
                  latest.stress < 25 ? 'text-yellow-400' : 'text-rose-400'
                }`}>
                  {latest.stress}
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* History table */}
      {expanded && (
        <div className="border-t border-zinc-800 pt-3">
          <div className="mb-2 grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 px-2">
            {['DATE', 'TOTAL', 'DEEP', 'REM', 'SPO₂', ''].map((h) => (
              <span key={h} className="font-mono text-[0.625rem] tracking-widest text-zinc-600">{h}</span>
            ))}
          </div>
          <div className="flex flex-col gap-0.5">
            {nights.map((night) => (
              <div
                key={night.date}
                className="group grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 items-center px-2 py-1.5 transition-colors hover:bg-cyan-500/[0.05]"
              >
                <span className="font-mono text-[0.75rem] text-zinc-400">
                  {new Date(night.date + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
                  {night.startTime && (
                    <span className="ml-1.5 text-zinc-600">{night.startTime}</span>
                  )}
                </span>
                <span className="font-mono text-[0.75rem] tabular-nums text-zinc-200">
                  {night.duration !== null ? `${night.duration.toFixed(1)}h` : '--'}
                </span>
                <span className="font-mono text-[0.75rem] tabular-nums text-indigo-400">
                  {night.deep !== null ? `${night.deep.toFixed(1)}h` : '--'}
                </span>
                <span className="font-mono text-[0.75rem] tabular-nums text-violet-400">
                  {night.rem !== null ? `${night.rem.toFixed(1)}h` : '--'}
                </span>
                <span className="font-mono text-[0.75rem] tabular-nums text-zinc-400">
                  {night.spo2Avg !== null ? `${night.spo2Avg}%` : '--'}
                </span>
                <button
                  onClick={() => handleDeleteNight(night)}
                  disabled={deleting === night.date}
                  className="opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.6875rem] text-zinc-500 hover:text-rose-400 disabled:text-zinc-600"
                  title="Delete this night's sleep data"
                >
                  {deleting === night.date ? '…' : '×'}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Workout card ──────────────────────────────────────────────────────────────

function parseActivityName(notes: string | null | undefined): string {
  if (!notes) return 'Workout';
  return notes.split(' · ')[0] || 'Workout';
}

function parseDurationFromNotes(notes: string | null | undefined): number | null {
  if (!notes) return null;
  const match = notes.match(/\b(\d+)min\b/);
  return match ? parseInt(match[1], 10) : null;
}

interface WorkoutEntry { session: BiometricLog; duration: number | null }

function WorkoutCard({
  sessions,
  durations,
  onRefresh,
}: {
  sessions:  BiometricLog[];
  durations: BiometricLog[];
  onRefresh: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const durationByTs = useMemo(() => {
    const map = new Map<string, { value: number; id: string }>();
    for (const d of durations) map.set(d.recorded_at, { value: Number(d.value), id: d.id });
    return map;
  }, [durations]);

  const entries: WorkoutEntry[] = useMemo(() => {
    const sorted = [...sessions].sort(
      (a, b) => new Date(b.recorded_at).getTime() - new Date(a.recorded_at).getTime(),
    );
    return sorted.map((s) => ({
      session:  s,
      duration: durationByTs.get(s.recorded_at)?.value ?? parseDurationFromNotes(s.notes),
    }));
  }, [sessions, durationByTs]);

  const sparkPoints: SparkPoint[] = useMemo(() => {
    return [...sessions]
      .sort((a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime())
      .map((s) => ({
        ts:    new Date(s.recorded_at).getTime(),
        value: Number(s.value),
        label: new Date(s.recorded_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      }));
  }, [sessions]);

  const trend = useMemo((): MetricGroup['trend'] => {
    const now = Date.now();
    const WEEK = 7 * 86_400_000;
    const thisWeek = sparkPoints.filter((p) => now - p.ts <= WEEK).map((p) => p.value);
    const prevWeek = sparkPoints.filter((p) => now - p.ts > WEEK && now - p.ts <= 2 * WEEK).map((p) => p.value);
    if (!thisWeek.length || !prevWeek.length) return 'flat';
    const diff = thisWeek.reduce((a, b) => a + b, 0) / thisWeek.length
               - prevWeek.reduce((a, b) => a + b, 0) / prevWeek.length;
    return diff > 3 ? 'up' : diff < -3 ? 'down' : 'flat';
  }, [sparkPoints]);

  const latest = entries[0];
  const hrValues = sparkPoints.map((p) => p.value);
  const durValues = entries.filter((e) => e.duration !== null).map((e) => e.duration as number);
  const avgDuration = durValues.length
    ? Math.round(durValues.reduce((a, b) => a + b, 0) / durValues.length)
    : null;

  const handleDelete = async (sessionId: string, ev: React.MouseEvent) => {
    ev.stopPropagation();
    setDeleting(sessionId);
    try {
      await deleteEntry('biometric', sessionId);
      const dId = durationByTs.get(sessions.find((s) => s.id === sessionId)?.recorded_at ?? '')?.id;
      if (dId) await deleteEntry('biometric', dId).catch(() => {});
      onRefresh();
    } finally {
      setDeleting(null);
    }
  };

  if (!sessions.length) return null;

  return (
    <div className="flex flex-col gap-4 border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex items-start justify-between gap-2">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="text-left font-mono text-[0.6875rem] tracking-widest text-zinc-500 hover:text-cyan-400 transition-colors"
        >
          {entries.length} SESSION{entries.length !== 1 ? 'S' : ''} {expanded ? '▲' : '▼'}
        </button>
        <div className="flex items-end gap-5">
          <div
            className="flex cursor-pointer flex-col items-end gap-0.5 px-1 py-0.5 transition-colors hover:bg-cyan-500/[0.07]"
            title="Pin to chat"
            onClick={() => {
              if (!latest) return;
              window.dispatchEvent(new CustomEvent('health:reference', {
                detail: { label: `${new Date(latest.session.recorded_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · avg HR`, value: `${Number(latest.session.value)} bpm` },
              }));
            }}
          >
            <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">AVG HR</span>
            <div className="flex items-baseline gap-1">
              <TrendIndicator trend={trend} inverted={false} />
              <span className="font-mono text-2xl font-bold tabular-nums text-white">
                {latest ? Number(latest.session.value) : '--'}
              </span>
              <span className="self-end pb-0.5 font-mono text-xs text-zinc-400">bpm</span>
            </div>
          </div>
          {latest?.duration != null && (
            <div className="flex flex-col items-end gap-0.5">
              <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">DURATION</span>
              <div className="flex items-baseline gap-1">
                <span className="font-mono text-2xl font-bold tabular-nums text-cyan-300">
                  {latest.duration}
                </span>
                <span className="self-end pb-0.5 font-mono text-xs text-zinc-400">min</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {sparkPoints.length > 1 ? (
        <Sparkline points={sparkPoints} />
      ) : (
        <div className="flex h-14 items-center justify-center font-mono text-[0.6875rem] tracking-widest text-zinc-500">
          SINGLE SESSION: LOG MORE TO SEE TREND
        </div>
      )}

      {sparkPoints.length > 1 && (
        <div className="flex justify-between">
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
            HR RANGE <span className="text-zinc-400">{Math.min(...hrValues)}–{Math.max(...hrValues)} bpm</span>
          </span>
          {avgDuration !== null && (
            <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
              AVG DURATION <span className="text-zinc-400">{avgDuration} min</span>
            </span>
          )}
        </div>
      )}

      {expanded && (
        <div className="border-t border-zinc-800 pt-3 flex flex-col gap-1">
          {entries.map(({ session: s, duration: dur }) => (
            <div
              key={s.id}
              className="group flex items-center gap-2 px-2 py-1.5 transition-colors hover:bg-cyan-500/[0.06]"
            >
              <span className="w-28 shrink-0 font-mono text-[0.75rem] text-zinc-400 tabular-nums">
                {new Date(s.recorded_at).toLocaleString(undefined, {
                  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                })}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-[0.6875rem] text-zinc-500">
                {parseActivityName(s.notes)}
              </span>
              <span className="shrink-0 font-mono text-[0.75rem] font-semibold tabular-nums text-zinc-200">
                {Number(s.value)} bpm
              </span>
              {dur !== null ? (
                <span className="w-14 shrink-0 text-right font-mono text-[0.75rem] tabular-nums text-cyan-400/80">
                  {dur} min
                </span>
              ) : (
                <span className="w-14 shrink-0 text-right font-mono text-[0.75rem] text-zinc-600">--</span>
              )}
              <button
                onClick={(ev) => handleDelete(s.id, ev)}
                disabled={deleting === s.id}
                className="ml-1 opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.6875rem] text-zinc-500 hover:text-rose-400 disabled:text-zinc-600"
              >
                {deleting === s.id ? '…' : '×'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Metric card ───────────────────────────────────────────────────────────────

function dispatchRef(metric_name: string, value: number, unit: string, recorded_at: string) {
  window.dispatchEvent(new CustomEvent('health:reference', {
    detail: {
      label: `${new Date(recorded_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · ${metricLabel(metric_name)}`,
      value: `${value} ${unit}`,
    },
  }));
}

function MetricCard({ group, onRefresh }: { group: MetricGroup; onRefresh: () => void }) {
  const { metric_name, unit, points, entries, latest, min, max, trend, inverted } = group;
  const [expanded, setExpanded] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const isSinglePoint = points.length === 1;
  const mean = points.reduce((s, p) => s + p.value, 0) / points.length;
  const latestEntry = entries[0];

  const handleDelete = async (id: string, ev: React.MouseEvent) => {
    ev.stopPropagation();
    setDeleting(id);
    try {
      await deleteEntry('biometric', id);
      onRefresh();
    } finally {
      setDeleting(null);
    }
  };

  // Format value for display: strip trailing decimal zero for whole numbers
  const fmtVal = (v: number) => Number.isInteger(v) ? String(v) : v.toFixed(1);
  // Strip redundant unit tokens
  const displayUnit = unit === 'score' ? '' : unit === 'hour_of_day' ? '' : unit;

  return (
    <div className="flex flex-col gap-4 border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <h3 className="font-mono text-xs font-semibold uppercase tracking-widest text-zinc-300">
            {metricLabel(metric_name)}
          </h3>
          <button
            onClick={() => setExpanded((e) => !e)}
            className="text-left font-mono text-[0.6875rem] tracking-widest text-zinc-500 hover:text-cyan-400 transition-colors"
          >
            {points.length} READING{points.length !== 1 ? 'S' : ''} {expanded ? '▲' : '▼'}
          </button>
        </div>
        <div
          className="flex cursor-pointer items-center gap-2 px-1 py-0.5 transition-colors hover:bg-cyan-500/[0.07]"
          onClick={() => latestEntry && dispatchRef(metric_name, Number(latestEntry.value), unit, latestEntry.recorded_at)}
          title="Pin latest reading to chat"
        >
          <TrendIndicator trend={trend} inverted={inverted} />
          <span className="font-mono text-2xl font-bold tabular-nums text-white">{fmtVal(latest)}</span>
          {displayUnit && <span className="self-end pb-0.5 font-mono text-xs text-zinc-400">{displayUnit}</span>}
        </div>
      </div>

      {isSinglePoint ? (
        <div className="flex h-14 items-center justify-center font-mono text-[0.6875rem] tracking-widest text-zinc-500">
          SINGLE READING: LOG MORE DATA TO SEE TREND
        </div>
      ) : (
        <Sparkline points={points} />
      )}

      {!isSinglePoint && (
        <div className="flex justify-between">
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
            RANGE <span className="text-zinc-400">{fmtVal(min)}–{fmtVal(max)}{displayUnit ? ` ${displayUnit}` : ''}</span>
          </span>
          <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
            AVG <span className="text-zinc-400">{mean.toFixed(1)}{displayUnit ? ` ${displayUnit}` : ''}</span>
          </span>
        </div>
      )}

      {expanded && (
        <div className="border-t border-zinc-800 pt-3 flex flex-col gap-1">
          {entries.map((e) => (
            <div
              key={e.id}
              onClick={() => dispatchRef(metric_name, Number(e.value), unit, e.recorded_at)}
              className="group flex cursor-pointer items-center justify-between gap-2 px-2 py-1.5 transition-colors hover:bg-cyan-500/[0.06]"
            >
              <span className="font-mono text-[0.75rem] text-zinc-400 tabular-nums">
                {new Date(e.recorded_at).toLocaleString(undefined, {
                  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                })}
              </span>
              <span className="font-mono text-[0.75rem] font-semibold text-zinc-200 tabular-nums">
                {fmtVal(Number(e.value))}{displayUnit ? ` ${displayUnit}` : ''}
              </span>
              <button
                onClick={(ev) => handleDelete(e.id, ev)}
                disabled={deleting === e.id}
                className="ml-auto opacity-0 group-hover:opacity-100 transition-opacity font-mono text-[0.6875rem] text-zinc-500 hover:text-rose-400 disabled:text-zinc-600"
              >
                {deleting === e.id ? '…' : '×'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 pt-1">
      <span className="font-mono text-[0.6875rem] font-semibold tracking-[0.2em] text-zinc-600">{label}</span>
      <div className="flex-1 border-t border-zinc-800" />
    </div>
  );
}

// ── Stat chip ─────────────────────────────────────────────────────────────────

function Stat({ label, value, color }: { label: string; value: number; color?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400">{label}</span>
      <span className={`font-mono text-xl font-bold leading-none tabular-nums ${color ?? 'text-white'}`}>
        {value}
      </span>
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export default function BiometricsPanel({ biometrics, isLoading, onRefresh }: BiometricsPanelProps) {
  const [searchQuery, setSearchQuery] = useState('');

  const workoutSessions  = useMemo(() => biometrics.filter((b) => b.metric_name === 'workout_session'),  [biometrics]);
  const workoutDurations = useMemo(() => biometrics.filter((b) => b.metric_name === 'workout_duration'), [biometrics]);
  const sleepNights      = useMemo(() => groupSleepByNight(biometrics), [biometrics]);
  const allGroups        = useMemo(() => groupByMetric(biometrics), [biometrics]);

  const groups = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return allGroups;
    return allGroups.filter((g) =>
      g.metric_name.toLowerCase().includes(q) || metricLabel(g.metric_name).toLowerCase().includes(q),
    );
  }, [allGroups, searchQuery]);

  const showSleep   = !searchQuery || 'sleep'.includes(searchQuery.toLowerCase());
  const showWorkout = !searchQuery || ['workout','session','duration'].some((w) => w.includes(searchQuery.toLowerCase()));

  // Category-split the non-sleep, non-workout groups
  const cardioGroups = groups.filter((g) => metricCategory(g.metric_name) === 'cardiovascular');
  const bodyGroups   = groups.filter((g) => metricCategory(g.metric_name) === 'body');
  const otherGroups  = groups.filter((g) => metricCategory(g.metric_name) === 'other');

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center font-mono text-xs tracking-widest text-zinc-500">
        LOADING BIOMETRIC DATA…
      </div>
    );
  }

  if (biometrics.length === 0) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-4">
        <p className="font-mono text-xs font-semibold tracking-widest text-zinc-300">NO BIOMETRIC DATA</p>
        <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">Log vitals or sync Garmin to track trends</p>
      </div>
    );
  }

  const totalMetrics = allGroups.length + (sleepNights.length > 0 ? 1 : 0) + (workoutSessions.length > 0 ? 1 : 0);
  const trendingUp   = allGroups.filter((g) => g.trend === 'up'   && !g.inverted).length
                     + allGroups.filter((g) => g.trend === 'down' &&  g.inverted).length;
  const trendingDown = allGroups.filter((g) => g.trend === 'down' && !g.inverted).length
                     + allGroups.filter((g) => g.trend === 'up'   &&  g.inverted).length;

  return (
    <div className="flex flex-col gap-3">
      {/* Summary bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 border border-zinc-800 bg-zinc-900 px-5 py-3">
        <div className="flex gap-8">
          <Stat label="METRICS" value={totalMetrics} />
          <Stat label="READINGS" value={biometrics.length} />
          <Stat label="IMPROVING" value={trendingUp}   color={trendingUp   > 0 ? 'text-emerald-400' : undefined} />
          <Stat label="DECLINING" value={trendingDown} color={trendingDown > 0 ? 'text-rose-500'    : undefined} />
        </div>
        <div className="flex items-center gap-1.5 border border-zinc-700 bg-zinc-800/60 px-3 py-1.5">
          <svg className="h-3 w-3 shrink-0 text-zinc-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
          </svg>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="FILTER METRICS…"
            className="w-36 bg-transparent font-mono text-[0.6875rem] tracking-widest text-zinc-300 placeholder-zinc-600 focus:outline-none"
          />
          {searchQuery && (
            <button onClick={() => setSearchQuery('')} className="text-zinc-600 hover:text-zinc-400">×</button>
          )}
        </div>
      </div>

      {/* Cards */}
      <div className="flex flex-col gap-4">
        {/* Sleep */}
        {showSleep && sleepNights.length > 0 && (
          <>
            <SectionHeader label="SLEEP" />
            <SleepCard nights={sleepNights} onRefresh={onRefresh} />
          </>
        )}

        {/* Cardiovascular */}
        {cardioGroups.length > 0 && (
          <>
            <SectionHeader label="CARDIOVASCULAR" />
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {cardioGroups.map((g) => <MetricCard key={g.metric_name} group={g} onRefresh={onRefresh} />)}
            </div>
          </>
        )}

        {/* Workouts */}
        {showWorkout && workoutSessions.length > 0 && (
          <>
            <SectionHeader label="WORKOUTS" />
            <WorkoutCard sessions={workoutSessions} durations={workoutDurations} onRefresh={onRefresh} />
          </>
        )}

        {/* Body composition */}
        {bodyGroups.length > 0 && (
          <>
            <SectionHeader label="BODY COMPOSITION" />
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {bodyGroups.map((g) => <MetricCard key={g.metric_name} group={g} onRefresh={onRefresh} />)}
            </div>
          </>
        )}

        {/* Other */}
        {otherGroups.length > 0 && (
          <>
            {(cardioGroups.length > 0 || bodyGroups.length > 0 || showSleep) && (
              <SectionHeader label="OTHER" />
            )}
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {otherGroups.map((g) => <MetricCard key={g.metric_name} group={g} onRefresh={onRefresh} />)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
