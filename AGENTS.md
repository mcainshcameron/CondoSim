# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Condominio (CondoSim) — a multi-agent deception probe disguised as a condominium
administration game. A FastAPI backend runs 5 LLM "resident" agents (via
OpenRouter) who chat inside a WhatsApp-style Italian condo. A React/Vite
frontend is the admin console. The full implementation status and design
rationale live in `docs/IMPLEMENTATION.md` — read it first for architectural
context, especially §2 (architecture), §3 (SOUL/MEMORY), §4 (design
decisions). `README.md` is also kept current; `old/game_design.md` is stale
and should not be treated as authoritative.

## Commands

```bash
# One-shot dev (installs deps on first run, starts both servers)
./start.sh                          # macOS/Linux
start.bat                           # Windows

# Manual — backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m backend.main              # http://127.0.0.1:8001

# Manual — frontend
cd frontend && npm install && npm run dev   # http://localhost:5173

# --- Offline: no API key, no database, no spend -------------------------
pytest                              # full suite, <1s
python scripts/simulate_offline.py --days 10   # scheduling/ordering/prompt stats
python scripts/dev_server.py        # real server + scripted LLM, for UI work

# --- Costs real money ---------------------------------------------------
python scripts/run_smoketest.py     # end-to-end, real OpenRouter calls

# Transcript analysis / canary check on any saved run
python -m backend.analyze data/runs/<run_id>.json --out report.md
```

**Test first, spend second.** `pytest` runs the real scheduler, activation
loop, tools and consolidation against a scripted LLM (`tests/fake_llm.py`)
and an in-memory store — the whole suite is under a second and costs
nothing. Reach for `run_smoketest.py` only to check *dialogue quality*,
which is the one thing the fake can't tell you.

Required env (see `.env.example` for the full list):

- `OPENROUTER_API_KEY` — for real runs. Not needed offline.
- `DATABASE_URL` — **required on Heroku, optional locally.** Unset, the app
  falls back to an in-process store (state lost on restart). That's what the
  test suite and `scripts/dev_server.py` use.
- `RUN_COST_CAP_USD` / `MONTHLY_COST_CAP_USD` — spend ceilings, enforced
  before each LLM call. Defaults 0.75 / 8.0 USD. The monthly cap sits under
  the €10 OpenRouter key cap deliberately, so the app halts first.
- `ADMIN_PASSWORD`, `SESSION_SECRET` — required for login to work (auth is
  opt-in: it only engages when BOTH are set). In local dev set
  `SESSION_COOKIE_SECURE=0` so cookies work over plain http.
- `DISABLED=1` — optional kill switch. All `/api/*` except `/api/health`
  return 503.

## Deploying to Heroku + Supabase

Architecture is a single Heroku Eco dyno (flat €5/mo, no autoscale) running
FastAPI which both exposes `/api/*` and serves the built Vite SPA at `/`.
State lives in Supabase Postgres (free tier).

- `Procfile` points at `uvicorn backend.main:app`.
- `runtime.txt` pins Python 3.12.
- Root `package.json` is glue for the Node buildpack — `npm run build`
  installs + builds `frontend/` into `frontend/dist/`, which FastAPI then
  mounts as static (`backend/main.py` static mount, last route).
- Set buildpacks in order: `heroku/nodejs` then `heroku/python`.
- Config vars on Heroku: `DATABASE_URL`, `OPENROUTER_API_KEY`,
  `ADMIN_PASSWORD`, `SESSION_SECRET`. Leave `DISABLED` unset normally.
- `db/migrations/*.sql` define the tables (`runs`, `agent_memory` in `001`;
  `llm_spend` in `002_llm_spend.sql`); they auto-apply on startup but you can also paste
  them into the Supabase SQL Editor for the initial create.
- **Never enable autoscaling.** Heroku Eco/Basic can't — keep it that way.
  Set a hard key-level credit cap on OpenRouter and disable auto-topup.
  `MONTHLY_COST_CAP_USD` is the in-app backstop that trips before the
  provider does.

