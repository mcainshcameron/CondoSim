"""Postgres persistence for runs."""
from __future__ import annotations

import asyncio
from collections import defaultdict

from . import timeline
from .db import has_database, pool
from .models import RunState


# In-memory fallback used when DATABASE_URL is unset (local dev / offline
# tests). Stores the serialized JSON exactly like Postgres does, so a run
# round-trips through the same validation path in both modes.
_MEM_RUNS: dict[str, str] = {}


# Per-run save mutex. Prevents concurrent save_run calls (scheduler batch +
# admin endpoint hitting at the same time) from overwriting each other: both
# coroutines call state.model_dump_json() synchronously, then await the DB
# write; whoever finishes writing LAST wins. The mutex serializes saves so
# the last serializer (inside the lock) always captures the combined state.
_save_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Per-run *mutation* mutex. Coarser than _save_locks: this serializes the
# whole load -> mutate -> save critical section across admin endpoints and
# the day-loop background task. Without it, two coroutines can each read
# state from the DB, mutate independent copies, and the second writer wipes
# the first writer's changes — even with _save_locks serializing the write
# itself. Acquire this BEFORE _get_run/load_run in any path that mutates.
_state_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def state_lock(run_id: str) -> asyncio.Lock:
    """Per-run lock to serialize the read-modify-write window."""
    return _state_locks[run_id]


async def save_run(state: RunState) -> None:
    async with _save_locks[state.run_id]:
        payload = state.model_dump_json()
        if not has_database():
            _MEM_RUNS[state.run_id] = payload
            return
        async with pool().acquire() as conn:
            await conn.execute(
                """
                insert into runs (run_id, state, updated_at)
                values ($1, $2::jsonb, now())
                on conflict (run_id) do update
                  set state = excluded.state,
                      updated_at = now()
                """,
                state.run_id,
                payload,
            )


async def run_exists(run_id: str) -> bool:
    """Cheap "is this a real run id?" probe.

    Exists so the API can 404 an unknown id *before* touching any of the
    `defaultdict`s keyed by run id (`_state_locks` here, `_subscribers` /
    `_recent` / `_run_locks` in events.py) — reading one of those with a
    garbage id mints an entry that is never evicted. The mutating endpoints
    can't just call `load_run` first, because they have to hold
    `state_lock(run_id)` *around* the load (see `_get_run_for_mutation`).

    Deliberately not `load_run(...) is not None`: this never deserializes the
    run, so the extra round trip stays cheap enough to sit in front of every
    admin action.
    """
    if not has_database():
        return run_id in _MEM_RUNS
    async with pool().acquire() as conn:
        row = await conn.fetchrow("select 1 from runs where run_id = $1", run_id)
    return row is not None


async def load_run(run_id: str) -> RunState | None:
    if not has_database():
        payload = _MEM_RUNS.get(run_id)
        state = RunState.model_validate_json(payload) if payload else None
    else:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                "select state::text as state from runs where run_id = $1",
                run_id,
            )
        state = RunState.model_validate_json(row["state"]) if row is not None else None
    if state is not None:
        # Runs saved before Message.seq existed deserialize with seq=0 for
        # every message, which would collapse the tiebreaker. Reconstruct a
        # stable order once, on load.
        timeline.backfill_seq(state)
    return state


