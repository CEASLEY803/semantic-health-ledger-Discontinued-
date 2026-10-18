'use client';

import { useEffect, useRef } from 'react';

// ── Event types ───────────────────────────────────────────────────────────────

export type LedgerEventType =
  | 'connected'
  | 'DATA_LOGGED'
  | 'SYNTHESIS_COMPLETE'
  | 'UI_COMMAND'
  | 'PROPOSAL_CREATED'
  | 'TRANSCRIPT_PROGRESS'
  | 'LEDGER_THOUGHT'
  | 'COGNITION_TICK';

export interface LedgerEvent {
  type: LedgerEventType;
  payload: Record<string, unknown>;
  ts: string;
}

export type EventBusHandler = (event: LedgerEvent) => void;

// ── Module-level SSE singleton ────────────────────────────────────────────────
// A single EventSource is shared across all useEventBus callers so the app
// maintains exactly one SSE connection to the backend regardless of how many
// components or hooks subscribe.

const _subscribers = new Set<EventBusHandler>();
let _es: EventSource | null = null;
let _retryTimer: ReturnType<typeof setTimeout> | null = null;
let _retryDelay = 1_000; // ms (doubles on each failure, capped at 30 s)

const SSE_URL = `${process.env.NEXT_PUBLIC_API_URL ?? ''}/api/v1/events/stream`;

function _teardown() {
  if (_es) {
    _es.close();
    _es = null;
  }
}

function _connect() {
  if (_es) return; // already live: nothing to do

  _es = new EventSource(SSE_URL);

  _es.onmessage = (e: MessageEvent<string>) => {
    try {
      const event = JSON.parse(e.data) as LedgerEvent;
      if (!event.type) return; // malformed: ignore
      _retryDelay = 1_000; // reset backoff on any valid message
      _subscribers.forEach((fn) => fn(event));
    } catch {
      // JSON parse error: swallow silently
    }
  };

  _es.onerror = () => {
    _teardown();
    if (_retryTimer) clearTimeout(_retryTimer);
    _retryTimer = setTimeout(() => {
      _retryDelay = Math.min(_retryDelay * 2, 30_000);
      _connect();
    }, _retryDelay);
  };
}

// ── Hook ──────────────────────────────────────────────────────────────────────

/**
 * Subscribe to the LEDGER SSE event bus.
 *
 * All calls share one underlying EventSource connection and are safe to call from any
 * number of components or hooks simultaneously. The handler is called for every
 * event received, including the initial `"connected"` confirmation.
 *
 * Use a stable callback (via useCallback with empty deps, or the useRef pattern
 * used internally here) to avoid unnecessary re-subscriptions.
 *
 * @example
 * useEventBus(useCallback((event) => {
 *   if (event.type === 'DATA_LOGGED') setKey((k) => k + 1);
 * }, []));
 */
export function useEventBus(onEvent: EventBusHandler): void {
  // Keep a ref so the module-level subscriber always calls the latest handler
  // without needing to re-subscribe on every render.
  const handlerRef = useRef<EventBusHandler>(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    // Create a stable wrapper that delegates to the latest ref value.
    const stable: EventBusHandler = (event) => handlerRef.current(event);

    _subscribers.add(stable);
    _connect(); // no-op if already connected

    return () => {
      _subscribers.delete(stable);
      // Do NOT close the EventSource here because other subscribers may still be active.
      // The connection stays open as long as at least one subscriber exists.
    };
  }, []); // stable identity: the handlerRef pattern handles handler changes
}
