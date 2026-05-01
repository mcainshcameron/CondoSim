# CondoSim v2 — Execution Plan

> **Mode shift:** This is no longer a reference design. This is the execution
> plan you'll ship against. It assumes:
>
> - **Heroku Eco €5/mo stays.** No host migration. (See "Why Heroku stays"
>   under §5.1.)
> - **Supabase Postgres stays** — switch to the **session pooler** URL.
> - **OpenRouter stays.** Three external services in the runtime path.
> - **Phased in-place migration**, not big-bang rewrite. v1 stays in prod
>   while v2 lands one phase at a time, each shippable to main.
> - **Realism + quality work is co-equal with substrate work**, not "later".
>   Half this document is about what makes the simulation *feel alive*, not
>   what makes it operationally clean.

---

## 1. Context — what this plan is solving

Two parallel problem sets, executed together:

### 1.1 Substrate / scheduling problems (operational)

| Problem | Where in v1 | What it costs you |
|---|---|---|
| 30s Heroku H12 timeout forces 202+background-task pattern | `backend/main.py:301-352` | Pattern works; reframe as design choice (round-robin needs >30s anyway). |
| Supabase transaction pooler forbids prepared statements | `backend/db.py:9-10` | One config-string fix. |
| Mobile Safari SSE needs custom reconnect + 5s watchdog | `App.jsx:1342-1386, 1549-1724` | UI fragility; watchdog burns API requests. |
| 1,983-line App.jsx + 6 closure-stale refs | `frontend/src/App.jsx` | Every UI change is risky; mobile bugs keep finding new corners. |
| RunState as JSONB blob; "what did agent X see at min 437" requires transcript archaeology | `backend/storage.py:32-46` | Tuning is blind. |
| Token cost goes to stderr only | `backend/openrouter.py`, IMPLEMENTATION.md §6.9 | Bills can surprise you. |
| No pytest — only `run_smoketest.py` (slow, paid) | `run_smoketest.py`, IMPLEMENTATION.md §6.10 | No fast feedback loop. |
| Day-end lock invariant lives in 4 lines of CLAUDE.md prose | CLAUDE.md "Day-end lock invariant" | Carries forever. |
| Engagement-roll batch parallelism creates near-duplicate messages, then a regex post-filter blocks them after tokens are burnt | `backend/scheduler.py`, `backend/tools.py:375-391` | Wasteful by design. |
| 14-day cap | App.jsx `day < 14` chain | Arbitrary; users want longer runs. |
| Per-month LLM spend uncapped | (absent) | Cost surprise vector. |

### 1.2 Quality + realism problems (product)

This is the new lens. As Chief Solutions Architect, the operational fixes
are necessary but **not sufficient**. The simulation works but feels:

- **Mechanical** — every agent posts roughly the same length, same cadence; characters don't have moods, energy levels, or time-of-day texture.
- **Hermetic** — only the chat exists. No weather, no power outages, no
  external events for residents to react to. Drama lives or dies on the
  admin's opening prompt.
- **Memory-heavy** — every day's diary is concatenated into the prompt
  context. By day 30, the prompt is enormous, expensive, and the model
  drowns in undifferentiated history. Memory should be *retrieved
  associatively*, not *dumped wholesale*.
- **Cost-blind under Anthropic-specific affordances** — SOUL is large and
  static and re-sent every activation. **Prompt caching** would cut
  per-activation cost dramatically and isn't being used.
- **Quality-blind** — there's no feedback signal that says "this run was
  good drama" vs. "this run was a ghost town." Tuning is by feel.
- **Onboarding-thin** — the setup screen drops a single empty textbox on
  the user. New operators don't know what makes a good opening.
- **Admin-poor** — admins can announce, DM, set goals, file motions. But
  they can't *shape the narrative* with time-skips, world events, or
  scenario presets.
- **Trust-flat** — trust is one scalar. Real relationships are
  multidimensional (practical trust ≠ emotional trust ≠ respect).

### 1.3 The intent of v2

Make the simulation **feel alive** — not just run cleanly. Every phase of
this plan touches both substrate and quality.

---

## 2. The realism + quality additions (Chief Solutions Architect lens)

These are the most impactful product additions, ordered roughly by
impact-per-effort. Each one is a phase or sub-phase below.

### 2.1 Prompt caching on SOUL (Anthropic-native)

**Why:** SOUL is ~15-30 lines of identity content, sent on every activation
(re-read fresh from disk per CLAUDE.md). Across a full run that's hundreds
of redundant token transmissions. Anthropic's prompt-cache feature makes
the SOUL block hit cache at ~10% of normal token cost and reduces TTFT by
30-50%. Free win. On non-Anthropic models that lack prompt caching, the
SOUL block is just sent normally — no degradation.

**Cost impact:** roughly 30-50% per-activation cost reduction on Anthropic
models. Across a long open-ended run, this is the difference between €5
and €15+ per run.

### 2.2 Memory RAG (associative retrieval)

**Why:** Open-ended runs make the memory-as-concat strategy break. By day
60, every activation prompt includes 60 days × 5 agents × ~500 tokens of
diary. That's 150K tokens of history per call. Associative retrieval keeps
the prompt small and *more* accurate: when agent X is about to message,
fetch the top-K diary entries semantically related to "what's happening
right now in the chat" rather than the entire history.

