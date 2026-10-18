"""Semantic Event Bus: lightweight async pub/sub dispatcher.

Provides a module-level singleton (``bus``) that any FastAPI route can emit to.
Multiple SSE subscribers (frontend connections, background workers) each get their
own asyncio.Queue so events fan out to all active listeners simultaneously.

Usage
-----
Emitting (from any async route handler)::

    from event_bus import bus
    await bus.emit("DATA_LOGGED", {"source": "supplement"})

Subscribing (SSE endpoint)::

    async for event in bus.subscribe():
        if event is None:           # heartbeat tick
            yield ": heartbeat\\n\\n"
        else:
            yield f"data: {json.dumps(event)}\\n\\n"
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)


class EventBus:
    """Fan-out async pub/sub bus backed by per-subscriber asyncio.Queue.

    Design notes
    ~~~~~~~~~~~~
    * All I/O is asyncio-native, with no threading required.
    * ``emit()`` uses ``put_nowait()`` so it never blocks the event loop.
      If a subscriber's queue is full (client too slow) its queue is dropped and
      removed from the subscriber list so it cannot accumulate back-pressure.
    * ``subscribe()`` yields ``None`` every *heartbeat_interval* seconds so
      long-lived SSE connections don't time out on idle systems.
    * List mutations (append / remove) are safe without a lock because asyncio
      is single-threaded and we never mutate the list inside an ``await``.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue] = []

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to every active subscriber.

        Non-blocking: uses ``put_nowait()``.  Subscribers whose queues are full
        are silently evicted because a slow or stuck client should not stall other readers.
        """
        if not self._subscribers:
            return  # fast path when no listeners are connected

        event: dict[str, Any] = {
            "type": event_type,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        evicted: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "[event_bus] subscriber queue full, evicting slow subscriber"
                )
                evicted.append(q)

        for q in evicted:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

        logger.debug(
            "[event_bus] emitted %s to %d subscriber(s)",
            event_type,
            len(self._subscribers),
        )

    def emit_sync(self, event_type: str, payload: dict[str, Any]) -> None:
        """Thread-safe synchronous emit for use from non-async contexts.

        Bridges from a daemon thread (e.g. a tool callback inside a
        ``ProcessPoolExecutor``) back to the asyncio event loop via
        ``asyncio.run_coroutine_threadsafe()``.

        Requires :func:`register_main_loop` to have been called at startup.
        Drops the event silently if the main loop has not been registered yet
        (can happen during early startup before the lifespan hook fires).

        Parameters
        ----------
        event_type:
            Arbitrary event label string (e.g. ``"UI_COMMAND"``).
        payload:
            JSON-serialisable dict of event data.
        """
        loop = _main_loop
        if loop is None or not loop.is_running():
            logger.warning(
                "[event_bus] emit_sync called before main loop registered; "
                "dropping %s",
                event_type,
            )
            return
        future = asyncio.run_coroutine_threadsafe(
            self.emit(event_type, payload), loop
        )
        try:
            future.result(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[event_bus] emit_sync error for %s: %s", event_type, exc
            )

    # ── Subscribe ─────────────────────────────────────────────────────────────

    async def subscribe(
        self, heartbeat_interval: float = 25.0
    ) -> AsyncGenerator[dict[str, Any] | None, None]:
        """Async generator that yields events as they arrive.

        Yields ``None`` every *heartbeat_interval* seconds when the bus is quiet
        so the caller can send an SSE keep-alive comment (``": heartbeat\\n\\n"``).

        The subscriber queue is registered on entry and automatically removed in
        the ``finally`` block when the caller disconnects or raises.

        Parameters
        ----------
        heartbeat_interval:
            Seconds between keep-alive ticks when no events are queued.
            Default 25 s, which stays safely under most proxy and browser SSE timeouts (30 s).
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(q)
        active = len(self._subscribers)
        logger.info("[event_bus] +subscriber (%d active)", active)

        try:
            while True:
                try:
                    # Wait up to heartbeat_interval for an event; yield None on timeout.
                    # asyncio.wait_for cancels q.get() on timeout, so the item (if any)
                    # remains in the queue and will be retrieved on the next iteration.
                    event = await asyncio.wait_for(
                        q.get(), timeout=heartbeat_interval
                    )
                    yield event
                except asyncio.TimeoutError:
                    yield None  # heartbeat signal to the caller
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            remaining = len(self._subscribers)
            logger.info("[event_bus] -subscriber (%d remaining)", remaining)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def subscriber_count(self) -> int:
        """Number of currently active subscribers (primarily for telemetry)."""
        return len(self._subscribers)


# ── Async-bridge helpers ──────────────────────────────────────────────────────
# emit_sync() (on EventBus above) needs a reference to the running event loop
# so it can call asyncio.run_coroutine_threadsafe().  Call register_main_loop()
# once at application startup in the FastAPI lifespan hook.

_main_loop: asyncio.AbstractEventLoop | None = None


def register_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Store the running asyncio event loop for use by :meth:`EventBus.emit_sync`.

    Must be called once during application startup, e.g.::

        register_main_loop(asyncio.get_running_loop())

    After this call, any code running in a daemon thread can call
    ``bus.emit_sync(...)`` and events will be correctly delivered to all SSE
    subscribers on the main thread.
    """
    global _main_loop
    _main_loop = loop
    logger.info("[event_bus] main asyncio loop registered: %s", loop)


# ── Module-level singleton ────────────────────────────────────────────────────
# Import and use directly:  ``from event_bus import bus``

bus = EventBus()
