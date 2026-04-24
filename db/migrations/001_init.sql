-- Condominio schema.
-- Apply by pasting into the Supabase SQL Editor, or via `psql $DATABASE_URL -f db/migrations/001_init.sql`.
-- backend/db.py runs this on startup too (idempotent).

create table if not exists runs (
    run_id      text primary key,
    state       jsonb not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists runs_updated_at_idx on runs (updated_at desc);

create table if not exists agent_memory (
    run_id      text not null references runs (run_id) on delete cascade,
    agent_id    text not null,
    content     text not null,
    updated_at  timestamptz not null default now(),
    primary key (run_id, agent_id)
);

create index if not exists agent_memory_run_idx on agent_memory (run_id);