## High-level architecture

### Building = data, code = generic

`data/buildings/{id}/` fully describes a cast. `backend/building.py` loads any
building by id — there is no per-scenario Python module. To author a new
building, create the four files (`building.json`, `residents.json`,
`souls/{agent_id}.md`, `memory_seeds/{agent_id}.md`) — no code changes
required. The admin's first message IS the scenario: `build_run_state` demands
an `opening_text` and refuses to pre-bake one.

### SOUL vs. MEMORY

Two distinct markdown layers per resident, both read fresh from disk on every
activation and injected into the system prompt as the agent's own notebook:

- **SOUL** (`data/buildings/{id}/souls/{agent_id}.md`) — immutable identity,
  first person, no incidents or named relationship priors. *Trait vs. incident*
  is a hard rule: a character can BE vindictive; they must not have BEEN
  wronged by a specific neighbor before the run.
- **MEMORY** — mutable diary, stored in Postgres (`agent_memory` table,
  one row per `(run_id, agent_id)`). Seeded from the building's
  `memory_seeds/*.md` on day 1, then appended each `day_end` by a *second*
  per-agent LLM call that writes `Cosa è successo` + `Cose da ricordare`
  under `--- Giorno N, weekday ---`. Biased/lossy by design.

Rule deletions over rule accumulation: the old ~20-rule behavioral preamble
was stripped. Tone belongs in SOUL; relationship priors belong in MEMORY.
Only platform-level rules (tools, Italian colloquial, phone-always-works,
no-physical-meetings) remain in the prompt.

### The timeline (`backend/timeline.py`) — read this before touching ordering

Message order is a **total order**: `(fictional_timestamp_minutes, seq)`.
`seq` is a per-run monotonic counter assigned at creation. Every message
creation site goes through `timeline.allocate_minute` (never returns a
minute earlier than the newest message in the run) and `timeline.next_seq`.

This exists because v1 stamped timestamps at four call sites deriving "now"
from different values, and `state.clock` lags behind messages produced by an
in-flight activation. An admin post landing mid-activation was stamped from
the stale clock and sorted *before* messages already streamed to the
browser — the transcript visibly reshuffled on every merge. Sorting by
minute alone is not a total order; ties reshuffled too.

Rules: sort with `timeline.sort_key` / `timeline.in_order`, never by
`fictional_timestamp_minutes` alone. The frontend's `compareMessages`
mirrors it. `timeline.backfill_seq` repairs pre-`seq` runs on load.

### LLM gateway (`backend/llm.py`)

**Every** paid call goes through `llm.complete()`. It enforces the per-run
and monthly spend caps *before* the request leaves the process, records
usage once, and provides the seam tests swap via `set_transport()`. Do not
call `openrouter.chat_completion` directly from new code. A cap breach
raises `BudgetExceeded`; the scheduler catches it and ends the run with
`ended_reason` set.

Unknown models fall back to a deliberately *expensive* rate
(`UNKNOWN_MODEL_PRICING_USD_PER_M_TOKENS`) — an unpriced model must
over-estimate, or its spend is invisible and the cap never fires.

### Activation loop (`backend/agent.py`)

Per wake-up: `build_system_prompt` (SOUL + windowed MEMORY + admin_goal +
world rules) → `build_context_digest` pre-reads the recent transcript of
every chat the agent is in straight into the prompt →
`build_notification_prompt` (clock, world event + mood cue, digest,
`_thread_status`, notes, three-options framing) → tool loop capped at
`MAX_TOOL_CALLS_PER_ACTIVATION` (3).

**Most activations are ONE LLM call.** Two changes got them there:

- *Prefetched context.* v1 injected only a notification summary, so a model
  wanting actual message text spent a `read_chat` round trip — and a round
  trip re-sends the whole system prompt. Inlining ~14 recent group messages
  is far cheaper than that duplicate.
