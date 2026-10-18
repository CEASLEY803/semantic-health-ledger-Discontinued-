'use client';

import { useCallback, useState } from 'react';
import type { ChatIngestResponse, CommittedCounts } from '@/lib/types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

interface UsePdfIngestResult {
  uploadPath: (path: string) => Promise<ChatIngestResponse>;
  uploadFile: (file: File) => Promise<ChatIngestResponse>;
  isPending: boolean;
  statusMsg: string | null;
  streamingText: string | null;
  error: string | null;
}

export function usePdfIngest(
  onSuccess?: (response: ChatIngestResponse) => void,
): UsePdfIngestResult {
  const [isPending, setIsPending] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // ── Shared SSE reader ────────────────────────────────────────────────────────
  const _readSseStream = useCallback(
    async (res: Response): Promise<ChatIngestResponse> => {
      if (!res.body) throw new Error('No response body from server');

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accText = '';
      let committed: CommittedCounts | null = null;

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
          else if (event.type === 'chunk') {
            accText += event.text as string;
            setStreamingText(accText);
          }
          else if (event.type === 'done') committed = (event.committed as CommittedCounts) ?? null;
          else if (event.type === 'error') throw new Error(event.msg as string);
        }
      }

      const response: ChatIngestResponse = {
        status: 'success',
        reply: accText,
        committed,
      };
      onSuccess?.(response);
      return response;
    },
    [onSuccess],
  );

  // ── Path-based upload (Tauri native drop → OS path) ──────────────────────────
  const uploadPath = useCallback(
    async (path: string): Promise<ChatIngestResponse> => {
      setIsPending(true);
      setError(null);
      setStatusMsg(null);
      setStreamingText(null);

      try {
        const res = await fetch(`${BASE_URL}/api/v1/upload/pdf-path`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path }),
        });

        if (!res.ok) {
          const body = await res.text().catch(() => res.statusText);
          throw new Error(`API ${res.status}: ${body}`);
        }
        return await _readSseStream(res);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Unknown error';
        setError(message);
        throw err;
      } finally {
        setIsPending(false);
        setStatusMsg(null);
        setStreamingText(null);
      }
    },
    [_readSseStream],
  );

  // ── File-based upload (HTML5 drop → File object → multipart) ─────────────────
  const uploadFile = useCallback(
    async (file: File): Promise<ChatIngestResponse> => {
      setIsPending(true);
      setError(null);
      setStatusMsg(null);
      setStreamingText(null);

      try {
        const formData = new FormData();
        formData.append('file', file, file.name);

        const res = await fetch(`${BASE_URL}/api/v1/upload/pdf`, {
          method: 'POST',
          body: formData,
        });

        if (!res.ok) {
          const body = await res.text().catch(() => res.statusText);
          throw new Error(`API ${res.status}: ${body}`);
        }
        return await _readSseStream(res);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Unknown error';
        setError(message);
        throw err;
      } finally {
        setIsPending(false);
        setStatusMsg(null);
        setStreamingText(null);
      }
    },
    [_readSseStream],
  );

  return { uploadPath, uploadFile, isPending, statusMsg, streamingText, error };
}
