"""Postgres persistence for runs."""
from __future__ import annotations

import asyncio
from collections import defaultdict

from .db import pool
from .models import RunState


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


async def load_run(run_id: str) -> RunState | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "select state::text as state from runs where run_id = $1",
            run_id,
        )
    if row is None:
        return None
    return RunState.model_validate_json(row["state"])


