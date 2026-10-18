// ── Client-side frontend event bus ────────────────────────────────────────────
// Lightweight pub/sub for events that originate in the frontend and need to
// cross component boundaries without prop drilling or a global store.
//
// Separate from hooks/useEventBus.ts which handles SSE events streamed from
// the backend server over a persistent EventSource connection.

export interface JournalReplyPayload {
  question: string;
}

// Add new event types here as the app grows.
type PayloadMap = {
  INITIATE_JOURNAL_REPLY: JournalReplyPayload;
};

export type LocalEventType = keyof PayloadMap;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Handler<T extends LocalEventType> = (payload: PayloadMap[T]) => void;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const _handlers = new Map<LocalEventType, Set<Handler<any>>>();

export const bus = {
  /** Dispatch an event to all current subscribers. */
  emit<T extends LocalEventType>(type: T, payload: PayloadMap[T]): void {
    _handlers.get(type)?.forEach((fn) => fn(payload));
  },

  /**
   * Subscribe to an event type.
   * @returns An unsubscribe function; call it in a useEffect cleanup or similar.
   */
  on<T extends LocalEventType>(type: T, handler: Handler<T>): () => void {
    if (!_handlers.has(type)) _handlers.set(type, new Set());
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (_handlers.get(type) as Set<Handler<any>>).add(handler as Handler<any>);
    return () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (_handlers.get(type) as Set<Handler<any>> | undefined)?.delete(handler as Handler<any>);
    };
  },
};
