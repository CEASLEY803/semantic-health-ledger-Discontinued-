'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchAuditReport, postRunAudit, postApplyAuditAction } from '@/lib/api';
import type { AuditChallenge, AuditReport, ProposedAction } from '@/lib/types';

// ── Helpers ───────────────────────────────────────────────────────────────────

type AuditRunState = 'idle' | 'running' | 'cooldown';

function fmtCountdown(s: number): string {
  return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, '0')}`;
}

const ACTION_LABELS: Record<ProposedAction, string> = {
  archive_node:      'ARCHIVE',
  demote_to_testing: 'DEMOTE',
  request_new_lab:   'FLAG LAB',
};

const ACTION_DESCS: Record<ProposedAction, string> = {
  archive_node:      'Remove node from the active graph',
  demote_to_testing: 'Lower confidence level to TESTING',
  request_new_lab:   'Mark as requiring lab validation',
};

// ── Challenge card ────────────────────────────────────────────────────────────

interface ChallengeCardProps {
  challenge:  AuditChallenge;
  isPending:  boolean;
  onAccept:   (c: AuditChallenge) => void;
  onDismiss:  (c: AuditChallenge) => void;
}

function ChallengeCard({ challenge, isPending, onAccept, onDismiss }: ChallengeCardProps) {
  const action = challenge.proposed_action;
  return (
    <div className="border border-rose-900/40 bg-rose-950/20 p-3.5 flex flex-col gap-2.5">
      {/* Concept name + action badge */}
      <div className="flex items-start justify-between gap-2">
        <p className="font-mono text-[0.75rem] font-bold text-rose-300 leading-snug">
          {challenge.concept_name}
        </p>
        <span className="shrink-0 border border-rose-800/50 bg-rose-950/60 px-1.5 py-0.5 font-mono text-[0.625rem] tracking-widest text-rose-400">
          {ACTION_LABELS[action]}
        </span>
      </div>

      {/* Critique */}
      <p className="font-sans text-[0.75rem] leading-relaxed text-zinc-400">
        {challenge.critique}
      </p>

      {/* Action description */}
      <p className="font-mono text-[0.625rem] tracking-wider text-zinc-600">
        {ACTION_DESCS[action]}
      </p>

      {/* Accept / Dismiss */}
      <div className="flex items-center gap-2 pt-0.5">
        <button
          onClick={() => onAccept(challenge)}
          disabled={isPending}
          className="flex-1 border border-rose-700/50 bg-rose-900/30 px-2 py-1.5 font-mono text-[0.625rem] tracking-widest text-rose-300 transition-colors hover:bg-rose-900/60 hover:text-rose-200 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {isPending ? '…' : 'ACCEPT'}
        </button>
        <button
          onClick={() => onDismiss(challenge)}
          disabled={isPending}
          className="flex-1 border border-zinc-700/50 bg-zinc-900/30 px-2 py-1.5 font-mono text-[0.625rem] tracking-widest text-zinc-500 transition-colors hover:bg-zinc-800/60 hover:text-zinc-300 disabled:cursor-not-allowed disabled:opacity-40"
        >
          DISMISS
        </button>
      </div>
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

interface RedTeamReviewProps {
  onClose:         () => void;
  onGraphUpdated:  () => void;
}

export default function RedTeamReview({ onClose, onGraphUpdated }: RedTeamReviewProps) {
  // Report data
  const [report,      setReport]      = useState<AuditReport | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [loadState,   setLoadState]   = useState<'loading' | 'error' | 'ready'>('loading');

  // Per-challenge state (keyed by concept_name)
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [accepted,  setAccepted]  = useState<Set<string>>(new Set());
  const [pending,   setPending]   = useState<Set<string>>(new Set());

  // Run-audit flow
  const [runState,    setRunState]    = useState<AuditRunState>('idle');
  const [cooldown,    setCooldown]    = useState(0);
  const [runSummary,  setRunSummary]  = useState<string | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch report on mount ──────────────────────────────────────────────────
  useEffect(() => {
    fetchAuditReport()
      .then((data) => {
        setReport(data.report);
        setGeneratedAt(data.generated_at);
        setLoadState('ready');
      })
      .catch(() => setLoadState('error'));
  }, []);

  // ── Cooldown countdown ─────────────────────────────────────────────────────
  useEffect(() => {
    if (runState !== 'cooldown') {
      if (tickRef.current) clearInterval(tickRef.current);
      tickRef.current = null;
      return;
    }
    tickRef.current = setInterval(() => {
      setCooldown((prev) => {
        if (prev <= 1) { setRunState('idle'); return 0; }
        return prev - 1;
      });
    }, 1000);
    return () => { if (tickRef.current) clearInterval(tickRef.current); };
  }, [runState]);

  // ── Run audit ──────────────────────────────────────────────────────────────
  const handleRunAudit = useCallback(async () => {
    if (runState !== 'idle') return;
    setRunState('running');
    setRunSummary(null);
    try {
      const result = await postRunAudit();
      if (result.cooldown_remaining_seconds != null && !result.success) {
        setCooldown(result.cooldown_remaining_seconds);
        setRunState('cooldown');
        return;
      }
      const n = result.challenges_found ?? 0;
      setRunSummary(`${n} challenge${n !== 1 ? 's' : ''} found`);
      setCooldown(3600);
      setRunState('cooldown');
      // Reload report with fresh findings
      const fresh = await fetchAuditReport();
      setReport(fresh.report);
      setGeneratedAt(fresh.generated_at);
      setDismissed(new Set());
      setAccepted(new Set());
    } catch {
      setRunState('idle');
    }
  }, [runState]);

  // ── Accept challenge → apply action ───────────────────────────────────────
  const handleAccept = useCallback(async (challenge: AuditChallenge) => {
    const key = challenge.concept_name;
    setPending((p) => { const s = new Set(p); s.add(key);    return s; });
    try {
      const result = await postApplyAuditAction(challenge.concept_name, challenge.proposed_action);
      setAccepted((a) => { const s = new Set(a); s.add(key); return s; });
      if (result.graph_updated) onGraphUpdated();
    } catch {
      // Error: leave card visible so user can retry
    } finally {
      setPending((p) => { const s = new Set(p); s.delete(key); return s; });
    }
  }, [onGraphUpdated]);

  // ── Dismiss challenge (local-only) ─────────────────────────────────────────
  const handleDismiss = useCallback((challenge: AuditChallenge) => {
    setDismissed((d) => { const s = new Set(d); s.add(challenge.concept_name); return s; });
  }, []);

  // Challenges that haven't been resolved yet
  const visibleChallenges = (report?.challenges ?? []).filter(
    (c) => !dismissed.has(c.concept_name) && !accepted.has(c.concept_name),
  );

  const isIdle = runState === 'idle';

  return (
    <div className="absolute left-3 top-3 bottom-3 z-20 flex w-80 flex-col border border-rose-900/50 bg-zinc-950/[0.97] shadow-2xl ring-1 ring-rose-900/20 backdrop-blur-sm">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex shrink-0 items-center justify-between border-b border-rose-900/40 px-3.5 py-2.5">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500" />
          <span className="font-mono text-[0.625rem] tracking-[0.18em] text-rose-400">
            RED TEAM AUDIT
          </span>
        </div>
        <button
          onClick={onClose}
          aria-label="Close audit panel"
          className="font-mono text-[0.75rem] text-zinc-600 transition-colors hover:text-zinc-300"
        >
          ✕
        </button>
      </div>

      {/* ── Run audit button ────────────────────────────────────────────────── */}
      <div className="shrink-0 border-b border-rose-900/20 px-3.5 py-2.5">
        <button
          onClick={handleRunAudit}
          disabled={!isIdle}
          className={`flex w-full items-center justify-center gap-2 border py-2 font-mono text-[0.625rem] tracking-widest transition-colors disabled:cursor-not-allowed ${
            isIdle
              ? 'border-rose-700/50 text-rose-400 hover:bg-rose-950/40 hover:text-rose-300'
              : 'border-zinc-800/50 text-zinc-600'
          }`}
        >
          {runState === 'running' && (
            <span className="h-2 w-2 shrink-0 animate-spin rounded-full border border-zinc-600 border-t-zinc-300" />
          )}
          {runState === 'running'  && 'AUDITING GRAPH…'}
          {runState === 'cooldown' && `NEXT AUDIT IN ${fmtCountdown(cooldown)}`}
          {runState === 'idle'     && 'RUN FULL AUDIT'}
        </button>
        {runSummary && (
          <p className="mt-1.5 text-center font-mono text-[0.625rem] tracking-widest text-rose-600">
            {runSummary}
          </p>
        )}
      </div>

      {/* ── Body: scrollable ────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto">

        {loadState === 'loading' && (
          <div className="flex h-full items-center justify-center">
            <span className="animate-pulse font-mono text-[0.6875rem] tracking-widest text-zinc-600">
              LOADING…
            </span>
          </div>
        )}

        {loadState === 'error' && (
          <div className="flex h-full items-center justify-center px-4">
            <span className="text-center font-mono text-[0.6875rem] tracking-widest text-rose-700">
              AUDIT REPORT UNAVAILABLE
            </span>
          </div>
        )}

        {loadState === 'ready' && report === null && (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-4">
            <span className="text-center font-mono text-[0.6875rem] tracking-widest text-zinc-600">
              NO AUDIT REPORT
            </span>
            <span className="text-center font-mono text-[0.625rem] tracking-widest text-zinc-700">
              Run a full audit to generate findings
            </span>
          </div>
        )}

        {loadState === 'ready' && report !== null && (
          <div className="flex flex-col">

            {/* Summary rationale */}
            <div className="border-b border-zinc-800/40 px-3.5 py-3">
              <p className="mb-1.5 font-mono text-[0.625rem] tracking-widest text-zinc-600">
                SUMMARY
              </p>
              <p className="font-sans text-[0.75rem] leading-relaxed text-zinc-400">
                {report.summary_rationale}
              </p>
              {generatedAt && (
                <p className="mt-2 font-mono text-[0.625rem] tracking-widest text-zinc-700">
                  GENERATED {generatedAt.slice(0, 10)}
                </p>
              )}
            </div>

            {/* Challenge cards */}
            {visibleChallenges.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 px-4 py-8">
                <span className="font-mono text-[0.6875rem] tracking-widest text-emerald-700">
                  ALL CHALLENGES RESOLVED
                </span>
              </div>
            ) : (
              <div className="flex flex-col gap-3 p-3">
                <p className="font-mono text-[0.625rem] tracking-widest text-zinc-600">
                  {visibleChallenges.length}&nbsp;CHALLENGE
                  {visibleChallenges.length !== 1 ? 'S' : ''}
                </p>
                {visibleChallenges.map((c) => (
                  <ChallengeCard
                    key={c.concept_name}
                    challenge={c}
                    isPending={pending.has(c.concept_name)}
                    onAccept={handleAccept}
                    onDismiss={handleDismiss}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
