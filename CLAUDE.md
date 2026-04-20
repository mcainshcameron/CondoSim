# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Condominio (CondoSim) — a multi-agent deception probe disguised as a condominium
administration game. A FastAPI backend runs 5 LLM "resident" agents (via
OpenRouter) who chat inside a WhatsApp-style Italian condo. A React/Vite
frontend is the admin console. The full implementation status and design
rationale live in `IMPLEMENTATION.md` — read it first for architectural
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

# End-to-end smoketest (no server, real OpenRouter calls, ~TOTAL_DAYS days)
python run_smoketest.py             # edit TOTAL_DAYS inside the script

# Transcript analysis / canary check on any saved run
python -m backend.analyze data/runs/<run_id>.json --out report.md
```

There is **no pytest suite** (noted as a known gap in IMPLEMENTATION.md §6.10).
`run_smoketest.py` is the regression check — it's slow and costs API credits.

Required env: `OPENROUTER_API_KEY` in `.env` (see `.env.example`).

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
- **MEMORY** (`data/runs/{run_id}/memory/{agent_id}.md`) — mutable diary. Copied
  from the building's `memory_seeds/` on day 1, then appended each `day_end`
  by a *second* per-agent LLM call that writes `Cosa è successo` + `Cose da
  ricordare` under `--- Giorno N, weekday ---`. Biased/lossy by design.

Rule deletions over rule accumulation: the old ~20-rule behavioral preamble
was stripped. Tone belongs in SOUL; relationship priors belong in MEMORY.
Only platform-level rules (tools, Italian colloquial, phone-always-works,
no-physical-meetings) remain in the prompt.

### Activation loop (`backend/agent.py`)

Per wake-up: `build_system_prompt` (SOUL + MEMORY + admin_goal + world rules)
→ inject `read_inbox` as initial context → `build_notification_prompt` (clock,
inbox, `_thread_status` for cross-day DM/group replies, notes, three-options
framing) → tool loop capped at `MAX_TOOL_CALLS_PER_ACTIVATION` (4). Loop ends
on `done`, on hitting the cap, or on no tool_calls returned.

### Scheduler (`backend/scheduler.py`)

Event-driven, not turn-based. Each new message triggers an audience-wide
engagement roll (`responsiveness_base × admin_boost × mention_boost ×
budget_penalty × saturation_damper`). Engaged agents enter a fictional-time
priority queue; activations run in parallel batches within a 60-minute
fictional window (`BATCH_FICTIONAL_WINDOW_MIN`). Cascade depth is bounded
(`CASCADE_MAX_DEPTH`). All timing is fictional minutes since Day 1 00:00,
wall-clock-free. At each `day_end`: save → publish SSE → parallel memory
consolidation per agent → clear notes.

### Containment (`backend/tools.py`)

Two enforcement layers:

1. **Tier-1 forbidden-vocabulary filter** — regex (`FORBIDDEN_RE`) over agent
   output blocks meta-terms (`simulation`, `as an AI`, `come intelligenza
   artificiale`, etc.) in `send_message` / `send_dm`.
2. **Content rule violations** (`_content_rule_violation`) — refuses sends
   proposing in-person meetings (`ci vediamo`, `passa da me`, `facciamo un
   caffè`, etc.). The condo exists **only in chat**.

**Block-and-bail invariant**: near-duplicate resend detection sets
`ctx.done = True`, ending the activation — this prevents the "tack on a
different tail to slip past the filter" workaround. Regular refusals (daily
cap, consecutive-DM) do NOT end the activation.

### Trust matrix (`backend/dials.py`)

Organic signals only, resident-to-resident. Motion votes (±0.10/−0.05 on
close), emoji reactions (±0.02/−0.04), message forwards (+0.01), DM replies
to partner's last (+0.02), attack-by-name in public chat (−0.05). Each update
publishes `trust_updated` SSE. The scalar feeds scheduler dampers + UI, but
is **not** fed back into the agent prompt (by design — relationships live in
MEMORY, not injected narration).

### Persistence & SSE

- `data/runs/{run_id}.json` is the full `RunState` snapshot (`storage.py`).
- `data/runs/{run_id}/memory/{agent_id}.md` is the per-agent growing diary.
- `GET /api/runs/{id}/events` is the SSE bus (`events.py`) —
  `typing`, `messages`, `day_start`, `day_end`, `motion_filed`, `vote_cast`,
  `motion_closed`, `trust_updated`, `memory_consolidation_start/done`.
- **Day-end race fix** is invariant: save run BEFORE publishing `day_end` so
  SSE observers see a consistent disk state.

## Conventions

- All agent-facing text is in **Italian**. Tool error strings are in-fiction
  Italian (no English runtime vocabulary).
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

See IMPLEMENTATION.md §6 for the full list. Highlights:

- No unit tests — smoketest + `analyze.py` canaries are the only safety net.
- Message volume is prompt-sensitive (±3× for small wording changes) —
  prefer instrumentation over re-tuning token caps blind.
- UI MEMORY viewer doesn't auto-refresh on `memory_consolidation_done`.
- Only one building authored (`001`, Condominio Via Garibaldi); the UI
  hardcodes it as default payload.
