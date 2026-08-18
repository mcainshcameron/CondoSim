# Condominium Simulator — Implementation Status

Living document describing what's actually built, how it's structured, and what still needs work. Supersedes `game_design.md` (which predates the SOUL/MEMORY refactor and is now stale in places).

---

## Current simplifications

- Automatic "Bacheca del palazzo" ambient notices have been removed. The game now only moves from admin/resident messages, motions, and memory consolidation.
- Frontend day chaining has no artificial inter-day pause or pause/resume control; time moves automatically and the player only participates by reading and writing messages.
- Forced admin reactions require a written resident response. Emoji reactions are still available for resident-to-resident chatter, but they no longer acknowledge admin prompts.

---

## 1. What this simulates

A five-resident Italian condominium WhatsApp group. The admin (you, via the UI) posts an opening message; the residents react, form alliances, fight, gossip, and occasionally file formal motions. Everything happens in chat — residents cannot meet in person.

Drama emerges from **flawed character SOULs** + **accumulated MEMORY** + **whatever the admin seeds into the opening message**. There is no pre-scripted scenario.

---

## 2. Architecture overview

### Code (generic, no scenario-specific names)

```
backend/
  main.py           # FastAPI app: run CRUD, advance-day, admin actions, SSE, auth, rate limits, static SPA mount
  building.py       # Generic building loader: load_building(id), build_run_state
  scheduler.py      # DayLoop: serial round-robin (ROUNDS_PER_DAY=4) with per-turn participation roll
  agent.py          # build_system_prompt (async), build_notification_prompt, activate_agent
  memory.py         # read_soul (file) / read_memory, initialize_run_memory, consolidate_day — Postgres
  tools.py          # send_message, send_dm, react_to_message, file_motion, + 8 others
  models.py         # Pydantic: Persona, OwnerBrief, Agent, Message, Chat, Motion, RunState
  openrouter.py     # Thin async LLM client with 429 retry + fallback cascade
  storage.py        # Postgres run persistence (async save_run / load_run / list_runs)
  db.py             # asyncpg pool + migration runner (db/migrations/*.sql)
  analyze.py        # CLI transcript analyzer (canaries, aggression lexicon, tone)
  config.py         # DEFAULT_AGENT_MODEL, temperatures, token caps, scheduler constants, auth env vars
  dials.py          # Trust matrix updates (currently: motion-close only)
  events.py         # Per-run SSE event bus

db/migrations/
  001_init.sql      # runs + agent_memory tables, applied idempotently at startup

frontend/
  src/App.jsx       # React UI: resident panel, chat column, admin console, profile modal
  src/Login.jsx     # Password entry screen (gated by /api/health auth check)
  src/api.js        # API client (relative URLs; VITE_API_BASE override for dev)
  src/App.css       # WhatsApp-style styling + login card
```

### Data (per-building, per-run)

**Static authoring data** (bundled with the deploy slug, read-only):

```
data/buildings/
  001/                        # "Condominio Via Garibaldi" — the only building today
    building.json             # id, name, fictional_start_iso
    residents.json            # 5 cast templates (persona + owner_kind + wallet)
    souls/{agent_id}.md       # immutable first-person SOUL per resident
    memory_seeds/{agent_id}.md  # day-1 MEMORY seed (bio facts, empty "Appunti")
```

**Mutable per-run state** (Supabase Postgres):

- `runs` — `run_id text pk, state jsonb, created_at, updated_at`. Entire
  `RunState` is round-tripped as JSONB on every `save_run`.
- `agent_memory` — `(run_id, agent_id)` pk, `content text`. Seeded from
  `memory_seeds/` on day 1, appended atomically at each day_end
  (`rtrim(content, E' \t\n\r') || $new`).

Adding a second building = `mkdir data/buildings/002` with the same four files. Zero code changes.

---

## 3. Core concepts

### 3.1 SOUL.md — identity, immutable

First-person markdown. ~15–30 lines. Defines who the resident IS: traits (including dark ones — narcissistic, vindictive, manipulative, etc.), values, communication style, boundaries. No relationship priors by name, no invented past events, no mood-coloring adjectives that pre-set interpretation.

