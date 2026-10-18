'use client';

import { useState } from 'react';
import { saveRegimenBatch, deleteRegimenByName } from '@/lib/api';
import type { RegimenProposal } from '@/lib/types';

interface ProposalCardProps {
  proposal: RegimenProposal;
  onApprove: () => void;
  onReject: () => void;
}

// Route abbreviations for a compact display
const ROUTE_ABBREV: Record<string, string> = {
  oral:           'oral',
  sublingual:     'SL',
  transdermal:    'TD',
  intranasal:     'IN',
  other:          'other',
};

export function ProposalCard({ proposal, onApprove, onReject }: ProposalCardProps) {
  const [busy, setBusy] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  const hasAdditions = proposal.additions.length > 0;
  const hasRemovals  = proposal.removals.length > 0;

  const handleApprove = async () => {
    setBusy(true);
    setApplyError(null);
    try {
      // Apply additions
      if (hasAdditions) {
        await saveRegimenBatch(proposal.additions);
      }
      // Apply removals sequentially.
      // 404 → already removed or never existed; treat as idempotent success.
      for (const r of proposal.removals) {
        try {
          await deleteRegimenByName(r.supplement_name, r.time_of_day);
        } catch (err) {
          if (err instanceof Error && err.message.includes('404')) continue;
          throw err;
        }
      }
      onApprove();
    } catch (err) {
      setApplyError(err instanceof Error ? err.message : 'Apply failed');
      setBusy(false);
    }
  };

  const handleReject = () => {
    if (busy) return;
    onReject();
  };

  return (
    <div className="mx-3 mb-2 border border-zinc-700 bg-zinc-900 ring-1 ring-inset ring-zinc-700/40">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-zinc-800 px-4 py-2.5">
        <span className="h-1.5 w-1.5 flex-none rounded-full bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.6)]" />
        <span className="font-mono text-[0.6875rem] tracking-widest text-amber-400/80 uppercase">
          Pending Regimen Change
        </span>
        <span className="ml-auto font-mono text-[0.625rem] tracking-widest text-zinc-600 uppercase">
          Awaiting Approval
        </span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-3">
        {/* Additions */}
        {hasAdditions && (
          <div>
            <p className="mb-1.5 font-mono text-[0.625rem] tracking-widest text-emerald-500/70 uppercase">
              Add / Update ({proposal.additions.length})
            </p>
            <div className="space-y-1">
              {proposal.additions.map((item, i) => {
                const route = ROUTE_ABBREV[item.route] ?? item.route;
                const tod   = item.time_of_day !== 'as_needed' ? item.time_of_day : 'as needed';
                return (
                  <div
                    key={i}
                    className="flex items-start gap-2 border-l-2 border-emerald-500/40 bg-emerald-500/[0.03] py-1.5 pl-2.5 pr-3"
                  >
                    <span className="mt-px font-mono text-[0.625rem] font-semibold tabular-nums text-emerald-600/50">
                      +
                    </span>
                    <div className="min-w-0">
                      <p className="font-mono text-xs font-semibold text-emerald-300">
                        {item.supplement_name}
                      </p>
                      <p className="font-mono text-[0.6875rem] text-zinc-400">
                        {item.dose_value} {item.dose_unit} {route} &middot; {tod} &middot; {item.frequency}
                        {item.days_of_week ? ` (${item.days_of_week})` : ''}
                      </p>
                      {item.notes && (
                        <p className="mt-0.5 font-mono text-[0.625rem] text-zinc-600">{item.notes}</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Removals */}
        {hasRemovals && (
          <div>
            <p className="mb-1.5 font-mono text-[0.625rem] tracking-widest text-rose-500/70 uppercase">
              Remove ({proposal.removals.length})
            </p>
            <div className="space-y-1">
              {proposal.removals.map((item, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 border-l-2 border-rose-500/40 bg-rose-500/[0.03] py-1.5 pl-2.5 pr-3"
                >
                  <span className="font-mono text-[0.625rem] font-semibold text-rose-600/50">−</span>
                  <p className="font-mono text-xs font-semibold text-rose-300">
                    {item.supplement_name}
                    {item.time_of_day ? (
                      <span className="ml-1.5 font-normal text-zinc-500">
                        ({item.time_of_day})
                      </span>
                    ) : null}
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Error */}
        {applyError && (
          <p className="font-mono text-[0.6875rem] text-rose-400">{applyError}</p>
        )}
      </div>

      {/* Action buttons */}
      <div className="flex items-center justify-end gap-2 border-t border-zinc-800 px-4 py-2.5">
        <button
          onClick={handleReject}
          disabled={busy}
          className="font-mono text-[0.6875rem] tracking-widest text-rose-500 hover:text-rose-300 transition-colors disabled:opacity-40"
        >
          REJECT
        </button>
        <button
          onClick={handleApprove}
          disabled={busy}
          className="border border-emerald-600/50 bg-emerald-500/10 px-3 py-1 font-mono text-[0.6875rem] tracking-widest text-emerald-400 hover:bg-emerald-500/20 hover:text-emerald-300 transition-colors disabled:opacity-40"
        >
          {busy ? 'APPLYING…' : 'APPROVE'}
        </button>
      </div>
    </div>
  );
}
