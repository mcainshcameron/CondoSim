# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

# Voice/independence eval — real LLM, ~$0.10 for 5 days. Run BEFORE and AFTER
# any prompt change; --run-id is pinned so both see the same schedule.
python scripts/eval_voices.py --days 5 --label before --dump before.txt
python scripts/eval_voices.py --days 5 --label after  --dump after.txt

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
- `DEBUG_ENDPOINTS=1` — opens `/api/debug/logs` (404 otherwise). That route
  returns the raw log ring buffer: live run ids and chat excerpts. Auth is
  opt-in, so leave it unset in production.

## Deploying to Heroku + Supabase

Architecture is a single Heroku Eco dyno (flat €5/mo, no autoscale) running
FastAPI which both exposes `/api/*` and serves the built Vite SPA at `/`.
State lives in Supabase Postgres (free tier).

- `Procfile` points at `uvicorn backend.main:app`, with
  `--proxy-headers --forwarded-allow-ips='*'` and `TRUST_PROXY_HEADERS=1`
  on the same line. They only make sense together: without the uvicorn
  flags every request arrives from the Heroku router's address and the
  per-IP rate limits become one global bucket; with them, uvicorn trusts
  X-Forwarded-For **entry 0**, which is whatever the caller wrote (Heroku
  appends rather than replaces), so `main._client_key` keys on the LAST
  entry instead — the one our own router added. `TRUST_PROXY_HEADERS` is
  what tells it a proxy we control is actually in front; unset, the header
  is ignored entirely, which is the fail-safe direction.
- `runtime.txt` pins Python 3.12.
- Root `package.json` is glue for the Node buildpack — `npm run build`
  installs + builds `frontend/` into `frontend/dist/`, which FastAPI then
  mounts as static (`backend/main.py` static mount, last route).
- Set buildpacks in order: `heroku/nodejs` then `heroku/python`.
- Config vars on Heroku: `DATABASE_URL`, `OPENROUTER_API_KEY`,
  `ADMIN_PASSWORD`, `SESSION_SECRET`. Leave `DISABLED` and
  `DEBUG_ENDPOINTS` unset normally; `TRUST_PROXY_HEADERS` is set by the
  Procfile, not as a config var.
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

The prompt takes its scene from data too: the group's name comes off
`state.chats`, and `BuildingConfig.city` (optional, defaults to `""`, so an
older `building.json` still validates) supplies the town. Read it through
`building.building_scene(building_id)` — a deliberately total wrapper that
logs and returns `("", "")` rather than letting a malformed `building.json`
kill an activation mid-run.

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

`Message` also carries `bookkeeping: bool = False` — additive, no
migration (state is jsonb, and old saved runs deserialize with it False).
It marks a machine-authored record that lands in the admin column without
the administrator having spoken; see the scheduler section below for what
reads it.

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

**Reasoning is disabled globally** (`DISABLE_MODEL_REASONING`, sent as
`reasoning: {"enabled": false}` from `openrouter._single_call`). This is
load-bearing, not a preference: reasoning tokens are billed as output and
are spent *before* any content, so against `AGENT_MAX_TOKENS` (180) a
reasoning model returns `finish_reason="length"` with **empty content** —
a silently failed activation that logs as a normal 200. Measured on the
real prompt: DeepSeek V4 Flash 0/6 activations with reasoning on vs 6/6
off; Grok 4.3 1/6 (~628 reasoning tokens). OpenRouter accepts and ignores
the parameter on non-reasoning models, so it ships unconditionally.
`reasoning: {"effort": "minimal"}` does **not** work — only the explicit
disable does.

**Before adopting any new model, check two things the catalog won't tell
you:** that it actually emits `tool_calls` rather than a prose reply (a
text answer is a no-op in this loop — `google/gemini-2.5-flash-lite`
writes excellent Italian and fails this 0/6), and that it produces output
under `AGENT_MAX_TOKENS`. Both fail *silently*.

### Activation loop (`backend/agent.py`)

Per wake-up: `build_system_prompt` (SOUL + windowed MEMORY + admin_goal +
world rules) → `build_context_digest` pre-reads the recent transcript of
every chat the agent is in straight into the prompt →
`build_notification_prompt` (clock, world event + mood cue, digest,
`_thread_status`, notes, three-options framing) → tool loop capped at
`MAX_TOOL_CALLS_PER_ACTIVATION` (3).

**A turn spent narrating is not a turn spent choosing silence.** When the
model answers in prose instead of calling a tool, the loop nudges once
("hai pensato, ma non hai toccato il telefono") and lets it retry, rather
than discarding the activation. Dropping it was invisible — it looked like
the resident chose to stay quiet, when the harness had thrown their turn
away. It cost the models most prone to thinking out loud almost all their
output: a 5-day eval had Greco and Ferrari on 1 message each while
Marchetti sent 10. The nudge fires at most once per activation and only
while nothing has landed yet.

