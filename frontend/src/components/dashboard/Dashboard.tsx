'use client';

import { useCallback, useState } from 'react';
import { useDashboardData } from '@/hooks/useDashboardData';
import { useEventBus } from '@/hooks/useEventBus';
import LabsPanel from './LabsPanel';
import BiometricsPanel from './BiometricsPanel';
import SupplementsPanel from './SupplementsPanel';
import JournalPanel from './JournalPanel';
import NetworkGraph from '@/components/knowledge/NetworkGraph';
import TranscriptImport from '@/components/knowledge/TranscriptImport';
import MindPanel from '@/components/cognition/MindPanel';
import type { LedgerEvent } from '@/hooks/useEventBus';

type Tab = 'labs' | 'biometrics' | 'supplements' | 'journal' | 'knowledge' | 'mind';

const TABS: { id: Tab; label: string }[] = [
  { id: 'labs', label: 'LABS' },
  { id: 'biometrics', label: 'BIOMETRICS' },
  { id: 'supplements', label: 'SUPPLEMENTS' },
  { id: 'journal', label: 'JOURNAL' },
  { id: 'knowledge', label: 'KNOWLEDGE' },
  { id: 'mind', label: 'MIND' },
];

// Maps backend tab names (uppercase) to the frontend Tab union type.
const TAB_MAP: Record<string, Tab> = {
  LABS:        'labs',
  BIOMETRICS:  'biometrics',
  SUPPLEMENTS:   'supplements',
  JOURNAL:     'journal',
  KNOWLEDGE:   'knowledge',
  MIND:        'mind',
};

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState<Tab>('labs');
  // Holds the search query injected by navigate_ui for the LABS panel.
  const [labSearch, setLabSearch] = useState('');
  // Survives NetworkGraph unmounting; bumped on SYNTHESIS_COMPLETE so the graph
  // re-fetches fresh data the moment the Knowledge tab is opened, even if synthesis
  // ran while the user was on a different tab.
  const [graphRefreshKey, setGraphRefreshKey] = useState(0);

  const { labs, biometrics, regimen, supplementLogs, journals, synthesisDate, isLoading, error, refetch } =
    useDashboardData();

  // ── SSE-driven navigation (Motor Cortex) + graph refresh ─────────────────
  // When the AI calls navigate_ui, a UI_COMMAND event arrives over the event
  // bus.  We switch the active tab and optionally pre-populate the search box.
  // SYNTHESIS_COMPLETE bumps graphRefreshKey so NetworkGraph re-fetches on next mount.
  useEventBus(useCallback((event: LedgerEvent) => {
    if (event.type === 'SYNTHESIS_COMPLETE') {
      setGraphRefreshKey((k) => k + 1);
      // useDashboardData already increments busKey on SYNTHESIS_COMPLETE, which
      // triggers its own re-fetch.  Calling refetch() here too would fire a second
      // parallel fetch waterfall; omit the redundant call.
      return;
    }
    if (event.type !== 'UI_COMMAND') return;
    const { tab, query } = event.payload as { tab?: string; query?: string };
    const next = TAB_MAP[tab ?? ''];
    if (next) {
      setActiveTab(next);
      if (next === 'labs') setLabSearch(query ?? '');
    }
  }, []));

  // ── Tab notification badges ───────────────────────────────────────────────
  // LABS: show rose dot when any result is explicitly flagged or numerically out of range.
  const hasFlag = labs.some((l) => {
    if (l.flagged) return true;
    const v  = l.value_numeric  !== null ? Number(l.value_numeric)  : null;
    const lo = l.reference_low  !== null ? Number(l.reference_low)  : null;
    const hi = l.reference_high !== null ? Number(l.reference_high) : null;
    if (v === null || isNaN(v)) return false;
    return (hi !== null && v > hi) || (lo !== null && v < lo);
  });
  // KNOWLEDGE: show cyan dot when synthesis ran on today's calendar date.
  const synthesizedToday =
    !isLoading && synthesisDate === new Date().toISOString().slice(0, 10);

  return (
    <div className="flex h-full flex-col">
      {/* Tab bar */}
      <nav className="flex shrink-0 border-b border-zinc-800 bg-zinc-950 px-4">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-3 font-mono text-[0.75rem] font-semibold tracking-widest transition-colors ${
              activeTab === tab.id
                ? 'border-b-2 border-cyan-400 bg-cyan-950/40 text-cyan-400'
                : 'text-zinc-400 hover:bg-white/[0.03] hover:text-zinc-200'
            }`}
          >
            {/* Label + optional notification dot */}
            <span className="inline-flex items-center gap-1.5">
              {tab.label}
              {tab.id === 'labs' && hasFlag && (
                <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
              )}
              {tab.id === 'knowledge' && synthesizedToday && (
                <span className="h-1.5 w-1.5 rounded-full bg-cyan-400" />
              )}
            </span>
          </button>
        ))}
      </nav>

      {error && (
        <div className="shrink-0 border-b border-rose-500/20 bg-rose-500/5 px-4 py-2 font-mono text-xs text-rose-400 tracking-wide">
          ERR // {error}
        </div>
      )}

      {/* Data panels: scrollable */}
      {activeTab !== 'knowledge' && activeTab !== 'mind' && (
        <div className="flex-1 overflow-y-auto p-4">
          {activeTab === 'labs'       && <LabsPanel labs={labs} isLoading={isLoading} onRefresh={refetch} externalSearch={labSearch} />}
          {activeTab === 'biometrics' && <BiometricsPanel biometrics={biometrics} isLoading={isLoading} onRefresh={refetch} />}
          {activeTab === 'supplements'  && <SupplementsPanel regimen={regimen} supplementLogs={supplementLogs} isLoading={isLoading} onRefresh={refetch} />}
          {activeTab === 'journal'    && <JournalPanel journals={journals} isLoading={isLoading} onRefresh={refetch} refreshKey={graphRefreshKey} />}
        </div>
      )}

      {/* Mind: LEDGER's cognition loop (open threads + inner monologue) */}
      {activeTab === 'mind' && (
        <div className="min-h-0 flex-1">
          <MindPanel />
        </div>
      )}

      {/* Knowledge: full-height canvas, no padding or scroll wrapper */}
      {activeTab === 'knowledge' && (
        <div className="relative flex-1 overflow-hidden">
          <NetworkGraph externalRefreshKey={graphRefreshKey} />
          <TranscriptImport />
        </div>
      )}
    </div>
  );
}