**Rule**: *trait vs. incident*. A character can BE vindictive; they cannot have BEEN wronged by a specific neighbor before the run starts.

Loaded fresh from disk on **every activation**, injected into the system prompt wrapped in first-person framing: *"Quello che segue sono i tuoi appunti su chi sei. Li hai scritti tu, tempo fa..."*.

### 3.2 MEMORY.md — growing diary, mutable

Two layers:
1. **Day-1 seed**: biographical facts (age, job, unit, finances, stake, starting relationships as "I've seen them on the stairs, no grudges"). Copied into the run at creation.
2. **Day-N appended**: at each `day_end`, the agent's own LLM call writes two short blocks:
   - `Cosa è successo` (2–4 lines of first-person observation, selective/biased/lossy)
   - `Cose da ricordare` (0–3 operational lessons/heuristics)

Written under `--- Giorno N, weekday ---` headers. The agent's `notes` scratchpad is cleared after consolidation.

### 3.3 Activation loop

Per-agent wake-up in the scheduler. Each activation:

1. `build_system_prompt(state, agent)` — Name + unit, SOUL, MEMORY, admin_goal, telephone/world/writing rules
2. `read_inbox` as a tool call → injected as initial context
3. `build_notification_prompt` — current time, inbox, admin_goal (repeated), `_thread_status` (cross-day DM + group threads with reply state), notes, three-options line
4. Tool loop: up to 4 steps. Agent calls tools; each tool result is appended to the conversation. Loop ends when agent calls `done` or no tool_calls are returned.

### 3.4 Day consolidation

At each `day_end` (scheduler):
1. Save run (so SSE observers see a consistent disk state)
2. Publish `day_end` event
3. Per-agent parallel LLM call to produce the diary entry → appended to `data/runs/{run_id}/memory/{agent_id}.md`
4. Clear each agent's intra-day `notes` list

### 3.5 Round-robin scheduler

Serial, not parallel. Each fictional day is split into `ROUNDS_PER_DAY=4` even windows across `[DAY_START_HOUR, DAY_END_HOUR]`. Within every round each agent takes one turn in a seeded random order (`run_id × day × round_idx` → reproducible) and rolls a participation probability:

```
participation_probability(persona)
  × time_of_day_window           # 0.4× outside agent's preferred hours
  × mention_boost                # ≥0.95 if surname appeared in last ~3h
  × admin_announce_boost         # +0.15 after a fresh admin main-chat post
  × saturation_damper            # 0.5× if 2 sent today in main, 0.2× if 3+
  × budget_damper                # 0.3× past PER_AGENT_DAILY_SOFT_BUDGET
  × admin_ping_damper            # 0.2× if a peer is pinging silent admin
  × quiet_morning_gate           # 0.1× on round 0 if day>1 and quiet prior day
```

On a hit the agent activates and sees the FULL up-to-date state every prior agent in the round just produced. On a miss the scheduler publishes `agent_skipped_turn` and continues.

`Persona.participation_probability` is an optional 0..1 override per resident; when unset the baseline derives from `responsiveness` (fast=0.85, medium=0.65, slow=0.35).

