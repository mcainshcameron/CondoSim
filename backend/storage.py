"""Postgres persistence for runs."""
from __future__ import annotations

from .db import pool
from .models import RunState


async def save_run(state: RunState) -> None:
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


async def list_runs() -> list[str]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "select run_id from runs order by updated_at desc"
        )
    return [r["run_id"] for r in rows]