**Most activations are ONE LLM call.** Two changes got them there:

- *Prefetched context.* v1 injected only a notification summary, so a model
  wanting actual message text spent a `read_chat` round trip — and a round
  trip re-sends the whole system prompt. Inlining ~14 recent group messages
  is far cheaper than that duplicate.
- *Implicit done.* v1 burned a second full call purely so the model could
  say `done`. The loop now exits as soon as output has **landed**, where
  "landed" is the single predicate `ToolContext.landed_output_count()` —
  the same one the scheduler's ack detector uses. The check is on what
  landed, not what was attempted — a send refused by containment or the DM
  cooldown produces nothing, so the loop continues and the agent can
  rewrite. That preserves the acknowledgment guarantee. There used to be
  three hand-rolled versions of this expression and they disagreed; do not
  add a fourth.

Prompt size is bounded, not growing: `MAIN_CHAT_CONTEXT` (14) /
`DM_CONTEXT` (6) cap inlined history, and `memory.window_memory` keeps the
biographical seed plus `MEMORY_DAYS_IN_PROMPT` (6) recent diary entries.
Measured flat by `scripts/simulate_offline.py`: ~11.7k avg / 12.7k max
prompt chars at day 10, ~12.0k / 12.7k at day 20. The number to defend is
the *flatness*, not the absolute — a day-20 max that tracks the day-10 max
means nothing accumulates. That holds for caller-controlled text too now:
every admin field is `Field(max_length=...)` on the request model (goal 2000,
announce/DM 4000, opening 8000, motion title 200), so oversize is a 422 at the
door, and both prompt sinks plus `build_context_digest` clamp defensively. An
unbounded `admin_goal` was a ~100x spend multiplier — it is re-inlined into
BOTH the system and the notification prompt on every activation for the rest
of the run, and the only symptom was `cost_cap_exceeded`.

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
service them at the end of the day. A drain target aims at `day_end - 60`
but is floored on the transcript head, so it can land a few minutes *past*
`day_end` when the head is already there — causality outranks the cap, and
the alternative was activating an agent behind messages they were woken to
answer. Pending admin DMs (recipient hasn't replied yet) bypass the roll
automatically too.

**Acknowledgment guarantee** — admin messages must not be silently
ignored:

1. *Causality clamp.* A forced agent's `target` fictional minute is
   pushed past the latest non-resident message in their audience, so
   they activate after they can actually see what they're owed to react
   to. (`read_inbox` / `_thread_status` filter by
   `fictional_timestamp_minutes <= now`.)
2. *Ack detector.* One predicate decides it —
   `ToolContext.landed_output_count()`. On an ordinary turn a message the
   agent **authored** or an emoji reaction both count as output. Under
   `forced_for_admin` **only an authored message discharges the
   obligation**; a reaction alone does not, and neither does a machine
   line the agent's tool call happened to trigger (a motion auto-close
   posts its tally as `sender_kind="admin"`, which is why the count
   filters on `sender_id == agent_id`). The rest of the stack agrees:
   `react_to_message` refuses a reaction to the admin's own message under
   force, and the prompt says *"Una reazione emoji non basta ... deve
   vedere parole tue"*. A forced agent that closes the phone without
   words stays in `pending_admin_reactions` and is retried in the bonus
   drain (max 2 rounds; on exhaustion a `WARNING` is logged and the set is
   cleared so the day can end).
   `cascaded` is flipped on **discharge**, not at schedule time. Anything
   still owed when the drain gives up is un-cascaded by
   `_revive_undischarged_obligations` so tomorrow's seed picks it up again
   — bounded by `_MAX_RETRY_DAY_AGE` (1 day) so a model that never calls a
   tool can't force-activate the building every morning until the spend cap
   trips, and skipping `bookkeeping` lines, which owe nobody a reply.
