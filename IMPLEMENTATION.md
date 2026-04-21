# Condominium Simulator — Implementation Status

Living document describing what's actually built, how it's structured, and what still needs work. Supersedes `game_design.md` (which predates the SOUL/MEMORY refactor and is now stale in places).

---

## 1. What this simulates

A five-resident Italian condominium WhatsApp group. The admin (you, via the UI) posts an opening message; the residents react, form alliances, fight, gossip, and occasionally file formal motions. Everything happens in chat — residents cannot meet in person.

Drama emerges from **flawed character SOULs** + **accumulated MEMORY** + **whatever the admin seeds into the opening message**. There is no pre-scripted scenario.

---

## 2. Architecture overview

### Code (generic, no scenario-specific names)

```
backend/
  main.py           # FastAPI app: run CRUD, advance-day, admin actions, SSE
  building.py       # Generic building loader: load_building(id), build_run_state
  scheduler.py      # DayLoop: engagement rolls → priority queue → parallel activations
  agent.py          # build_system_prompt, build_notification_prompt, activate_agent
  memory.py         # read_soul / read_memory, initialize_run_memory, consolidate_day
  tools.py          # send_message, send_dm, react_to_message, file_motion, + 8 others
  models.py         # Pydantic: Persona, OwnerBrief, Agent, Message, Chat, Motion, RunState
  openrouter.py     # Thin async LLM client with 429 retry + fallback cascade
  storage.py        # JSON run persistence
  analyze.py        # CLI transcript analyzer (canaries, aggression lexicon, tone)
  config.py         # DEFAULT_AGENT_MODEL, temperatures, token caps, scheduler constants
  dials.py          # Trust matrix updates (currently: motion-close only)
  events.py         # Per-run SSE event bus

frontend/
  src/App.jsx       # React UI: resident panel, chat column, admin console, observer
  src/api.js        # API client
  src/App.css       # WhatsApp-style styling
```

### Data (per-building, per-run)

```
data/
  buildings/
    001/                      # "Condominio Via Garibaldi" — the only building today
      building.json           # id, name, fictional_start_iso
      residents.json          # 5 cast templates (persona + owner_kind + wallet)
      souls/{agent_id}.md     # immutable first-person SOUL per resident
      memory_seeds/{agent_id}.md  # day-1 MEMORY seed (bio facts, empty "Appunti")

  runs/
    {run_id}.json             # full RunState snapshot
    {run_id}/
      memory/{agent_id}.md    # copied from seed on day 1, appended at each day_end
```

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

### 3.5 Engagement scheduler

Event-driven with probabilistic engagement rolls, not turn-based. Each new message triggers an audience-wide roll. Engagement probability = `responsiveness_base × (1 + admin_boost) × (1 + mention_boost) × (soft_budget_penalty) × (saturation_damper)`. Engaged agents enter a fictional-time priority queue; activations run in parallel batches within a 60-minute fictional window.

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

### 4.6 Block-and-bail ONLY on near-duplicate

When the dedup filter catches a near-identical resend, `ctx.done = True` ends the activation — preventing the "tack on a different tail to slip past the filter" workaround. Consecutive-DM and daily-cap blocks just refuse the send; the agent can still do other things.

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
| Block-and-bail on near-duplicate send | ✓ |
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

- WhatsApp-style layout (topbar, resident panel, chat column with main/DM tabs, admin console, alliance panel)
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

**Status**: by design during the SOUL/MEMORY refactor, I stopped injecting trust-scalar narration into the system prompt ("Con X vai d'accordo da tempo"). Relationships now live in MEMORY.md — and the matrix now populates from multiple signals (see 6.1). The scalar is used for **scheduler engagement dampers** and displayed in the UI alleanza panel, but not fed into the agent prompt.

**Option (not currently needed)**: re-expose strong trust signals in the prompt once the matrix has meaningful values (e.g. `|score| >= 0.3`). Deferred until we see whether MEMORY-carried relationships alone produce good enough coherence. The matrix is now observable (panel populates), so this is easy to evaluate.

### 6.7 Frontend: observer MEMORY viewer doesn't auto-refresh

**Status**: MEMORY.md viewer in profile modal has a manual refresh (↻). It doesn't auto-update when a new day's consolidation writes to the file.

**Fix direction**: listen to the `memory_consolidation_done` SSE event and auto-refetch. Trivial — 10 min.

### 6.8 Single building only

**Status**: `data/buildings/001/` exists. The architecture supports multiple, but no second building is authored. The UI hardcodes building_id "001" as default payload.

**Fix direction**: (a) author a second building's cast (e.g., a small Milano building with 3 residents) as a content exercise, (b) add a building selector to the Setup screen.

### 6.9 No cost observability

**Status**: every activation and consolidation is an API call. No per-run cost tracking or token accounting.

**Fix direction**: aggregate `usage.prompt_tokens` / `completion_tokens` from openrouter responses into the RunState. Display total on the observer. Estimated 30 min.

### 6.10 No tests

**Status**: no pytest suite. Regression verification relies on `run_smoketest.py` (end-to-end live run) which is slow and costs API calls.

**Fix direction**: unit tests for `_thread_status`, `_content_rule_violation`, `_is_near_duplicate`, `build_run_state` validation, SOUL/MEMORY file readers. Estimated 2h.

---

## 7. Prioritized next-step recommendations

In order of user impact / effort ratio:

1. **MEMORY viewer auto-refresh on SSE** (6.7) — 10 min, makes the observer useful during live play
2. **Admin-goal discoverability in UI** — ensure the UI clearly shows goal state per resident and makes setting/clearing easy. User flagged this as important. Worth auditing the current AdminConsole UX.
3. **Admin-bot for DM replies** (6.4) — 1h, removes the dead-end feel of admin DMs
4. **Second building** (6.8) — content exercise, not code. 1h to author + 15 min for UI selector
5. **Token/cost observability** (6.9) — 30 min, useful for long runs
6. **Unit tests** (6.10) — 2h, pays back whenever we tune prompts again

Recently shipped: multi-signal trust matrix (6.1).

Not recommended as urgent: motion-filing nudge (use admin_goal instead), prompt re-tuning without instrumentation, game_design.md rewrite (replaced by this doc).

---

## 8. How to run

```bash
# Backend
python -m backend.main         # http://127.0.0.1:8001

# Frontend
cd frontend && npm run dev     # http://localhost:5173

# Smoketest (N-day end-to-end run, direct scheduler call, no server needed)
python run_smoketest.py        # set TOTAL_DAYS inside the script

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
