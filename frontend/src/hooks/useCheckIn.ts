'use client';

import { useCallback, useState } from 'react';
import type { ChatIngestResponse } from '@/lib/types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

interface CheckInPayload {
  days: number;
  focus?: string;
}

interface UseCheckInResult {
  run: (payload: CheckInPayload) => Promise<ChatIngestResponse>;
  isPending: boolean;
  statusMsg: string | null;
  streamingText: string | null;
  error: string | null;
}

export function useCheckIn(): UseCheckInResult {
  const [isPending, setIsPending] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (payload: CheckInPayload): Promise<ChatIngestResponse> => {
    setIsPending(true);
    setError(null);
    setStatusMsg(null);
    setStreamingText(null);

    try {
      const res = await fetch(`${BASE_URL}/api/v1/check-in`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const body = await res.text().catch(() => res.statusText);
        throw new Error(`API ${res.status}: ${body}`);
      }
      if (!res.body) throw new Error('No response body from server');

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accText = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() ?? '';

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;
          let event: Record<string, unknown>;
          try { event = JSON.parse(line.slice(6)); } catch { continue; }

          if (event.type === 'status') setStatusMsg(event.msg as string);
          else if (event.type === 'chunk') { accText += event.text as string; setStreamingText(accText); }
          else if (event.type === 'error') throw new Error(event.msg as string);
        }
      }

      return { status: 'success', reply: accText, committed: null };
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unknown error';
      setError(message);
      throw err;
    } finally {
      setIsPending(false);
      setStatusMsg(null);
      setStreamingText(null);
    }
  }, []);

  return { run, isPending, statusMsg, streamingText, error };
}