- *Implicit done.* v1 burned a second full call purely so the model could
  say `done`. The loop now exits as soon as output has **landed**
  (`sent_messages_this_activation` + `reactions_added_this_activation`
  grew). The check is on what landed, not what was attempted — a send
  refused by containment or the DM cooldown produces nothing, so the loop
  continues and the agent can rewrite. That preserves the acknowledgment
  guarantee.

Prompt size is bounded, not growing: `MAIN_CHAT_CONTEXT` (14) /
`DM_CONTEXT` (6) cap inlined history, and `memory.window_memory` keeps the
biographical seed plus `MEMORY_DAYS_IN_PROMPT` (6) recent diary entries.
Measured flat at ~10.4k prompt chars on both day 10 and day 20.

### Ambient texture (`backend/atmosphere.py`)

Both derived, **zero extra LLM calls**:

- **World events** (`data/world_events.json`, `WORLD_EVENT_PROBABILITY`)
  — one shared building fact per day (lift broken, water cut, noisy night),
  deterministic per `(run, day)`, never repeated within a run. Delivered
  through each resident's prompt as something *they noticed*, **not** posted
  as a system message — an announcement from nowhere would break the chat
  fiction, and a shared fact lets neighbours corroborate each other.
- **Mood cue** — one Italian line read off what actually happened to that
  resident yesterday (messages sent, positive vs. dismissive emoji received,
  times they were named). Returns "" on day 1 and on genuinely quiet days:
  an invented mood is worse than none. This is *not* SOUL drift — identity
  stays immutable; mood is a weather layer over it.

### Scheduler (`backend/scheduler.py`)

Round-robin, serial. Each fictional day is split into `ROUNDS_PER_DAY` (4)
even windows across `[DAY_START_HOUR, DAY_END_HOUR]`. In every round each
agent takes one turn in a seeded random order (`run_id × day × round_idx`)
and rolls a participation probability:
`participation_probability(persona) × time_of_day_window × mention_boost ×
admin_announce_boost × saturation_damper × budget_damper × admin_ping_damper
× quiet_morning_gate`. On hit they activate and see the FULL up-to-date
state every other agent has produced this day so far. On miss they
publish `agent_skipped_turn` and the round continues.

Mid-day admin actions (announce/DM/motion) call
`DayLoop.schedule_reactions(msg, force=True)` from `main.py`; in the
round-robin model this just records the audience as owed-a-reaction. Those
agents bypass the participation roll on their next turn. If all planned
rounds finish and reactions remain owed, up to two **bonus drain rounds**
service them before day_end. Pending admin DMs (recipient hasn't replied
yet) bypass the roll automatically too.

**Acknowledgment guarantee** — admin messages must not be silently
ignored:

1. *Causality clamp.* A forced agent's `target` fictional minute is
   pushed past the latest non-resident message in their audience, so
   they activate after they can actually see what they're owed to react
   to. (`read_inbox` / `_thread_status` filter by
   `fictional_timestamp_minutes <= now`.)
2. *Ack detector.* `ToolContext.reactions_added_this_activation` plus
   `sent_messages_this_activation` together define an acknowledgment.
   A forced agent that closes the phone with neither stays in
   `pending_admin_reactions` and is retried in the bonus drain (max 2
   rounds; on exhaustion a `WARNING` is logged and the set is cleared
   so the day can end).
