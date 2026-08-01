-- Month-to-date LLM spend, shared across every run.
--
-- One row per calendar month. `backend/llm.py` increments it after each
-- completion and reads it in the pre-call budget guard, so the app halts
-- itself before the OpenRouter key-level credit cap is reached.
create table if not exists llm_spend (
  month      text primary key,          -- 'YYYY-MM' (UTC)
  cost_usd   double precision not null default 0,
  updated_at timestamptz      not null default now()
);
