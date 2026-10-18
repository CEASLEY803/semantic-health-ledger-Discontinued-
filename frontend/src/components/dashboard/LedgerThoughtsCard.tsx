'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchMorningBriefing, fetchSynthesisStatus, postForceSynthesis } from '@/lib/api';
import type { ClinicalEdge, ClinicalNode, ConfidenceLevel, MorningBriefing } from '@/lib/types';
import { useEventBus } from '@/hooks/useEventBus';

const CONFIDENCE_STYLES: Record<ConfidenceLevel, { badge: string; dot: string }> = {
  confirmed:  { badge: 'bg-emerald-950/60 text-emerald-400 border-emerald-800/50', dot: 'bg-emerald-400' },
  testing:    { badge: 'bg-amber-950/60  text-amber-400  border-amber-800/50',  dot: 'bg-amber-400'  },
  hypothesis: { badge: 'bg-zinc-800/60   text-zinc-400   border-zinc-700/50',   dot: 'bg-zinc-500'   },
};

function NodePill({ node }: { node: ClinicalNode }) {
  const [expanded, setExpanded] = useState(false);
  const styles = CONFIDENCE_STYLES[node.confidence_level] ?? CONFIDENCE_STYLES.hypothesis;

  return (
    <button
      onClick={() => setExpanded((e) => !e)}
      className="w-full text-left"
    >
      <div className={`border px-3 py-2 transition-colors hover:bg-white/[0.03] ${styles.badge}`}>
        <div className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${styles.dot}`} />
          <span className="font-mono text-[0.6875rem] font-semibold tracking-widest uppercase">
            {node.confidence_level}
          </span>
          <span className="font-mono text-[0.75rem] font-bold text-white truncate">
            {node.concept_name}
          </span>
          <span className="ml-auto font-mono text-[0.6875rem] text-zinc-500 shrink-0">
            {node.category}
          </span>
        </div>
        {expanded && (
          <p className="mt-2 font-mono text-[0.75rem] leading-relaxed text-zinc-300 whitespace-normal">
            {node.summary_text}
          </p>
        )}
      </div>
    </button>
  );
}

function EdgeRow({ edge }: { edge: ClinicalEdge }) {
  const REL_COLORS: Record<string, string> = {
    CAUSES:          'text-rose-400',
    MITIGATES:       'text-emerald-400',
    EXACERBATES:     'text-orange-400',
    CORRELATES_WITH: 'text-cyan-400',
    REQUIRES:        'text-violet-400',
    PRECEDES:        'text-blue-400',
  };
  const relColor = REL_COLORS[edge.relationship_type] ?? 'text-zinc-400';

  return (
    <div className="flex items-center gap-1.5 font-mono text-[0.6875rem] py-0.5">
      <span className="text-zinc-300 truncate max-w-[28%]">{edge.source}</span>
      <span className="text-zinc-600">-[</span>
      <span className={`font-semibold ${relColor}`}>{edge.relationship_type}</span>
      <span className="text-zinc-600">]→</span>
      <span className="text-zinc-300 truncate max-w-[28%]">{edge.target}</span>
      {edge.evidence_summary && (
        <span className="text-zinc-500 truncate flex-1 ml-1">{edge.evidence_summary}</span>
      )}
    </div>
  );
}

// ── Force-synthesis button ────────────────────────────────────────────────────

type SynthBtnState = 'idle' | 'running' | 'cooldown';

function fmtCountdown(s: number): string {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function ForceSynthesisButton({ onComplete }: { onComplete: () => void }) {
  const [state, setState] = useState<SynthBtnState>('idle');
  const [cooldown, setCooldown] = useState(0);
  const [lastDate, setLastDate] = useState<string | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // On mount: check if we're already in a cooldown window from a previous run
  useEffect(() => {
    fetchSynthesisStatus()
      .then((s) => {
        setLastDate(s.last_synthesis_date);
        if (s.cooldown_remaining_seconds > 0) {
          setCooldown(s.cooldown_remaining_seconds);
          setState('cooldown');
        }
      })
      .catch(() => {});
  }, []);

  // Countdown tick: only active while in cooldown state
  useEffect(() => {
    if (state !== 'cooldown') {
      if (tickRef.current) clearInterval(tickRef.current);
      tickRef.current = null;
      return;
    }
    tickRef.current = setInterval(() => {
      setCooldown((prev) => {
        if (prev <= 1) {
          setState('idle');
          setSummary(null);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => { if (tickRef.current) clearInterval(tickRef.current); };
  }, [state]);

  const handleClick = async () => {
    if (state !== 'idle') return;
    setState('running');
    setSummary(null);
    try {
      const result = await postForceSynthesis();

      if (result.cooldown_remaining_seconds != null && !result.success) {
        // Server-side cooldown was still active (clock skew edge case)
        setCooldown(result.cooldown_remaining_seconds);
        setState('cooldown');
        return;
      }

      const total = (result.nodes_created ?? 0) + (result.nodes_updated ?? 0);
      const edges = result.edges_created ?? 0;
      setSummary(`${total} node${total !== 1 ? 's' : ''} · ${edges} edge${edges !== 1 ? 's' : ''}`);
      setLastDate(result.synthesis_date ?? null);
      setCooldown(result.cooldown_seconds ?? 600);
      setState('cooldown');
      onComplete();
    } catch {
      // On error, return to idle so the user can retry
      setState('idle');
    }
  };

  const isIdle = state === 'idle';

  return (
    <div className="flex items-center justify-between">
      <div className="flex flex-col gap-0.5">
        <button
          onClick={handleClick}
          disabled={!isIdle}
          className={`flex items-center gap-2 font-mono text-[0.6875rem] tracking-widest transition-colors ${
            isIdle
              ? 'text-cyan-500 hover:text-cyan-300 cursor-pointer'
              : 'text-zinc-600 cursor-not-allowed'
          }`}
        >
          {state === 'running' && (
            <span className="inline-block h-2 w-2 shrink-0 animate-spin rounded-full border border-zinc-500 border-t-zinc-300" />
          )}
          {state === 'running'  && 'SYNTHESIZING...'}
          {state === 'cooldown' && `READY IN ${fmtCountdown(cooldown)}`}
          {state === 'idle'     && 'RUN SYNTHESIS'}
        </button>
        {summary && state === 'cooldown' && (
          <span className="font-mono text-[0.625rem] text-emerald-600 tracking-widest">{summary} updated</span>
        )}
      </div>

      {lastDate && (
        <span className="font-mono text-[0.625rem] tracking-widest text-zinc-700">
          last run {lastDate}
        </span>
      )}
    </div>
  );
}

// ── Card ──────────────────────────────────────────────────────────────────────

export default function LedgerThoughtsCard() {
  const [briefing, setBriefing] = useState<MorningBriefing | null>(null);
  const [internalKey, setInternalKey] = useState(0);

  // Re-fetch when the knowledge graph is updated via the SSE event bus.
  // The ForceSynthesisButton's onComplete callback also increments internalKey
  // for the local-action case (both paths converge on the same state setter).
  useEventBus(useCallback((event) => {
    if (event.type === 'SYNTHESIS_COMPLETE') {
      setInternalKey((k) => k + 1);
    }
  }, []));

  useEffect(() => {
    let cancelled = false;
    fetchMorningBriefing()
      .then((data) => { if (!cancelled) setBriefing(data); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [internalKey]);

  const noData = !briefing?.ready || briefing.nodes.length === 0;

  return (
    <div className="flex flex-col gap-4">
      {/* Synthesis trigger: always visible */}
      <div className="border border-zinc-800 bg-zinc-900/60 px-4 py-3">
        <ForceSynthesisButton onComplete={() => setInternalKey((k) => k + 1)} />
      </div>

      {noData ? (
        <div className="flex flex-col items-center justify-center gap-7 py-20">
          {/* Sparkles / synthesis icon */}
          <div className="flex h-14 w-14 items-center justify-center bg-zinc-800 text-zinc-500">
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" />
            </svg>
          </div>
          <div className="flex flex-col items-center gap-1.5">
            <h2 className="font-mono text-lg font-semibold tracking-wide text-zinc-200">
              Awaiting Synthesis
            </h2>
            <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-500">
              Run synthesis above, or wait: the graph updates automatically at first boot each day
            </p>
          </div>
        </div>
      ) : (
      <div>
      {/* Header */}
      <div className="mb-3 flex items-center gap-3">
        <div className="h-px flex-1 bg-zinc-800" />
        <span className="font-mono text-[0.6875rem] font-semibold tracking-[0.2em] text-cyan-500/80 uppercase">
          Ledger&apos;s Thoughts ({briefing.synthesis_date})
        </span>
        <div className="h-px flex-1 bg-zinc-800" />
      </div>

      {/* Nodes */}
      <div className="grid gap-1.5 sm:grid-cols-2">
        {briefing.nodes.map((node) => (
          <NodePill key={node.concept_name} node={node} />
        ))}
      </div>

      {/* Edges */}
      {briefing.edges.length > 0 && (
        <div className="mt-3 border border-zinc-800 bg-zinc-900/50 px-3 py-2">
          <p className="mb-1.5 font-mono text-[0.625rem] tracking-widest text-zinc-500 uppercase">
            Relationships
          </p>
          <div className="space-y-0.5">
            {briefing.edges.map((edge, i) => (
              <EdgeRow key={i} edge={edge} />
            ))}
          </div>
        </div>
      )}
      </div>
      )}
    </div>
  );
}
