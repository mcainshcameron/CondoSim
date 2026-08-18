"""Per-run event bus for live UI streaming via Server-Sent Events.

Events published to a run bus are consumed by the frontend EventSource. They
drive the typing indicator, live message appends, and day lifecycle UI.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from time import time
from typing import Any


# Replay window for new SSE subscribers. Covers the race where the browser's
# EventSource is mid-handshake while the backend has already published events
# (e.g. a fresh run auto-advancing while the page is still mounting). The
# frontend dedupes by message id, so replays are idempotent.
REPLAY_BUFFER_SIZE = 200
REPLAY_TTL_SEC = 60.0

# Hard cap on simultaneous SSE streams for one run. Each subscriber owns a
# Queue(maxsize=500) and a `message_sent` event measures ~3.2 KB, so a backed-up
# stream can pin ~1.6 MB — on a 512 MB Heroku Eco dyno that is not free. Normal
# use is one browser tab per run; the headroom is for reconnect churn, where the
# server can briefly hold both the abandoned stream and its replacement (the old
# generator's `finally` only runs once Starlette notices the disconnect).
MAX_SUBSCRIBERS_PER_RUN = 16


class TooManySubscribers(RuntimeError):
    """Raised by `subscribe` when a run is already at MAX_SUBSCRIBERS_PER_RUN.

    Deliberately not an HTTPException — the bus knows nothing about HTTP.
    `api_run_events` translates it into a 429.
    """


@dataclass
class Event:
    type: str  # "message_sent" | "typing_start" | "typing_stop" | "day_start" | "day_end" | "state" | "error"
    run_id: str
    data: dict[str, Any]
    wall_clock: float

    def to_sse(self) -> str:
        payload = json.dumps({
            "type": self.type,
            "data": self.data,
            "t": self.wall_clock,
        }, ensure_ascii=False)
        return f"event: {self.type}\ndata: {payload}\n\n"


class RunEventBus:
    """Per-run pub/sub. Subscribers get a queue that receives new events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._recent: dict[str, deque[Event]] = defaultdict(
            lambda: deque(maxlen=REPLAY_BUFFER_SIZE)
        )
        # Per-run async lock prevents concurrent day-advances from trampling state.
        self._run_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def subscribe(self, run_id: str) -> tuple[asyncio.Queue[Event], list[Event]]:
        # `.get(run_id, ())` rather than `self._subscribers[run_id]`: reading
        # the defaultdict would mint the entry before we know we're keeping it.
        if len(self._subscribers.get(run_id, ())) >= MAX_SUBSCRIBERS_PER_RUN:
            raise TooManySubscribers(run_id)
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
        self._subscribers[run_id].add(q)
        cutoff = time() - REPLAY_TTL_SEC
        replay = [e for e in self._recent.get(run_id, ()) if e.wall_clock >= cutoff]
        return q, replay

    def unsubscribe(self, run_id: str, q: asyncio.Queue[Event]) -> None:
        self._subscribers.get(run_id, set()).discard(q)

    def publish(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        ev = Event(type=event_type, run_id=run_id, data=data, wall_clock=time())
        self._recent[run_id].append(ev)
        dead: list[asyncio.Queue[Event]] = []
        for q in list(self._subscribers.get(run_id, set())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(run_id, q)

    def lock(self, run_id: str) -> asyncio.Lock:
        return self._run_locks[run_id]


_BUS = RunEventBus()


def bus() -> RunEventBus:
    return _BUS