3. *Prompt nudge.* `build_notification_prompt` takes a
   `forced_for_admin` flag from the scheduler and adds a one-line cue
   ("L'amministratore ha scritto e tu non hai ancora reagito... non
   chiudere il telefono senza dire niente") so the model isn't left
   guessing what the activation is for.

**`Message.bookkeeping` — admin column, but nobody is speaking.** A vote
tally (`tools._close_motion_if_ready`, `main.api_close_motion`) is stamped
`sender_kind="admin"` so the UI puts it in the right column, and every
consumer downstream then read it as the administrator talking: it was
quoted back to residents as *"Cosa ha detto, parole sue"*, it dragged the
forced-agent causality clamp forward, and the day-start seed force-activated
all five residents the next morning to answer a scoreboard — `prob = 1.0`,
bypassing the participation roll, the quiet-morning gate, the saturation
damper and the soft budget. Set `bookkeeping=True` on any machine-authored
record and skip such messages in anything that means "unanswered admin
input". Do **not** express this as a new `SenderKind` — the frontend
branches on `sender_kind` in nine places.

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

Cross-agent near-duplicate detection has been **removed**: the round-robin
scheduler prevents the parallel-activation race that produced near-dupes in
the first place, so the post-hoc fingerprint regex is no longer
load-bearing. **Self**-repetition is a different failure and is still
blocked (`_is_self_repeat`): an agent whose point drew no reply will
otherwise post it again a round later, verbatim or with extra emoji. Scoped
to same agent + same chat + same day, and short interjections ("ok", "mah")
are exempt. Unlike a content-rule violation it does NOT set `ctx.done` —
the useful outcome is a rephrase or a DM, and ending the turn would
recreate the silence it exists to fix.
Content rule violations still set `ctx.done = True` and end the activation
(prevents the "tack on a different tail to slip past the filter"
workaround). DM cooldown refusals do NOT end the activation.

**DM cooldown** (`_dm_cooldown_active`, `DM_REPLY_COOLDOWN_MIN = 240`
fictional minutes): reply-gated, not turn-counted. An agent can send a
follow-up DM to the same chat only once the partner replies OR the cooldown
elapses. Replaces the old `PER_DM_DAILY_HARD_CAP`, which stifled realistic
follow-ups (especially to a silent admin).

### Motion tallies (`backend/motions.py`)

One rule, two callers. `tally_motion(state, motion)` applies quorum
(`QUORUM_MIN_ATTENDING`) plus the 500/1000 `MILLESIMI_MAJORITY` and a yes-head
majority; `motion_is_decided(state, motion)` decides only *when* a motion can
close — everyone has voted, or no combination of the outstanding votes can
still change the outcome.

The module exists because the resident `vote` tool's auto-close
(`tools._close_motion_if_ready`) and the admin close endpoint
(`main.api_close_motion`) each counted the votes themselves and disagreed: the
tool path used a bare head-count `total // 2 + 1` with no millesimi and no
quorum. The weaker rule always won, because the API path short-circuits on an
already-closed motion — the admin could never overrule a tally the tool had
already written. Each caller still owns its own wording and side effects
(`tools` posts the `bookkeeping=True` "📋 [Esito mozione]" line, `main` writes
the millesimi-bearing `outcome_note` and applies trust); only the arithmetic
is shared.

### Trust matrix (`backend/dials.py`)

Organic signals only, resident-to-resident. Motion votes (±0.10/−0.05 on
close), emoji reactions (±0.02/−0.04), message forwards (+0.01), DM replies
to partner's last (+0.02), attack-by-name in public chat (−0.05). Each update
publishes `trust_updated` SSE. The scalar feeds the **UI only** — the
scheduler's damper stack contains no trust term (`grep -n trust
backend/scheduler.py` returns nothing), and it is **not** fed back into the
agent prompt either (by design — relationships live in MEMORY, not injected
narration). Treat it as an observation surface, not a control input.

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
- **Save before you publish** is invariant, everywhere: `save_run` completes
  BEFORE the matching SSE goes out, so an observer that reacts to the event
  by refetching cannot read a state older than the event describes. Holds at
  `day_end` and, since the admin endpoints were fixed, at announce / DM /
  motion / goal / close too — those five published first and saved after, so
  a refetch raced the write and a crash in between streamed a message that
  was never persisted. `trust_updated` counts too — `apply_trust_from_votes`
  used to publish from inside itself, so `api_close_motion` passes
  `publish=False` and calls `dials.publish_deltas` after the save.
- **A `DayLoop` captured before an `await` is not a `DayLoop` after it.**
  `DayLoop.run` pops itself out of `_ACTIVE_LOOPS` without holding
  `state_lock`, so the admin endpoints re-read `active_loop(run_id)` *after*
  `save_run` and before `schedule_reactions`. Calling it on a dead loop
  records the obligation nowhere while still stamping `cascaded=True`, which
  the loop's post-consolidation save then persists — an admin message
  discharged forever without anyone having seen it.
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
  `timeline.next_seq`. Sorting MUST use `timeline.sort_key`. That includes
  tests — plant transcript content with `tests.helpers.append_message`
  rather than hand-rolling a `Message`, or the test becomes the thing that
  breaks the total order it asserts on.
- New LLM calls MUST go through `llm.complete()`, never
  `openrouter.chat_completion` — otherwise they escape the spend caps.
- Add a test to `tests/` for anything load-bearing. The suite is offline and
  fast; there is no excuse for shipping on a paid smoketest alone.
- Run ids are opaque (`run_{hex}`) and scenario-free — do not encode
  scenario names in code, ids, or module paths. Do not reintroduce a
  `backend/scenarios/` directory.
- Fictional time only — never mix in `datetime.now()` for in-sim scheduling.
- New containment terms go in `FORBIDDEN_TERMS` / `_content_rule_violation`
  in `tools.py`, not scattered through call sites. A blocked phrase that is
  subject-free ("problemi tecnici") must be a compiled pattern anchored on
  `_PHONE_SUBJECT`, not a bare substring — the bare version refused
  residents discussing the building's actual problems. Refusals are worded
  once, by `_refusal_text(category, phrase, verb)`; there are four call
  sites and there must not be a fifth copy of the string.
- One predicate per question. `ToolContext.landed_output_count()` is the
  only definition of "this agent produced output"; `motions.tally_motion`
  is the only vote count; `agent._clamp` is the only prompt truncation.
  Each of these was three copies that disagreed.
- Per-agent model overrides go in `residents.json` (`model` field); defaults
  live in `backend/config.py` (`DEFAULT_AGENT_MODEL`,
  `AGENT_FALLBACK_MODELS`).
- Every new mutating endpoint needs its own `@limiter.limit(...)` and a
  `request: Request` parameter. There is no `default_limits` and there must
  not be one — slowapi only reads them from `SlowAPIASGIMiddleware`, which
  we cannot install (its `send_wrapper` re-emits `http.response.start` per
  body chunk and breaks the SSE stream), so an undecorated route is simply
  unlimited. `tests/test_api_hardening.py` asserts the decorator exists per
  endpoint.
- Every admin-supplied text field needs a `Field(max_length=...)`. It is
  re-inlined into agent prompts, so an unbounded field is a spend
  multiplier, not a cosmetic problem.

## Known gaps worth knowing before changing things

See docs/IMPLEMENTATION.md §6 for the full list. Highlights:

- The offline suite covers mechanics (ordering, scheduling, budgets, API),
  **not dialogue quality**. Only a real smoketest tells you whether the
  residents sound like people.
- Message volume is prompt-sensitive (±3× for small wording changes) —
  prefer instrumentation over re-tuning token caps blind. The brevity
  examples in `build_system_prompt` are load-bearing; the procedural block
  in `build_notification_prompt` is the safe place to edit. **Length is
  sensitive to that block too**: adding the options menu pushed median
  message length 102 → 130 chars until a one-line brevity reminder was
  restated at the decision point. Run `scripts/eval_voices.py` either side
  of any edit here.
- **The closing options menu decides which tools get used at all.** While it
  listed only "messaggio / reazione / metti giù il telefono", a 5-day eval
  produced 21 main-chat messages and ZERO DMs and ZERO motions — five
  help-desks, not five neighbours. Listing the private channel took it to 7
  DM messages across 2 threads and produced an actual Greco↔Marchetti
  alliance. Anything you want the residents to *do* has to be on that menu.
- **Initiative must be aimed at real material or it becomes fabrication.** A
  first attempt at the independence nudge just said "bring your own topic";
  with nothing real to raise, residents invented building events and past
  conversations ("una settimana fa nel gruppo…", on day 2). Pointing it at
  concrete sources — your unanswered question, what a neighbour said, what
  you noticed today — took invented-history hits to 0.
- Ambient world events (`atmosphere.py`, `data/world_events.json`) are
  **legitimate** shared facts. A resident discussing the electricity bill on
  the day the event fires is the system working, not fabrication — don't
  "fix" it. Check `atmosphere.pick_world_event(state)` for the day before
  concluding anything was invented.
- UI MEMORY viewer doesn't auto-refresh on `memory_consolidation_done`.
- Only one building authored (`001`, Condominio Via Garibaldi). The system
  prompt is building-agnostic now, but three places still name it literally:
  the `send_message` schema description in `tools.py`, `scripts/dev_server.py`'s
  scripted responder, and the frontend (landing, topbar, login, default
  payload). Authoring `002` would produce an agent told the group is
  "Palazzo X" while the tool schema still offers "Condominio Via Garibaldi"
  as the example.
- Motions still have no `kind`, so there's no admin-revocation end
  condition — runs end on spend caps only. `docs/IMPLEMENTATION.md` and
  `Possible_plan.md` describe further phases (structured outputs, memory
  RAG, quality eval) that remain unimplemented.
