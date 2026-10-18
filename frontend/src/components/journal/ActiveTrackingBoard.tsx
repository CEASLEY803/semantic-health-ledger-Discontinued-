'use client';

import { useEffect, useState } from 'react';
import { fetchActiveTracking } from '@/lib/api';
import type { ClinicalNode } from '@/lib/types';

interface ActiveTrackingBoardProps {
  refreshKey?: number;
}

export function ActiveTrackingBoard({ refreshKey }: ActiveTrackingBoardProps) {
  const [nodes, setNodes] = useState<ClinicalNode[]>([]);

  useEffect(() => {
    fetchActiveTracking()
      .then(setNodes)
      .catch(() => {/* silently ignore; board simply stays empty */});
  }, [refreshKey]);

  if (nodes.length === 0) return null;

  return (
    <div className="flex flex-col gap-3">
      <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400 uppercase">
        Pharmacokinetics &amp; Clearance
      </span>

      {nodes.map((node) => (
        <div
          key={node.concept_name}
          className="flex flex-col gap-3 border border-zinc-800 bg-zinc-900 p-4"
        >
          {/* Header row */}
          <div className="flex flex-wrap items-start justify-between gap-2">
            <span className="font-medium text-zinc-200 text-sm">
              {node.concept_name}
            </span>
            {node.expires_at && (
              <span className="border border-amber-500/20 bg-amber-500/[0.06] px-2 py-0.5 font-mono text-[0.6875rem] tracking-wide text-amber-400">
                Target Clearance: {node.expires_at}
              </span>
            )}
          </div>

          {/* Summary text: AI generates bullet points; preserve whitespace */}
          <p className="whitespace-pre-wrap text-sm text-zinc-400 leading-relaxed">
            {node.summary_text}
          </p>
        </div>
      ))}
    </div>
  );
}
