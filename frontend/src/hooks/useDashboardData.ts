'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  fetchBiometrics,
  fetchSupplements,
  fetchJournals,
  fetchLabResults,
  fetchRegimen,
  fetchSynthesisStatus,
} from '@/lib/api';
import type { BiometricLog, SupplementLog, DailyJournal, LabResult, RegimenItem } from '@/lib/types';
import { useEventBus } from './useEventBus';

interface DashboardData {
  labs: LabResult[];
  biometrics: BiometricLog[];
  regimen: RegimenItem[];
  supplementLogs: SupplementLog[];
  journals: DailyJournal[];
  /** ISO date string (YYYY-MM-DD) of the last synthesis run, or null if never run. */
  synthesisDate: string | null;
}

interface UseDashboardDataResult extends DashboardData {
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
}

const EMPTY: DashboardData = {
  labs: [],
  biometrics: [],
  regimen: [],
  supplementLogs: [],
  journals: [],
  synthesisDate: null,
};

// manualKey is incremented by refetch() for explicit UI-triggered refreshes (e.g. delete).
// busKey is incremented by the SSE event bus whenever DATA_LOGGED or SYNTHESIS_COMPLETE
// arrives; this replaces the old refreshKey prop that page.tsx used to thread down.
export function useDashboardData(): UseDashboardDataResult {
  const [data, setData] = useState<DashboardData>(EMPTY);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [manualKey, setManualKey] = useState(0);
  const [busKey, setBusKey] = useState(0);

  const refetch = useCallback(() => setManualKey((k) => k + 1), []);

  // Subscribe to the SSE event bus.  Any DATA_LOGGED or SYNTHESIS_COMPLETE event
  // triggers a full re-fetch so the dashboard stays in sync without polling.
  useEventBus(useCallback((event) => {
    if (event.type === 'DATA_LOGGED' || event.type === 'SYNTHESIS_COMPLETE') {
      setBusKey((k) => k + 1);
    }
  }, []));

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    Promise.all([
      fetchLabResults(),
      fetchBiometrics(),
      fetchRegimen(),
      fetchSupplements(),
      fetchJournals(),
      fetchSynthesisStatus().catch(() => null),  // non-fatal: badge just stays dark
    ])
      .then(([labs, biometrics, regimen, supplementLogs, journals, synthStatus]) => {
        if (!cancelled) {
          setData({
            labs, biometrics, regimen, supplementLogs, journals,
            synthesisDate: synthStatus?.last_synthesis_date ?? null,
          });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load dashboard data');
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [manualKey, busKey]);

  return { ...data, isLoading, error, refetch };
}