**Implementation:** embed each day's diary entry on consolidation (using
OpenAI text-embedding-3-small or Voyage's voyage-3-lite — both cheap), store
in a `memory_embeddings` table, retrieve at activation time with cosine
similarity against the current chat-state context. Top 10 entries
in-prompt; rest stored but not loaded.

### 2.3 Mood + energy drift on residents

**Why:** Real personalities have weather. Same person grumpy after a fight,
upbeat after good news, drained at the end of a chatty day. v1's SOUL is
static; mood is whatever leaks through MEMORY's diary writing.

**Implementation:** small per-agent state vector — `mood` (numeric ±1),
`energy` (0-100), `last_significant_event` (text). Updated at day_end as
part of consolidation. Injected into the notification prompt as a one-line
scene cue ("Sei un po' giù dopo la lite di ieri." / "Sei stanco, hai
risposto a 12 messaggi oggi.").

This is **not** SOUL drift — identity is still immutable. Mood is a
weather layer over the identity.

### 2.4 World event injection

**Why:** The condo only contains its own gossip. Real condos contain:
weather, broken elevators, noisy neighbors, postal slips, news on TV. v1
agents have nothing external to react to, so all drama is interpersonal —
which gets repetitive.

**Implementation:** a curated `world_events.json` content file with ~50
ambient events tagged by type, season, and likelihood ("È piovuto tutta la
notte", "Hanno tagliato l'acqua alle 14, ricomincia stasera", "Strano
viavai al cancello"). At each `day_started`, with probability ~0.4, sample
one event and post it to the main chat as a `system`-authored ambient
notice. Agents react organically.

This deletes the need for "the admin must constantly inject drama." The
world does it for them.

### 2.5 Quality eval LLM ("Direttore di scena")

**Why:** No feedback signal on whether a run was good drama. Tuning is by
gut feel.

**Implementation:** at run end (or daily), run a Sonnet-grade pass over
the day's transcript scoring: realism (0-10), narrative arc, character
distinctness, conflict-without-fabrication, dialog-quality. Stored in a
`run_quality_scores` table. Surfaced in admin UI as a per-day chart so you
can watch quality drift across long runs.

This becomes the regression signal for prompt changes. Replaces "did the
smoketest feel right?" with "did quality scores hold/move?".

### 2.6 Per-call model quality dial

**Why:** Right now every activation runs on Gemini Flash Lite. Memory
consolidation runs on Haiku. Some moments deserve a better model:

- Motion-close summaries: Sonnet-grade. (One call per motion close. ~10
  per run. Cost trivial.)
- Day-end consolidation if the day was eventful: Sonnet over Haiku.
- Trust-pivot moments (a big trust delta): Sonnet for that activation.
- Routine activations: stay on Flash Lite.

**Implementation:** `model_quality_tier` enum on each LLM call:
`'routine' | 'reflective' | 'highlight'`. Mapped to actual model IDs in
`config.py`. Cost guard treats highlight tier as ~5× the cost of routine.

### 2.7 Setup screen scaffolding

**Why:** A blank textarea is a high-friction first experience. Operators
don't know what produces good drama vs. ghost towns.

**Implementation:** the setup screen shows 6-8 curated opening-message
templates with difficulty labels ("Light: Holiday parking dispute" /
"Medium: A pet was found injured; nobody admits ownership" / "Hard:
Suspected embezzlement of the Christmas-decoration fund"). Each is a
one-paragraph prompt the operator can use as-is or edit. Templates live
in `data/openings.json`.

### 2.8 Admin analytics dashboard

**Why:** Operators want to see *what's happening* across a long run, not
just read every message. Sentiment timeline, drama spike detection,
trust-graph evolution, per-agent talk-time.

**Implementation:** a `/runs/:id/dashboard` route over the existing
projection tables. Sentiment is derived from a small classifier pass at
each `day_done` (cheap; Haiku-grade). Drama spike = activations per fic-min
peak detection. Trust graph = D3 force layout over `trust_matrix` view.
~600 LOC frontend, all read-only.

### 2.9 Multi-dimensional trust (deferred — flagged, not v2-launch)

**Why:** Real relationships have facets — practical trust, emotional
trust, respect, affection. v1's single scalar conflates them.

**Why deferred:** schema change, dial logic rewrite, trust-projection
rewrite, UI rewrite. High effort. Stretch goal post-launch unless you find
the single-scalar model is the bottleneck on quality.

### 2.10 Branching/forking runs (deferred — flagged, not v2-launch)

**Why:** Event sourcing makes "fork from day N" trivial. "What if I'd
sent THIS message instead?" is a powerful research tool.

**Why deferred:** UI work + projection-rebuild plumbing. Stretch goal.

---

## 3. The simulation invariants that survive (preserved verbatim)

To make the deletions safe, name what's NOT changing:

- Building-as-data, not building-as-code (`data/buildings/{id}/`).
- SOUL/MEMORY split: immutable identity vs. mutable diary.
- Trait vs. incident: characters can BE flawed; cannot have BEEN wronged.
- Admin's first message IS the scenario.
- Organic memory — LLM writes the diary, lossy and biased by design.
- Fictional time only.
- Trust scalar is not fed back into prompts. (Multi-dim trust deferred.)
- Italian-only for v2 launch.
- The scheduler's three-options framing in the notification prompt
  (message / react / put phone down).
- v1's `backend/openrouter.py` retry + fallback cascade.

---

## 4. Activation contracts (the round-robin shift)

Round-robin serial turns + per-slot participation roll. Restated tightly:

1. **Activations are serial within a run.** No two agents activate at
   once.
2. **Every activation sees the complete, up-to-date state of every chat
   the agent participates in.**
3. **Turn order within a round is randomly shuffled** (seeded for
   reproducibility).
4. **Each agent on their turn rolls participation:** `random() <
   agent.participation_probability`. On miss, `agent_skipped_turn` event,
   continue.
5. **Duplicate-content blocking is a property of the schedule, not a
   runtime check.** v1's near-duplicate regex is **deleted**.
6. **Containment is structured-output-first** (JSON Schema strict at
   OpenRouter). Forbidden-vocab regex survives as defense-in-depth.

### Run-end conditions (open-ended)

7. No `MAX_DAY` cap.
8. Run ends on:
   - **`admin_voted_out`** — `motion_kind = 'admin_revocation'`, all
     residents voted YES at close.
   - **`cost_cap_exceeded`** — per-run `cost_cap_usd` reached.
   - **`monthly_budget_exhausted`** — global month-to-date cap reached.
   - **`abandoned`** — nightly cron marks runs untouched for ~7 days.

---

## 5. Architecture — what changes, condensed

### 5.1 Why Heroku stays (the convince-me answer)

You asked. Honest comparison:

| Dim | Heroku Eco €5 (current) | Fly.io | Hostinger VPS |
|---|---|---|---|
| Marginal cost/mo | €5 | €0–3 | €0 (sunk) |
| Setup time | already done | 2–4h | 4–8h |
| Ops burden | near zero | low | high (you're SRE) |
| 30s timeout | yes — 202 pattern needed | none | none |
| Restart blip | ~1s/day | none | none |
| Migration risk | n/a | "is it stable?" tax | DNS, SSL, intrusion surface |

**Stay on Heroku.** The 202 pattern is 5 lines of code that already works.
Round-robin makes a day take 60–250s, longer than 30s on **any** host —
so the async-job shape is what you'd write on Fly anyway. €5 is your
fixed predictable cost; saving €2-5/mo is not worth the migration tax for
a working solo probe. The Hostinger VPS adds an SRE role that has nothing
to do with making the simulation better.

**The 202 + `asyncio.create_task` pattern is reframed in v2 as a
deliberate design choice**, not a workaround.

### 5.2 Module layout (final)

```
backend/
  api/
    runs.py            # POST /runs, GET /runs/:id, POST /runs/:id/advance_day (202)
    admin.py           # admin/announce, admin/dm, motions, world_events
    events.py          # GET /runs/:id/events (SSE w/ Last-Event-ID replay)
    trace.py           # GET /runs/:id/trace, /activations/:id, /quality
    dashboard.py       # GET /runs/:id/dashboard data
    auth.py
  domain/              # Pure simulation logic; NO I/O, NO DB
    soul.py
    memory.py          # parsing only; retrieval is in engine/
    activation.py      # build_system_prompt, build_notification_prompt — pure
    tools_schema.py    # JSON Schema strict mode
    containment.py     # forbidden vocab + content rules — defense-in-depth
    dials.py
    turn_order.py      # random-shuffle round permutation, participation roll
    end_conditions.py
    mood.py            # NEW — pure mood/energy update functions
  engine/
    day_loop.py        # serial round-robin loop with participation rolls
    activation_runner.py
    consolidation.py
    cost_guard.py      # per-run AND per-month gates
    memory_rag.py      # NEW — embed + retrieve
    quality_eval.py    # NEW — Direttore di scena
    world_events.py    # NEW — sample + inject ambient events
  gateways/
    llm.py             # OpenRouter via openai-python; prompt-caching aware
    embed.py           # NEW — embedding gateway (OpenAI or Voyage)
    storage.py
    bus.py             # pg_notify
  projections/
    chat_view.py
    trust_view.py
    cost_view.py
    sentiment_view.py  # NEW
  tests/
    domain/            # offline, fast
    engine/            # respx-stubbed
    e2e/               # pytest -m e2e
  cli/
    import_building.py # condosim import-building data/buildings/001/

frontend/src/
  api/
    queryClient.ts
    runs.ts
    sse.ts
  store/
    ui.ts              # Zustand: godView=TRUE default
  features/
    runSetup/          # <300 LOC, with template gallery
    chatList/          # <200 LOC
    chatView/          # <400 LOC
    adminConsole/      # <400 LOC
    profileModal/      # <300 LOC
    traceViewer/       # NEW — in-app, replaces Langfuse
    runEnded/          # NEW — terminal state UI
    dashboard/         # NEW — sentiment timeline + trust graph + drama spikes
  hooks/
    useSseSubscription.ts
  App.tsx              # <100 LOC

data/
  buildings/           # canonical input format; ingested via CLI
  openings.json        # NEW — curated opening-message templates
  world_events.json    # NEW — ~50 ambient events

ops/
  Procfile             # kept
  runtime.txt          # kept (python-3.12.x)
  package.json         # kept (heroku/nodejs build glue)
  .github/workflows/ci.yml
  .github/workflows/deploy.yml
```

### 5.3 The day loop (final shape)

```python
async def run_day(run_id: str, day: int) -> None:
    async with day_lock(run_id):
        await emit_event(run_id, "day_started", {"day": day})
        await maybe_inject_world_event(run_id, day)         # §2.4
        await save_run_snapshot(run_id)

        agents = await load_agents(run_id)
        windows = allocate_round_windows(config.ROUNDS_PER_DAY)

        for round_idx, window in enumerate(windows):
            order = random.Random(seed_for(run_id, day, round_idx)).sample(agents, k=len(agents))
            tick = (window.end - window.start) // len(order)

            for i, agent in enumerate(order):
                end = await end_conditions.check(run_id)
                if end is not None:
                    await end_run(run_id, end)
                    return

                if random.random() >= agent.participation_probability:
                    await emit_event(run_id, "agent_skipped_turn",
                                     {"agent_id": agent.id, "round": round_idx})
                    continue

                fictional_minute = window.start + i * tick + jitter()
                state = await load_run(run_id)
                await activate_agent(state, agent, fictional_minute)

        await save_run_snapshot(run_id)
        await emit_event(run_id, "day_ended", {"day": day})
        await asyncio.gather(*[consolidate_memory(run_id, a, day) for a in agents])
        await emit_event(run_id, "day_done", {"day": day})
        await maybe_score_quality(run_id, day)              # §2.5
```

`activate_agent` internally:
1. Update mood/energy from prior day events (§2.3).
2. Build system prompt, mark SOUL block as cacheable (§2.1).
3. Build notification prompt with mood cue + RAG-retrieved memory (§2.2).
4. Strict tool-call LLM call.
5. Validate + apply tool effects, emit events.

---

## 6. Data model — additions to v1

```sql
-- v1 additions only; existing tables (runs, agent_memory) get migrated in Phase 1

-- Append-only event log (Phase 1)
create table event_log (
  seq bigserial primary key,
  run_id text not null,
  event_type text not null,
  payload jsonb not null,
  fictional_minute integer,
  day integer,
  actor_kind text not null,
  actor_id text,
  created_at timestamptz default now()
);
create index event_log_run_seq on event_log (run_id, seq);

create or replace function notify_event() returns trigger as $$
begin perform pg_notify('run_events',
  json_build_object('run_id', new.run_id, 'seq', new.seq)::text); return new;
end $$ language plpgsql;
create trigger event_log_notify after insert on event_log
  for each row execute function notify_event();

-- Activations (Phase 1)
create table activations (
  activation_id uuid primary key default gen_random_uuid(),
  run_id text not null,
  agent_id text not null,
  day integer not null,
  round_idx integer not null,
  turn_idx_in_round integer not null,
  fictional_minute integer not null,
  trigger_event_seq bigint references event_log,
  participation_rolled numeric(4,3) not null,
  participation_threshold numeric(3,2) not null,
  was_skipped boolean not null,
  system_prompt_text text,
  system_prompt_hash text,
  notification_prompt_text text,
  inbox_snapshot jsonb,
  tool_calls jsonb,
  outcomes jsonb,
  model text,
  prompt_tokens integer,
  completion_tokens integer,
  cached_prompt_tokens integer,                  -- §2.1 prompt caching
  cost_usd numeric(10,6),
  started_at timestamptz not null,
  ended_at timestamptz not null
);
create index activations_run_min on activations (run_id, agent_id, fictional_minute);

-- LLM calls (Phase 1)
create table llm_calls (
  call_id uuid primary key default gen_random_uuid(),
  run_id text not null,
  activation_id uuid references activations on delete cascade,
  purpose text not null,                          -- 'activation' | 'consolidation' | 'quality_eval' | 'sentiment'
  model_quality_tier text not null default 'routine',  -- §2.6
  model text not null,
  prompt_text text not null,
  response_text text not null,
  prompt_tokens integer not null,
  completion_tokens integer not null,
  cached_prompt_tokens integer default 0,
  cost_usd numeric(10,6) not null,
  latency_ms integer not null,
  status text not null,
  error_text text,
  created_at timestamptz default now()
);
create index llm_calls_month on llm_calls (created_at);

-- Migration: residents.responsiveness → participation_probability (Phase 2)
alter table residents add column participation_probability numeric(3,2) not null default 0.65;
-- Backfill: fast → 0.85, medium → 0.65, slow → 0.35

-- Motions get a kind column (Phase 4)
alter table motions add column kind text not null default 'standard';
-- 'standard' | 'admin_revocation'

-- Runs ended_reason (Phase 4)
alter table runs add column ended_reason text;
-- 'admin_voted_out' | 'cost_cap_exceeded' | 'monthly_budget_exhausted' | 'abandoned'

-- Mood + energy (Phase 6.A)
create table agent_state (
  run_id text not null,
  agent_id text not null,
  mood numeric(3,2) not null default 0.0,        -- ±1
  energy integer not null default 70,            -- 0-100
  last_significant_event text,
  updated_at timestamptz default now(),
  primary key (run_id, agent_id)
);

-- Memory embeddings for RAG (Phase 6.B)
create extension if not exists vector;
create table memory_embeddings (
  run_id text not null,
  agent_id text not null,
  day integer not null,
  content_md text not null,
  embedding vector(1536) not null,               -- OpenAI text-embedding-3-small
  created_at timestamptz default now(),
  primary key (run_id, agent_id, day)
);
create index on memory_embeddings using ivfflat (embedding vector_cosine_ops);

-- Quality scores (Phase 6.C)
create table run_quality_scores (
  run_id text not null,
  day integer not null,
  realism integer not null,
  narrative_arc integer not null,
  character_distinctness integer not null,
  conflict_quality integer not null,
  dialog_quality integer not null,
  notes text,
  scored_by_call_id uuid references llm_calls,
  created_at timestamptz default now(),
  primary key (run_id, day)
);

-- Sentiment timeline (Phase 6.D dashboard)
create table sentiment_observations (
  run_id text not null,
  day integer not null,
  agent_id text not null,
  sentiment numeric(3,2) not null,               -- ±1
  scored_by_call_id uuid references llm_calls,
  primary key (run_id, day, agent_id)
);

-- World events catalog (content; Phase 6.E)
-- Lives in data/world_events.json, not in DB. Sampled at runtime.
```

---

## 7. Execution Plan — phased migration

Each phase ships to main. v1 stays in prod throughout. Each phase is a
shippable PR-or-set; nothing big-bang.

---

### Phase 0 — Foundation (3-4 days)

**Goal:** Set up the skeleton, CI, test harness, and Postgres migrations.
No behavior change.

**Deliverables:**
- New module skeleton: `backend/api/`, `backend/domain/`, `backend/engine/`,
  `backend/gateways/`, `backend/projections/`, `backend/cli/` — with
  `__init__.py` and stub modules. Existing code stays put.
- pytest harness: `pyproject.toml` test config, `pytest-anyio` + `respx`
  installed, `tests/conftest.py` with DB and HTTP-stub fixtures.
- Two pytest markers: default (offline) and `e2e` (gated on
  `OPENROUTER_API_KEY`).
- `.github/workflows/ci.yml`: lint (`ruff`), offline pytest, frontend
  `npm test` if any, on every PR.
- `.github/workflows/deploy.yml`: on merge to main, run
  `git push https://heroku.com/...`.
- Switch `DATABASE_URL` to Supabase **session pooler** URL. Remove
  `statement_cache_size=0` from `backend/db.py`. Verify smoketest still
  passes.
- Migration `002_event_sourcing_skeleton.sql`: creates `event_log`,
  `activations`, `llm_calls`, `pg_notify` trigger. Empty until Phase 1
  starts dual-writing.

**Acceptance criteria:**
- `pytest -m "not e2e"` passes (with no real tests yet — just collection).
- `pytest -m e2e` runs the existing smoketest end-to-end.
- CI runs on a junk PR.
- v1 still works in prod with the new Supabase URL.

---

### Phase 1 — Event log + traceability (1 week)

**Goal:** Make "what did agent X see at fictional minute 437" answerable
in one SQL query. Lays the foundation for the trace UI and the SSE
replay-on-reconnect.

**Deliverables:**
- `gateways/storage.py` — append-only event writer; every place v1 calls
  `events.publish_*` now ALSO writes to `event_log` (dual-write phase).
- `gateways/llm.py` — wrap OpenRouter calls, write `llm_calls` rows. Token
  + cost tracking lands in DB.
- `engine/activation_runner.py` — write `activations` rows on every
  activation (dual with the existing v1 path).
- `api/events.py` — new SSE endpoint reading from `event_log` with
  `Last-Event-ID` replay. Mounted alongside v1's existing in-memory bus
  (both run during cutover).
- `frontend/src/hooks/useSseSubscription.ts` — feature-flagged behind a
  `?v2sse=1` query param so you can A/B the new transport against v1.
- Trace API: `GET /api/runs/:id/trace?day=N` returns paginated activations
  + their llm_calls.

**Acceptance criteria:**
- After running the smoketest: `SELECT * FROM activations WHERE
  run_id=? AND agent_id=? AND fictional_minute=?` returns full prompts +
  outcomes for every activation in the run.
- Mobile-Safari: open admin console, background 10× in 30 min while a run
  is active with `?v2sse=1`, foreground — no missed messages, no watchdog
  poll in network tab.
- `SELECT sum(cost_usd) FROM llm_calls WHERE run_id=?` returns the run's
  total cost.
- v1 SSE still works without the query param (old path untouched).

**Risk:** dual-write doubles DB writes for one phase. Acceptable —
Supabase free tier handles it. Cleaned up in Phase 5.

---

### Phase 2 — Round-robin day loop (1 week)

**Goal:** Replace v1's engagement-roll batch parallelism with serial
round-robin + participation rolls. Delete near-duplicate fingerprinting.

**Deliverables:**
- `domain/turn_order.py` — pure: random shuffle per round, participation
  roll, fictional-minute tick allocation.
- `engine/day_loop.py` — the serial coroutine with participation rolls
  and end-condition checks (end conditions stub for now; full version in
  Phase 4).
- Schema migration `003_residents_participation.sql` — add
  `participation_probability`, backfill from existing `responsiveness`
  enum (fast=0.85, medium=0.65, slow=0.35). Update `residents.json` schema
  in `data/buildings/001/`.
- Replace `backend/main.py:advance_day` body to call the new `run_day`.
  Keep the 202 + `asyncio.create_task` pattern.
- **Delete**: `backend/scheduler.py` engagement rolls, priority queue,
  batch parallelism, cascade depth. Delete near-duplicate fingerprinting
  in `backend/tools.py:375-391`.
- Frontend: remove the v1 "is this a duplicate?" handling if any leaked
  client-side.

**Acceptance criteria:**
- Run-the-day SQL invariant: no two activations have overlapping
  `[started_at, ended_at]` windows.
- Run-the-day SQL invariant: at every activation, the `inbox_snapshot`
  covers all `chat_messages` for chats the agent participates in with
  `fictional_minute < activation.fictional_minute`.
- Participation roll sanity: across 10 rounds, an agent's skip rate is
  within ±15% of `(1 - participation_probability)`.
- Smoketest passes (qualitatively: messages feel staggered, not all
  fired at once; quiet agents skip turns).
- Containment-vocab canaries still 0 (forbidden-vocab regex still works
  as defense-in-depth).
- A new run produces zero `near_duplicate_blocked` events because the
  detection code is gone — the schedule prevents the cause.

---

### Phase 3 — Structured outputs as primary containment (3-4 days)

**Goal:** JSON Schema strict mode at OpenRouter is the wall. Forbidden-vocab
regex becomes secondary defense.

**Deliverables:**
- `domain/tools_schema.py` — full JSON Schema for every tool, with
  `strict: true` and inline forbidden-vocab `not.pattern` clauses.
- `gateways/llm.py` — pass `tools` as the strict-mode-formatted schemas.
- `domain/containment.py` — pure `contains_forbidden`,
  `_content_rule_violation`, `_dm_cooldown_active`. Called as
  defense-in-depth, after the LLM responds.
- Update `engine/activation_runner.py` to use the new tool-call validation
  path.

**Acceptance criteria:**
- Schema-violating tool call from the model is rejected provider-side
  (verifiable by inspecting `llm_calls.error_text`); never reaches our
  code as a free-text "send_message" with bad shape.
- Forbidden-vocab canary count: still 0 across smoketest.
- Cost per activation drops slightly (fewer retries on bad tool shapes).

---

### Phase 4 — Open-ended runs + cost caps + end conditions (1 week)

**Goal:** Drop the 14-day cap. Add the unanimous-revocation end condition.
Add per-run AND per-month cost gates.

**Deliverables:**
- Schema migration `004_motion_kind_run_end.sql`: add `motions.kind`,
  `runs.ended_reason`.
- `domain/end_conditions.py` — pure detectors for cost_cap, monthly_budget,
  admin_voted_out, abandoned.
- `engine/cost_guard.py` — per-run AND per-month gates, called before
  every LLM call.
- Motion of admin revocation: tool-side, agents can already file motions
  in v1; just add `kind = 'admin_revocation'` to the schema and detection
  logic in `end_conditions`.
- Frontend: delete `day < 14` chain logic. Add `features/runEnded/` with
  reason-specific copy.
- Heroku Scheduler addon (free): nightly cron calls `POST
  /api/internal/abandoned_run_sweep` which marks runs untouched for ~7
  days as `ended_reason='abandoned'`.
- UI: monthly-budget banner when `monthly_budget_exhausted`, with reset
  date.

**Acceptance criteria:**
- Set `RUN_COST_CAP_USD=0.50`, run a fresh game — halts mid-day,
  `ended_reason='cost_cap_exceeded'`.
- Set `MONTHLY_BUDGET_USD=0.10`, start a new run — halts before first
  activation, banner shown.
- All 5 residents file + vote YES on a `motion_kind='admin_revocation'`
  motion → run ends with `ended_reason='admin_voted_out'`. Verify the new
  terminal-state UI shows.
- Day 30 reachable in a normal run.

---

### Phase 5 — Frontend rewrite (2 weeks)

**Goal:** App.jsx → feature folders + TanStack Query + Zustand. Drop the
1,983-LOC monolith. Default observer mode on.

**Deliverables:**
- `npm i @tanstack/react-query zustand`.
- `src/api/queryClient.ts`, `src/api/runs.ts`, `src/api/sse.ts`,
  `src/store/ui.ts` (with `godView: true` default).
- Extract `features/runSetup`, `features/chatList`, `features/chatView`,
  `features/adminConsole`, `features/profileModal`. Each <500 LOC.
- `hooks/useSseSubscription.ts` — final form, default-on, no v1 fallback.
- `hooks/useMessagePacer.ts` — extracted from App.jsx pacing refs.
- Delete the v1 in-memory pub/sub in `backend/events.py` once the new
  endpoint is the only consumer.
- Remove dual-write for `event_log` (it's now the only writer).
- Remove the `?v2sse=1` query param — v2 SSE is the default.
- Remove the 5s watchdog poll.
- Build the in-app trace UI: `features/traceViewer/`, ~400 LOC.

**Acceptance criteria:**
- `cloc --by-file frontend/src` — no file >500 LOC.
- App.jsx <100 LOC.
- 10× mobile Safari background test passes without watchdog.
- Trace UI: click any chat message → opens activation timeline.
- `git grep "useState.*godView"` returns 0 matches; `useUiStore` owns it.
- Bundle size before/after comparison documented (TanStack Query +
  Zustand cost ~40 KB gzipped — acceptable).

---

### Phase 6 — Realism + quality additions

This is where v2 stops being "v1 cleaned up" and starts being a better
product. Sub-phases can be parallelized or sequenced; each is shippable
on its own.

#### 6.A — Prompt caching on SOUL (2 days)

- `gateways/llm.py`: when the model is Anthropic, mark the SOUL block in
  the system prompt as `{"type": "ephemeral"}` cache control. For other
  models, no change.
- `activations.cached_prompt_tokens` populated from response usage.
- **Acceptance:** on a Sonnet/Haiku run, ≥80% of activations after the
  first show non-zero `cached_prompt_tokens`. Average cost-per-activation
  drops measurably (track in `run_costs`).

#### 6.B — Memory RAG (1 week)

- `gateways/embed.py`: OpenAI text-embedding-3-small client.
- Schema migration: `memory_embeddings` table with `pgvector`.
- `engine/consolidation.py`: on each `day_done` per agent, embed the new
  diary entry and write the row.
- `engine/memory_rag.py`: at activation time, embed the current chat
  context window (last ~20 messages from chats this agent participates
  in), retrieve top-K diary entries by cosine similarity. Inject into
  prompt instead of full MEMORY concat.
- Keep day-1 seed in full (it's biographical bedrock); RAG only over
  appended day-N entries.
- **Acceptance:** activation prompt size grows roughly *constantly* with
  run length, not linearly. Smoketest at day 30 has activation prompts
  under 4K tokens.

#### 6.C — Mood + energy + world events (1 week, parallel)

- `domain/mood.py`: pure update functions taking (prior mood, day events)
  → new mood + energy.
- Schema: `agent_state` table.
- `engine/consolidation.py`: at day_end, update each agent's mood/energy.
- `domain/activation.py`: notification prompt includes a one-line scene
  cue: "Sei un po' giù dopo la lite di ieri." (Italian, drawn from a
  small templated table by mood + energy bucket.)
- `data/world_events.json`: ~50 ambient events.
- `engine/world_events.py`: at `day_started`, with `WORLD_EVENT_PROB=0.4`,
  sample one event and post to main chat as `system`-authored ambient
  notice.
- **Acceptance:** in a 14-day smoketest, ~5-7 world events occur;
  qualitative review shows agents organically reference them. Mood-cue
  line varies day-to-day per agent.

#### 6.D — Quality eval LLM (3 days, parallel)

- `engine/quality_eval.py`: at `day_done`, run a Sonnet pass over the
  day's transcript. Returns 5 dimensions on a 0-10 scale. Stored in
  `run_quality_scores`.
- `engine/sentiment.py` (lighter): at `day_done`, run a Haiku-grade
  per-agent sentiment classifier over the day's messages from that
  agent. Stored in `sentiment_observations`.
- Both gated by cost guard (skipped if monthly budget low).
- **Acceptance:** dashboard route renders sentiment timeline + quality
  scores per day across a smoketest run.

#### 6.E — Setup screen + admin dashboard (1 week, parallel)

- `data/openings.json`: 6-8 curated opening templates with difficulty.
- `features/runSetup/`: template gallery, one-click insert.
- `features/dashboard/`: sentiment timeline (line chart), drama-spike
  heatmap (activations × fictional time), trust graph (D3 force layout
  over `trust_matrix`), per-agent talk-time bars.
- **Acceptance:** new operator can create a run from a template in <30
  seconds. Dashboard renders all four panels for a 14-day run.

#### 6.F — Per-call model quality dial (2 days, parallel)

- `model_quality_tier` column on `llm_calls` (already in schema).
- Config: `MODEL_BY_TIER = {'routine': 'gemini-flash-lite', 'reflective':
  'haiku', 'highlight': 'sonnet'}`.
- Gateway selects model based on tier.
- Routes that bump tier: motion-close summaries (highlight), eventful-day
  consolidations (reflective), big trust-pivot activations (reflective).
- **Acceptance:** `SELECT purpose, model_quality_tier, count(*) FROM
  llm_calls GROUP BY 1,2` shows mix.

---

### Phase 7 — Cleanup + retire v1 paths (3-4 days)

**Goal:** Delete v1 code that's been superseded; tighten docs.

**Deliverables:**
- Delete `backend/scheduler.py` (already replaced).
- Delete `backend/events.py` in-memory pub/sub.
- Delete the `?v2sse=1` toggle and the v1 reconnect block.
- Delete near-duplicate fingerprinting code (already disabled).
- Delete the `responsiveness` enum once `participation_probability` is
  the only path.
- Update CLAUDE.md and IMPLEMENTATION.md to reflect v2.
- Document the operational runbook: how to bump caps, how to inspect a
  bad run via the trace UI, how to handle a stuck day-lock (manual
  `UPDATE` query).

**Acceptance:** `git grep "responsiveness\|cascade_max_depth\|near_duplicate"`
returns nothing. CLAUDE.md no longer mentions the day-end lock invariant
prose (the code structure is the invariant).

---

### Stretch / post-launch (not in v2 scope)

- Multi-dimensional trust (§2.9)
- Run forking / branching (§2.10)
- Building authoring UI
- Multi-language buildings (Spanish, Portuguese, English)
- Public read-only "share this run" route
- Researcher API for transcript export

---

## 8. v1 → v2 file mapping (final)

| v1 file / behavior | Phase | v2 destination |
|---|---|---|
| `backend/agent.py` | P2 | `domain/activation.py` (pure) + `engine/activation_runner.py` (stateful) |
| `backend/scheduler.py` | P2 | **DELETED.** Replaced by `engine/day_loop.py` + `domain/turn_order.py`. |
| `backend/tools.py` near-duplicate fingerprinting | P2 | **DELETED.** |
| `backend/tools.py` forbidden-vocab regex + `_content_rule_violation` | P3 | `domain/containment.py` — defense-in-depth. |
| `backend/tools.py` `_dm_cooldown_active` | P3 | `domain/containment.py` — kept. |
| `backend/main.py:301-352` (202 pattern) | P2 (body change only) | **KEPT** as deliberate design choice; body now calls `run_day` not `advance_to_next_day`. |
| `backend/storage.py` (JSONB save) | P1 | `gateways/storage.py` — same approach + append-only event writer. |
| `backend/memory.py` | P6.B | `domain/memory.py` (parsing) + `engine/consolidation.py` + `engine/memory_rag.py` + `gateways/storage.py`. |
| `backend/db.py:9-10` (`statement_cache_size=0`) | P0 | **DELETED.** Session pooler URL. |
| `backend/dials.py` | P1 | `domain/dials.py` (pure) + `projections/trust_view.py`. |
| `backend/events.py` (in-memory bus) | P5 | **DELETED.** `event_log` + `pg_notify` + SSE replay. |
| `backend/openrouter.py` | P1 → P6.A | `gateways/llm.py` — same retry; + `llm_calls` writes; + prompt-caching. |
| `frontend/src/App.jsx` (1,983 LOC) | P5 | Split across `features/*` + `store/ui.ts` + `api/*` + hooks. |
| `App.jsx:1342-1386` (5s watchdog) | P5 | **DELETED.** |
| `App.jsx:1549-1724` (custom SSE reconnect) | P5 | **DELETED.** |
| `App.jsx` `day < 14` chain | P4 | **DELETED.** Open-ended. |
| `useState(false)` for godView | P5 | `useUiStore` initializes `true`. |
| `data/buildings/001/` | P0 | Stays as canonical input; ingested via `condosim import-building` CLI. |
| `run_smoketest.py` | P0 | Stays as `pytest -m e2e`. |
| `backend/analyze.py` | — | Stays. Augmented by trace UI + dashboard. |
| Narrator model (planned, never implemented) | — | **DROPPED** entirely. |
| `responsiveness` enum (fast/medium/slow) | P2 | Replaced by `participation_probability` numeric. |

---

## 9. Risks and mitigations

- **Phase 6 scope creep.** Realism additions are tempting to expand. Stick
  to the sub-phases listed; defer multi-dim trust and forking.
- **pgvector adds an extension dependency.** Supabase supports it on the
  free tier; no risk.
- **Prompt caching on Anthropic only.** OpenRouter passes the cache_control
  hint through; non-Anthropic models ignore it. Worst case: same cost as
  today.
- **Eval-LLM adds cost.** ~€0.05 per day-end Sonnet pass. Monthly budget
  needs to cover it. Document in cost-cap defaults.
- **Round-robin lower throughput per day.** v1 ~25s wall-clock per day; v2
  60-250s. This is the explicit price of the schedule. Already accepted.
- **Open-ended runs may accumulate.** Nightly cron sweeps abandoned ones
  (Phase 4). Monitor `runs` table size; truncate `event_log` for ended
  runs older than ~30 days as a cleanup job.
- **Frontend rewrite is the highest-risk phase.** Mitigation: keep v1
  code in place under `frontend/legacy/` during P5 with a feature-flag
  toggle, so rollback is easy.
- **Smoketest cost.** With prompt caching + memory RAG, smoketest cost
  drops; without those, ~€0.25 per run. Cap monthly smoketest spend
  separately if it becomes meaningful.

---

## 10. Sequencing summary (the calendar view)

| Week | Phase | Theme |
|---|---|---|
| 1 | P0 | Foundation — CI, migrations, skeleton, session pooler |
| 2 | P1 | Event log, traceability, trace API, SSE replay |
| 3 | P2 | Round-robin loop + participation rolls; delete scheduler.py |
| 4 | P3 | Structured outputs primary; containment as defense-in-depth |
| 5 | P4 | Open-ended runs + cost caps + revocation vote |
| 6-7 | P5 | Frontend rewrite (largest single phase) |
| 8 | P6.A + P6.F | Prompt caching + per-call model dial |
| 9 | P6.B | Memory RAG |
| 10 | P6.C | Mood + energy + world events |
| 11 | P6.D + P6.E | Eval LLM + sentiment + setup gallery + dashboard |
| 12 | P7 | Cleanup, retire v1 paths, doc updates |

12 weeks of part-time work assuming ~10-15 hours/week. Compresses to ~6
weeks at full-time. Each phase is shippable on its own, so the calendar
can stretch or compress without breaking the chain.

---

## 11. Definition of done for v2

- All NF requirements from §2 of the prior reference design pass (no host
  timeout workarounds, single-query traceability, offline pytest <30s,
  observer default-on, runs open-ended, structural day-lock invariant).
- Round-robin invariants hold (no overlap, full context per turn,
  expected skip distribution).
- Three external services in the runtime path: Heroku, Supabase,
  OpenRouter. No others.
- Per-run cost cap AND per-month budget cap functional and enforced
  before each call.
- Unanimous admin revocation ends a run cleanly.
- Mobile Safari runs untouched for 30 minutes survive without refresh.
- Quality scores observable in the admin dashboard for every completed
  day.
- A 30-day smoketest run completes without prompt overflow (memory RAG
  works), under €5 (prompt caching + cost guard work), with discernibly
  varied agent moods + 1 world event per ~3 days (Phase 6 features
  visible).
- CLAUDE.md and IMPLEMENTATION.md reflect v2 reality; no stale prose
  about lock invariants or 14-day caps.
