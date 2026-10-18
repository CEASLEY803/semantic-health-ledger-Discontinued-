'use client';

import { useCallback, useEffect, useState } from 'react';
import { useEventBus, type LedgerEvent } from '@/hooks/useEventBus';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

export interface TranscriptDocument {
  id: string;
  filename: string;
  source_label: string | null;
  status: 'pending' | 'processing' | 'complete' | 'failed';
  total_chunks: number;
  error: string | null;
  created_at: string;
}

interface UseTranscriptIngestResult {
  documents: TranscriptDocument[];
  uploadFile: (file: File) => Promise<void>;
  deleteDocument: (id: string) => Promise<void>;
  refresh: () => Promise<void>;
  isUploading: boolean;
  error: string | null;
}

// Tracks the live processing stage per document, pushed over the SSE event bus.
export type TranscriptStage =
  | 'chunking'
  | 'embedding'
  | 'extracting_facts'
  | 'complete'
  | 'failed';

export function useTranscriptIngest(): UseTranscriptIngestResult & {
  stages: Record<string, TranscriptStage>;
} {
  const [documents, setDocuments] = useState<TranscriptDocument[]>([]);
  const [stages, setStages] = useState<Record<string, TranscriptStage>>({});
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${BASE_URL}/api/v1/transcript/documents`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      const data = (await res.json()) as { documents: TranscriptDocument[] };
      setDocuments(data.documents ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load documents');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Live progress: update the per-doc stage and refresh the list on terminal states.
  useEventBus(
    useCallback(
      (event: LedgerEvent) => {
        if (event.type !== 'TRANSCRIPT_PROGRESS') return;
        const { document_id, stage } = event.payload as {
          document_id?: string;
          stage?: TranscriptStage;
        };
        if (!document_id || !stage) return;
        setStages((prev) => ({ ...prev, [document_id]: stage }));
        if (stage === 'complete' || stage === 'failed') void refresh();
      },
      [refresh],
    ),
  );

  const uploadFile = useCallback(
    async (file: File) => {
      setIsUploading(true);
      setError(null);
      try {
        const form = new FormData();
        form.append('file', file, file.name);
        const res = await fetch(`${BASE_URL}/api/v1/transcript/upload`, {
          method: 'POST',
          body: form,
        });
        if (!res.ok) {
          const body = await res.text().catch(() => res.statusText);
          throw new Error(`API ${res.status}: ${body}`);
        }
        await refresh();
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        setError(message);
        throw err;
      } finally {
        setIsUploading(false);
      }
    },
    [refresh],
  );

  const deleteDocument = useCallback(
    async (id: string) => {
      try {
        const res = await fetch(`${BASE_URL}/api/v1/transcript/${id}`, {
          method: 'DELETE',
        });
        if (!res.ok) throw new Error(`API ${res.status}`);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Delete failed');
      }
    },
    [refresh],
  );

  return { documents, stages, uploadFile, deleteDocument, refresh, isUploading, error };
}
