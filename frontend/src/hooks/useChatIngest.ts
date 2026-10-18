'use client';

import { useCallback, useState } from 'react';
import type { ChatIngestResponse } from '@/lib/types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

interface UseChatIngestResult {
  submit: (text: string, loggingEnabled?: boolean) => Promise<ChatIngestResponse>;
  isPending: boolean;
  statusMsg: string | null;
  toolStatus: string | null;
  streamingText: string | null;
  error: string | null;
}

export function useChatIngest(
  sessionId: string,
  onSuccess?: (response: ChatIngestResponse) => void,
): UseChatIngestResult {
  const [isPending, setIsPending] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [toolStatus, setToolStatus] = useState<string | null>(null);
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (text: string, loggingEnabled = true): Promise<ChatIngestResponse> => {
      setIsPending(true);
      setError(null);
      setStatusMsg(null);
      setToolStatus(null);
      setStreamingText(null);

      try {
        const res = await fetch(`${BASE_URL}/api/v1/chat/ingest`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, logging_enabled: loggingEnabled, session_id: sessionId }),
        });

        if (!res.ok) {
          const body = await res.text().catch(() => res.statusText);
          throw new Error(`API ${res.status}: ${body}`);
        }
        if (!res.body) {
          throw new Error('No response body from server');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let accText = '';
        let committed: ChatIngestResponse['committed'] = null;
        let isSilent = false;
        let userMessageId: string | null = null;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // SSE events are separated by double newlines.
          const parts = buffer.split('\n\n');
          buffer = parts.pop() ?? '';

          for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith('data: ')) continue;
            let event: Record<string, unknown>;
            try {
              event = JSON.parse(line.slice(6));
            } catch {
              continue;
            }

            if (event.type === 'status') {
              const msg = event.msg as string;
              setStatusMsg(msg);
              if (msg.startsWith('Tool:')) {
                setToolStatus(msg.slice(5).trim());
              }
            } else if (event.type === 'chunk') {
              accText += event.text as string;
              setStreamingText(accText);
              setToolStatus(null);
            } else if (event.type === 'silent_receipt') {
              // AI executed an administrative task silently with no conversational text.
              isSilent = true;
              setStatusMsg(null);
              setToolStatus(null);
            } else if (event.type === 'done') {
              committed = (event.committed as ChatIngestResponse['committed']) ?? null;
              userMessageId = (event.user_message_id as string) ?? null;
            } else if (event.type === 'error') {
              throw new Error(event.msg as string);
            }
          }
        }

        const response: ChatIngestResponse = {
          status: 'success',
          reply: accText,
          committed,
          isSilent,
          userMessageId,
        };
        onSuccess?.(response);
        return response;
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Unknown error';
        setError(message);
        throw err;
      } finally {
        setIsPending(false);
        setStatusMsg(null);
        setToolStatus(null);
        setStreamingText(null);
      }
    },
    [onSuccess, sessionId],
  );

  return { submit, isPending, statusMsg, toolStatus, streamingText, error };
}