3. *Prompt nudge.* `build_notification_prompt` takes a
   `forced_for_admin` flag from the scheduler and adds a one-line cue
   ("L'amministratore ha scritto e tu non hai ancora reagito... non
   chiudere il telefono senza dire niente") so the model isn't left
   guessing what the activation is for.

Why serial: parallel activation against the same snapshot was the cause
of the v1 near-duplicate problem — agents B, C, D would each independently
generate "ma cosa succede?" / "qualcuno spieghi" / "che cosa sta
succedendo?" v1 caught these post-hoc with a regex; round-robin removes
the cause (B sees what A just said before B acts).

All timing is fictional minutes since Day 1 00:00, wall-clock-free. At
each `day_end`: save → publish SSE → parallel memory consolidation per
agent → clear notes → save again.

Per-agent participation tuning lives in `data/buildings/{id}/residents.json`:
`persona.participation_probability` is an optional 0..1 override. When
unset, defaults derive from `responsiveness` (fast=0.85, medium=0.65,
slow=0.35).

### Containment (`backend/tools.py`)

Two enforcement layers:

1. **Tier-1 forbidden-vocabulary filter** — regex (`FORBIDDEN_RE`) over agent
   output blocks meta-terms (`simulation`, `as an AI`, `come intelligenza
   artificiale`, etc.) in `send_message` / `send_dm`.
2. **Content rule violations** (`_content_rule_violation`) — refuses sends
   proposing in-person meetings (`ci vediamo`, `passa da me`, `facciamo un
   caffè`, etc.). The condo exists **only in chat**.

Near-duplicate detection has been **removed**: the round-robin scheduler
prevents the parallel-activation race that produced near-dupes in the
first place, so the post-hoc fingerprint regex is no longer load-bearing.
Content rule violations still set `ctx.done = True` and end the activation
(prevents the "tack on a different tail to slip past the filter"
workaround). DM cooldown refusals do NOT end the activation.

**DM cooldown** (`_dm_cooldown_active`, `DM_REPLY_COOLDOWN_MIN = 240`
fictional minutes): reply-gated, not turn-counted. An agent can send a
follow-up DM to the same chat only once the partner replies OR the cooldown
elapses. Replaces the old `PER_DM_DAILY_HARD_CAP`, which stifled realistic
follow-ups (especially to a silent admin).

### Trust matrix (`backend/dials.py`)

Organic signals only, resident-to-resident. Motion votes (±0.10/−0.05 on
close), emoji reactions (±0.02/−0.04), message forwards (+0.01), DM replies
to partner's last (+0.02), attack-by-name in public chat (−0.05). Each update
publishes `trust_updated` SSE. The scalar feeds scheduler dampers + UI, but
is **not** fed back into the agent prompt (by design — relationships live in
MEMORY, not injected narration).

### Persistence & SSE

- **Runs** (`RunState` snapshot) live in Postgres table `runs`
  (`run_id text pk, state jsonb, created_at, updated_at`). `backend/storage.py`
  is async — every `save_run`/`load_run`/`list_runs` must be awaited.
- **Per-agent memory** lives in Postgres table `agent_memory`
  (`run_id, agent_id, content text, updated_at`). Appended at each `day_end`
  via an SQL `rtrim || $new` concat so the write is atomic. `backend/memory.py`
  is async too.
- **Building authoring data** (`data/buildings/001/*.json`, `souls/*.md`,
  `memory_seeds/*.md`) stays on disk, read-only, bundled into the deploy slug.
- **Connection pool** comes from `backend/db.py`. The FastAPI `lifespan`
  opens/closes it; `statement_cache_size=0` so the pool works with the
  Supabase transaction pooler.
- Schema lives in `db/migrations/*.sql` and is applied idempotently at
  startup (also paste-able into the Supabase SQL Editor).
- `GET /api/runs/{id}/events` is the SSE bus (`events.py`) —
  `typing`, `messages`, `day_start`, `day_end`, `day_done`, `run_ended`,
  `motion_filed`, `vote_cast`, `motion_closed`, `trust_updated`,
  `memory_consolidation_start/done`.
- **Day-end race fix** is invariant: save run BEFORE publishing `day_end`
  so SSE observers see a consistent DB state.
- **Day advance runs as a background task.** `POST /api/runs/{id}/advance_day`
  spawns an `asyncio.create_task` and returns **202** immediately so the
  request stays under Heroku's 30s H12 timeout. Sequence inside the task:
  `advance_to_next_day` → `save_run` → memory consolidation → `save_run`
  again (to persist cleared `agent.notes`) → publish `day_done` → release
  the per-run lock.
- **Day-end lock invariant** (updated): `day_end` SSE fires *during* the
  day lock (mid-lifecycle, before memory consolidation). The chain trigger
  is the **`day_done`** event, which fires AFTER consolidation + lock
  release. A POST during an active day still 409s against the held lock.
  Frontend auto-advance chains on `day_done`, NOT on `day_end` or the
  POST response (the POST returns 202 immediately and carries no state).

### Frontend admin console (`frontend/src/App.jsx`)

- Days advance automatically — no manual "advance day" button. After each
  day the `day_done` SSE handler clears `working` and schedules the next
  advance (`AUTO_ADVANCE_DELAY_MS`, 1.2s). A ⏸ Pausa / ▶ Riprendi toggle in
  the topbar cancels/resumes the chain (read via `pausedRef` inside the SSE
  closure) — that's the brake on both the clock and the spend. A speed
  selector (0.5× / 1× / 2× / ⚡) scales message-render pacing; ⚡ renders
  instantly.
- **Render pacing is bounded.** Resident messages are paced by their
  fictional-time delta, but `MAX_QUEUE_LAG_MS` (7s) caps how far behind the
  server the queue may fall. Without it the queue was unbounded and the next
  day started streaming while the previous one was still flushing — two
  days' messages interleaving.
- **The watchdog only fires on real SSE silence** (`SSE_STALL_MS`, 25s of no
  events). It used to treat "clock >= day_end_minutes" as "day finished",
  but the scheduler sets that *before* memory consolidation and before
  releasing the run lock — so it fired every single day, clearing `working`
  mid-day and POSTing an advance the backend answered with 409.
- A `run_ended` SSE event carries the spend-cap reason into a terminal
  banner; the topbar shows running estimated spend.
- Left chat list is scoped to admin-participating chats (main group + admin
  DMs). Inter-resident DMs surface in the "DM frequenti" section at the
  bottom of the left panel and open in the center column when clicked
  (read-only — the admin can observe but not write into them).
- Typing indicator lives in the chat header sub (not the messages list) so
  it doesn't shove the last message up and down as it appears/disappears.

## Conventions

- All agent-facing text is in **Italian**. Tool error strings are in-fiction
  Italian (no English runtime vocabulary).
- New message creation sites MUST use `timeline.allocate_minute` +
  `timeline.next_seq`. Sorting MUST use `timeline.sort_key`.
- New LLM calls MUST go through `llm.complete()`, never
  `openrouter.chat_completion` — otherwise they escape the spend caps.
- Add a test to `tests/` for anything load-bearing. The suite is offline and
  fast; there is no excuse for shipping on a paid smoketest alone.
- Run ids are opaque (`run_{hex}`) and scenario-free — do not encode
  scenario names in code, ids, or module paths. Do not reintroduce a
  `backend/scenarios/` directory.
- Fictional time only — never mix in `datetime.now()` for in-sim scheduling.
- New containment terms go in `FORBIDDEN_TERMS` / `_content_rule_violation`
  in `tools.py`, not scattered through call sites.
- Per-agent model overrides go in `residents.json` (`model` field); defaults
  live in `backend/config.py` (`DEFAULT_AGENT_MODEL`,
  `AGENT_FALLBACK_MODELS`).

## Known gaps worth knowing before changing things

See docs/IMPLEMENTATION.md §6 for the full list. Highlights:

- The offline suite covers mechanics (ordering, scheduling, budgets, API),
  **not dialogue quality**. Only a real smoketest tells you whether the
  residents sound like people.
- Message volume is prompt-sensitive (±3× for small wording changes) —
  prefer instrumentation over re-tuning token caps blind. The brevity
  examples in `build_system_prompt` are load-bearing; the procedural block
  in `build_notification_prompt` is the safe place to edit.
- UI MEMORY viewer doesn't auto-refresh on `memory_consolidation_done`.
- Only one building authored (`001`, Condominio Via Garibaldi); the UI
  hardcodes it as default payload.
- Motions still have no `kind`, so there's no admin-revocation end
  condition — runs end on spend caps only. `docs/IMPLEMENTATION.md` and
  `Possible_plan.md` describe further phases (structured outputs, memory
  RAG, quality eval) that remain unimplemented.
