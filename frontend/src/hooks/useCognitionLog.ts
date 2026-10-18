'use client';

import { useCallback, useEffect, useState } from 'react';
import { useEventBus } from './useEventBus';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

export interface CognitionLogEntry {
  id: string;
  kind: 'idle' | 'observation' | 'spoke';
  salience: number;
  summary: string;
  detail: string | null;
  thread_id: string | null;
  created_at: string;
}

export interface CognitionThread {
  id: string;
  signature: string;
  title: string;
  status: string;
  salience: number;
  detail: string | null;
  opened_at: string;
  last_considered_at: string;
  last_surfaced_at: string | null;
}

/**
 * Fetches LEDGER's inner-monologue stream + open threads from the cognition loop,
 * and re-fetches live whenever the backend emits a COGNITION_TICK or LEDGER_THOUGHT.
 */
export function useCognitionLog(limit = 60) {
  const [log, setLog] = useState<CognitionLogEntry[]>([]);
  const [threads, setThreads] = useState<CognitionThread[]>([]);
  const [isLoading, setLoading] = useState(true);

  const refetch = useCallback(async () => {
    try {
      const res = await fetch(`${BASE_URL}/api/v1/cognition/log?limit=${limit}`);
      if (!res.ok) return;
      const data = (await res.json()) as { log?: CognitionLogEntry[]; threads?: CognitionThread[] };
      setLog(data.log ?? []);
      setThreads(data.threads ?? []);
    } catch {
      // backend offline: keep whatever we have
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  useEventBus(
    useCallback(
      (event) => {
        if (event.type === 'COGNITION_TICK' || event.type === 'LEDGER_THOUGHT') void refetch();
      },
      [refetch],
    ),
  );

  return { log, threads, isLoading, refetch };
}