**Mid-day admin actions** (announce / DM / motion) call `DayLoop.schedule_reactions(msg, force=True)` from `main.py`. The audience is added to `pending_admin_reactions`; those agents bypass the participation roll on their next turn. Pending admin DMs (recipient hasn't replied yet) bypass the roll automatically.

**Acknowledgment guarantee** — admin messages are not silently ignored:

1. *Causality clamp* — a forced agent's `target` fictional minute is pushed past the latest non-resident message in their audience so the message is visible via `read_inbox` (which filters by `fictional_timestamp_minutes <= now`).
2. *Ack detector* — `ToolContext.landed_output_count()` is the single definition of "this agent produced output". On an ordinary turn an authored message or an emoji reaction both count; under `forced_for_admin` **only an authored message discharges the obligation** — a 👍 does not, and neither does a machine line the agent's own tool call happened to trigger (a motion auto-close posts its tally as `sender_kind="admin"`, so the count filters on `sender_id == agent_id`). The rest of the stack already agreed: `react_to_message` refuses a reaction to the admin's own message under force, and the prompt says *"Una reazione emoji non basta"*; the implicit-done side was the outlier and was corrected to match. A forced agent that closes the phone without words stays in `pending_admin_reactions` and is retried in the bonus drain (max 2 rounds; on exhaustion a `WARNING` is logged and the set cleared so the day can end). `cascaded` flips on **discharge**, not at schedule time: anything still owed when the drain gives up is un-cascaded by `_revive_undischarged_obligations` so the next morning's seed picks it up again, bounded by `_MAX_RETRY_DAY_AGE` (1 day) and skipping `bookkeeping` lines, which owe nobody a reply.
3. *Prompt nudge* — `build_notification_prompt` accepts a `forced_for_admin` flag and adds a one-line cue ("L'amministratore ha scritto e tu non hai ancora reagito... non chiudere il telefono senza dire niente").

Why serial: parallel activation against the same snapshot was the cause of the v1 near-duplicate problem — agents B/C/D would each independently produce "ma cosa succede?" / "qualcuno spieghi" / "che cosa sta succedendo?". v1 caught these post-hoc with a regex; round-robin removes the cause (B sees what A just said before B acts).

---

## 4. Key design decisions

### 4.1 Building is data, code is generic

`backend/building.py` loads any building by ID from `data/buildings/{id}/`. No Python module per scenario. No "heating_crisis" anywhere in code. Run IDs are opaque (`run_{hex}`), scenario-free.

### 4.2 Admin's first message IS the scenario

`build_run_state(building_id, opening_text)` requires `opening_text` — no pre-baked default. The simulator reacts to whatever the admin types: heating crisis, noise complaint, embezzlement accusation, etc. Residents' personalities are stable; the situation they face is open.

### 4.3 No physical meetings

The condo exists only in chat. Residents cannot meet for coffee, cannot "pass by", cannot arrange in-person assemblies. Enforced at two layers:
1. System + notification prompt: explicit "Il tuo mondo" block listing forbidden phrases
2. Tool layer: `_content_rule_violation` in `tools.py` refuses sends containing "ci vediamo", "passa da me", "facciamo un caffè", etc., and ends the activation

### 4.4 Rule deletions over rule accumulation

The old system prompt had ~20 behavioral rules ("tono cresce con gli eventi", "non partire prevenuto", "l'amministratore è impegnato"...). Most were deleted. The SOUL now carries tone; empty day-1 MEMORY prevents invented history by construction. Only platform-level rules remain (tool use, Italian colloquial, phone-always-works).

### 4.5 Organic memory, not a structured log

The end-of-day diary is written BY THE AGENT with an LLM call, not extracted by a script. Subjective, lossy, biased — which is the point. A narcissist's memory of day 1 differs from a pragmatist's memory of the same day.

### 4.6 Block-and-bail ONLY on world-rule violations

When `_content_rule_violation` catches a forbidden phrase (in-person meetings, "chat sparita" / phone-fiction excuses), `ctx.done = True` ends the activation — preventing the "tack on a different tail to slip past the filter" workaround. DM cooldown refusals just refuse the send; the agent can still do other things.

Near-duplicate fingerprint detection has been **removed** in the round-robin shift (§3.5): the schedule prevents the parallel-activation race that produced near-dupes, so the post-hoc regex is no longer load-bearing.

### 4.7 Auto-advance chains on `day_done` SSE, not on the POST response

Days chain automatically in the frontend. `POST /advance_day` now returns **202** immediately and the day runs as a background `asyncio.create_task` on the server — this is what keeps the request under Heroku's 30s H12 timeout. The POST therefore carries no state the frontend can chain on.

The chain trigger is the new `day_done` SSE event, published from the background helper AFTER `save_run` + memory consolidation + lock release. `day_end` still fires *during* the lock (mid-lifecycle) and must NOT be used for chaining — a POST fired on `day_end` will 409 against the still-held lock.

Frontend rule: `onAdvance` only kicks off the work (`api.advanceDay(id)` → 202); the `day_done` SSE handler is what clears the `working` flag and schedules the next advance. The `paused` flag is read via a ref so the SSE closure sees the current value without re-subscribing.

---

## 5. What's implemented + verified

### Behavioural

| Feature | Status |
|---|---|
| SOUL.md per resident (first-person, ~15–30 lines) | ✓ 5 residents authored |
| MEMORY.md day-1 seed + end-of-day consolidation | ✓ |
| Cross-day `_thread_status` in notification prompt | ✓ |
| Emoji reactions as first-class engagement option | ✓ (agents using them: 14 reactions / 3 days in latest run) |
| Anti-hallucination prompt line + phrase filter | ✓ 0 "chat sparita" across recent runs |
| No-physical-meetings rule + phrase filter | ✓ 0 meeting proposals in recent runs |
| Round-robin scheduler with participation rolls | ✓ verified end-to-end (10-day smoketest, 0 leaks, 0 errors) |
| Acknowledgment guarantee for forced admin reactions | ✓ retry path observed firing successfully on day-4 admin follow-up |
| Admin goal injection (system + notification) | ✓ via `PUT /api/runs/{id}/agents/{aid}/goal` |
| Motion filing/voting/closing | ✓ tool + API + trust update on close |
| Day-end race fix (save before SSE publish) | ✓ |

### API

| Endpoint | Purpose |
|---|---|
| `POST /api/runs` | Create run with `{opening_text, building_id}` |
| `GET /api/runs/{id}` | Load run state |
| `POST /api/runs/{id}/advance_day` | Advance one fictional day |
| `POST /api/runs/{id}/admin/announce` | Admin posts to main chat |
| `POST /api/runs/{id}/admin/dm` | Admin DMs a resident |
| `POST /api/runs/{id}/motions` | File a motion |
| `POST /api/runs/{id}/motions/{mid}/close` | Close & tally |
| `PUT /api/runs/{id}/agents/{aid}/goal` | Set/clear admin-authored goal |
| `GET /api/runs/{id}/agents/{aid}/soul` | Read SOUL markdown |
| `GET /api/runs/{id}/agents/{aid}/memory` | Read MEMORY markdown |
| `GET /api/runs/{id}/events` | SSE stream (typing, messages, day_start, day_end, motion_filed, vote_cast, motion_closed, trust_updated, memory_consolidation_start/done) |

### UI

- WhatsApp-style layout (topbar, left chat+resident panel, chat column, admin console for motions)
- **Auto-advance days** with ⏸ Pausa / ▶ Riprendi toggle in the topbar and a live mm:ss timer in the topbar sub. Scheduling is keyed off the `day_done` SSE event (not the `advance_day` POST response and not `day_end` — see §4.7), so backend memory consolidation completes before the next day starts. Short grace delay on run load, ~3s pause between days.
- Left chat list scoped to admin-participating chats (main + admin DMs); inter-resident DMs surface in "DM frequenti" at the bottom and open read-only in the center column when clicked
- Typing indicator lives in the chat header sub, not the messages list — prevents the last message from being shoved up and down as agents start/stop typing
- Setup screen with cast preview + admin's opening-message compose
- Profile modal: admin-goal editor, chat participation summary, trust view, **SOUL.md + MEMORY.md collapsible viewers**
- SSE-driven live updates: typing indicators, new messages, motion events, day transitions

### Canaries (verified passing in recent runs)

- `first_morning_prior_history_hits ≤ 1` — no fabricated past events
- `first_morning_aggression_hits` — no longer a hard canary (we allow flawed characters), but still tracked
- `containment_hits = 0` — no AI/roleplay/meta leakage
- 0 phone-fiction phrases in messages or MEMORY files
- 0 meeting-proposal phrases

---

## 6. Known limitations / what needs improving

### 6.1 ~~Empty alliance/trust matrix~~ — ✅ SHIPPED

**Resolved**: `dials.py` now houses five signal handlers that feed the trust matrix:

| Signal | Delta | Fires at |
|---|---|---|
| Motion vote aligned | +0.10 | `apply_trust_from_votes` on motion close |
| Motion vote opposed | −0.05 | same |
| Positive emoji reaction (👍❤️🔥💯😊👏🙌🎉✨🤝) | +0.02 | `on_reaction` in `tool_react_to_message` |
| Negative emoji reaction (🙄😡😤👎💢😒) | −0.04 | same |
| Forward another resident's message | +0.01 | `on_forward` in `tool_forward_message` |
| DM reply to partner's last message | +0.02 | `on_dm_reply` in `tool_send_dm` |
| Attack-by-name (resident name + aggression term, same msg, public chat only) | −0.05 | `on_message_attack` in `tool_send_message` |

All resident-to-resident only. Each signal publishes `trust_updated` SSE with `{deltas, cause_group}` so the UI alleanza panel refreshes live.

**Verified**: 3-day smoketest produced 3 non-zero entries from reactions alone. Matrix populates organically without needing motions.

**Minor open**: agents are conservative with emoji variety (👍 dominates). Not a tuning priority — expect variety to grow with situational range.

### 6.2 Motion filing is rare

**Status**: across a typical 7–14 day run with only the ambient scenario, 0 motions get filed. The tool exists and is mentioned in the prompt, but the hint is too subtle. Motions DO get filed when admin behavior becomes egregious (we saw a successful revoca vote when the admin started insulting residents).

**Fix direction**:
- Stronger prompt hint when the topic in chat is plainly decision-shaped ("dovremmo decidere se", "si vota", "approviamo")
- OR the admin steers via `admin_goal` on one resident: *"ti stai chiedendo se vale la pena depositare una mozione su X"*

Currently deferred. Admin-goal is the cleanest steering lever.

### 6.3 Message volume fragility

**Status**: the balance between "too chatty" and "ghost town" is narrow. Recent tuning cycles:
- AGENT_MAX_TOKENS=500 → 220 → 150 → 180 (now)
- PER_DM_DAILY_HARD_CAP 4 → 2 → removed (replaced by reply-gated cooldown: DM_REPLY_COOLDOWN_MIN=240 fictional minutes; an agent can DM the same chat again only once the partner replies OR ~4h of fictional time have passed)
- Brevity examples + three-options notification framing

**Observation**: small prompt changes swing volume 3× either way. The model is highly sensitive to phrasing of restraint cues. Current setting (180 tokens, equal three-options framing) produces ~2 text + ~3 reactions per day per agent, which is realistic — but day-2 dead zones still happen.

**Fix direction**: rather than more prompt tuning, instrument engagement. Log per-activation: "model was offered, chose X". See where the decisions cluster. Then shape with minimal interventions. Estimated 1h instrumentation + iteration.

### 6.4 Admin ↔ resident threads still one-sided

**Status**: residents DM the admin occasionally; the admin is silent by default (the admin is YOU via UI). No auto-reply.

**Fix direction**: optional "admin auto-reply bot" — small LLM call when a resident DMs the admin, produces a brief in-character reply ("ricevuto, mi attivo") so the thread isn't a dead end. Could be toggled per run. Estimated 1h.

### 6.5 `game_design.md` is stale

**Status**: references `backend/scenarios/heating_crisis.py` paths, v1 cast framing, `brief_text`, and other concepts that no longer exist post-refactor.

**Fix direction**: either update to match current architecture, or deprecate in favor of this document. Recommend deprecating — living specs should be one place.

### 6.6 Trust scalar no longer verbalized

**Status**: by design during the SOUL/MEMORY refactor, I stopped injecting trust-scalar narration into the system prompt ("Con X vai d'accordo da tempo"). Relationships now live in MEMORY.md — and the matrix now populates from multiple signals (see 6.1). The scalar is displayed in the UI alleanza panel and emits a `trust_updated` SSE on every change, but is **not** fed back into the agent prompt or the scheduler's participation roll.

**Option (not currently needed)**: re-expose strong trust signals in the prompt once the matrix has meaningful values (e.g. `|score| >= 0.3`). Deferred until we see whether MEMORY-carried relationships alone produce good enough coherence. The matrix is now observable (panel populates), so this is easy to evaluate.

### 6.7 ~~Frontend: MEMORY viewer doesn't auto-refresh~~ — ✅ SHIPPED

**Resolved**: the profile modal listens for `memory_consolidation_done` via the app-level SSE handler. If the MEMORY panel is open, it refetches the resident's MEMORY after each consolidation. Manual refresh remains available.

**Verified**: production frontend build passes after the SSE refresh wiring.

### 6.8 Single building only

**Status**: `data/buildings/001/` exists. The architecture supports multiple, but no second building is authored. The UI hardcodes building_id "001" as default payload.

**Fix direction**: (a) author a second building's cast (e.g., a small Milano building with 3 residents) as a content exercise, (b) add a building selector to the Setup screen.

### 6.9 No cost observability

**Status**: every activation and consolidation is an API call. No per-run cost tracking or token accounting.

**Fix direction**: aggregate `usage.prompt_tokens` / `completion_tokens` from openrouter responses into the RunState. Display total in the UI. Estimated 30 min.

### 6.10 ~~No tests~~ — ✅ SHIPPED

**Resolved**: `pytest` runs the real scheduler, activation loop, tools and consolidation against a scripted LLM (`tests/fake_llm.py`) and an in-memory store — offline, under two seconds, zero spend. The 2026-08-01 review closed the last three blind spots it found: `dials.py` had no test at all (`tests/test_dials.py`), motions and votes had no tally coverage (`tests/test_motions.py`), and the bonus drain had none (`tests/test_scheduler_forcing.py`). `tests/helpers.py` plants transcript content through `timeline.allocate_minute` / `next_seq` so a test can never be the thing that breaks the total order it asserts on.

**Still true**: the suite covers *mechanics*, not dialogue quality. Only `scripts/eval_voices.py` or a real smoketest tells you whether the residents sound like people.

---

## 7. Prioritized next-step recommendations

In order of user impact / effort ratio:

1. **Admin-goal discoverability in UI** — ensure the UI clearly shows goal state per resident and makes setting/clearing easy. User flagged this as important. Worth auditing the current AdminConsole UX.
2. **Admin-bot for DM replies** (6.4) — 1h, removes the dead-end feel of admin DMs
3. **Second building** (6.8) — content exercise, not code. 1h to author + 15 min for UI selector
4. **Token/cost observability** (6.9) — 30 min, useful for long runs

Recently shipped: multi-signal trust matrix (6.1), the offline pytest suite (6.10).

Not recommended as urgent: motion-filing nudge (use admin_goal instead), prompt re-tuning without instrumentation, game_design.md rewrite (replaced by this doc).

---

## 8. How to run

```bash
# Backend
python -m backend.main         # http://127.0.0.1:8001

# Frontend
cd frontend && npm run dev     # http://localhost:5173

# Smoketest (N-day end-to-end run, direct scheduler call, no server needed)
python scripts/run_smoketest.py    # set TOTAL_DAYS inside the script

# Transcript analysis
python -m backend.analyze data/runs/{run_id}.json --out report.md
```

## 9. Authoring new buildings

1. `mkdir -p data/buildings/{new_id}/souls data/buildings/{new_id}/memory_seeds`
2. Write `building.json` (id, name, fictional_start_iso)
3. Write `residents.json` (list of `{persona, owner_kind, starting_wallet_eur}`)
4. Author one SOUL.md and one memory_seed.md per resident
5. Register in the API by passing `building_id` in `POST /api/runs` payload

No code changes required.

## 10. Authoring new residents in the existing building

Same, but within `data/buildings/001/`. The `residents.json` is the source of truth; `load_residents` reads it on every run creation. Remember to add SOUL and memory_seed files matching each new resident's `persona.id`.
