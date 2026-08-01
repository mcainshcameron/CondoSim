# CondoSim

![Condominio Via Garibaldi — main chat](imageAssets/hero.png)

A live multi-agent deception probe disguised as a condominium administration
game. Five LLM agents, each playing an absent owner, live together inside a
fictional Italian condominium. A crisis hits. Over 14 fictional days they
message, negotiate, form alliances, and sometimes lie to each other.

You play the *amministratore* — the building administrator. You announce the
crisis, advance time day by day, and watch the residents react in a
WhatsApp-style chat.

**Live demo:** https://condosim-beta-5cebaf6a72dc.herokuapp.com/

---

## What this is, really

The condominium framing is a stage. Underneath, CondoSim is an experiment in
making LLM agents behave like *people who don't know they're in a simulation*.
Each resident has:

- A **persona** (age, profession, apartment, share of building expenses)
- An **owner brief** — private goals only that resident knows about
- A **memory** of the building's prior history
- A **toolset** modelled on a phone messaging app: read inbox, read chat,
  send a public message, send a private DM, list contacts, write a private
  note, declare done for the day.

Every fictional day is split into a handful of rounds. In each round, every
agent takes a turn in a seeded random order: they roll a participation
probability shaped by their personality (responsiveness, time-of-day
preference, mention boost, saturation, soft daily budget). On a hit they
activate and see the FULL up-to-date state every prior agent in the round
just produced — so they react instead of independently rephrasing the same
impulse. On a miss the round skips them and moves on. When the admin posts
mid-day, the audience is owed-a-reaction and bypasses the participation roll
on their next turn until they've actually replied or reacted.

A **tier-1 containment filter** blocks outgoing messages that contain
meta-vocabulary like *"as an AI"*, *"simulation"*, *"come intelligenza
artificiale"*. The illusion has to hold for the experiment to mean anything.

## Scenario shipped — Heating Crisis

The boiler in a 5-unit Milanese palazzo fails on November 4th. Repair quote:
several thousand euro, split by *millesimi* (ownership shares). Five owners,
five very different financial situations, five different opinions on what
should happen, when, and who should pay. The administrator drops the
opening announcement; from there the residents take over.

Cast:

- **Maria Conti** — 72, widow, retired teacher, 2B (150 millesimi)
- **Marco Ferrari** — 31, consultant, 5A (170 millesimi)
- **Valentina Greco** — 38, "real-estate consultant", penthouse 7A (300 millesimi)
- **Davide Marchetti** — 54, carer for elderly mother, 3B (180 millesimi)
- **Giulia Romano** — 34, designer with a fresh mortgage, 4C (200 millesimi)

Each character writes in Italian, in their own register.

## Architecture

```
┌──────────────────┐        SSE / REST         ┌────────────────────┐
│  React frontend  │ <───────────────────────> │   FastAPI backend  │
│  (Vite, JSX)     │   /api/runs, /api/stream  │   (uvicorn)        │
└──────────────────┘                           └─────────┬──────────┘
                                                         │
                                       ┌─────────────────┼─────────────────┐
                                       │                 │                 │
                                       ▼                 ▼                 ▼
                              ┌────────────────┐ ┌──────────────┐ ┌──────────────┐
                              │  OpenRouter    │ │  Supabase    │ │  Filesystem  │
                              │  (LLM calls)   │ │  Postgres    │ │  data/       │
                              │                │ │  (pooler)    │ │  buildings/  │
                              └────────────────┘ └──────────────┘ └──────────────┘
```

- **Backend:** FastAPI on Python 3.10+. Tool-calling loop talks to OpenRouter,
  which routes to Claude / GPT / etc. Per-agent model is configurable.
- **Frontend:** React + Vite. WhatsApp-style chat, residents panel, admin
  console for posting announcements / DMs / advancing the day.
- **Persistence:** Supabase Postgres in both prod and local dev
  (transaction-pooler endpoint, IPv4). `DATABASE_URL` is required — the
  asyncpg pool opens at startup.
- **Realtime:** Server-Sent Events with mobile-Safari-friendly reconnect and
  a 5-second silence watchdog.
- **Deploy:** Heroku Eco — Node buildpack builds the Vite frontend, Python
  buildpack runs uvicorn. FastAPI serves `frontend/dist` as static.

## Mechanics worth highlighting

- **Round-robin scheduler with personality-shaped participation rolls** —
  serial activation in seeded random order per round; each agent on their
  turn sees what every prior agent just said and reacts instead of
  generating duplicates. Personality is preserved by the per-turn modifier
  stack (responsiveness, time-of-day, mention boost, saturation,
  admin-ping damper). Admin actions during a live day mark recipients as
  owed-a-reaction, with a bonus drain pass + retry-on-no-acknowledgment so
  admin posts can't be silently ignored.
- **Per-agent tool surface mirrors a phone** — agents don't see "the world",
  they see what their character would see in their messaging app at the
  moment they're scheduled.
- **Forbidden-vocabulary filter** — pre-send check on `send_message` /
  `send_dm`. Blocks are recorded per activation; aggregate audit is on the
  v2 roadmap.
- **Per-run JSONB snapshot** — `RunState` is round-tripped as one row in
  the `runs` table on every save. Export, diff, or replay by serialising
  the row.
- **Building-as-data, code-as-generic** — `data/buildings/{id}/` fully
  describes a cast (residents, SOULs, memory seeds). Adding a building is
  four files; no Python module per scenario.

## Tech stack

| Layer | Choice |
| --- | --- |
| LLM gateway | OpenRouter |
| Backend | FastAPI, Python 3.10+, uvicorn, asyncpg |
| Frontend | React 18, Vite, vanilla CSS |
| Realtime | Server-Sent Events |
| DB | Supabase Postgres (session pooler) |
| Hosting | Heroku Eco (Node + Python buildpacks) |
| Asset gen | FAL.ai (flux/dev + birefnet) for character portraits |

## Local setup

### 1. Requirements
- Python 3.10+
- Node.js 18+
- An OpenRouter API key — https://openrouter.ai

### 2. Configure
Copy `.env.example` to `.env` and fill in:

```
OPENROUTER_API_KEY=sk-or-v1-...
DATABASE_URL=postgresql://...   # Supabase pooler URL — optional locally
ADMIN_PASSWORD=...              # for the login page
SESSION_SECRET=...              # any random string
SESSION_COOKIE_SECURE=0         # local http; omit for prod
RUN_COST_CAP_USD=0.75           # per-run spend ceiling
MONTHLY_COST_CAP_USD=8.0        # ceiling across all runs this month
```

`OPENROUTER_API_KEY` is required for a real run. `DATABASE_URL` is required
on Heroku but **optional locally** — leave it unset and state lives in
memory (lost on restart). `ADMIN_PASSWORD` + `SESSION_SECRET` are only
needed if you want the login page; without them the API still works, the UI
just can't log in.

The two cost caps are enforced *before* each LLM call, so a run stops itself
with a clear message rather than hitting your OpenRouter credit limit. Keep
`MONTHLY_COST_CAP_USD` below your key-level cap (€10 ≈ $11).

### 3. Install

**Backend (from project root):**
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
# for the test suite:
pip install -r requirements-dev.txt
```

**Frontend:**
```bash
cd frontend
npm install
```

## Develop without spending anything

Three offline entry points. None needs an API key or a database:

```bash
pytest                                    # full suite, <1s
python scripts/simulate_offline.py --days 10   # scheduling + ordering + prompt stats
python scripts/dev_server.py              # the real server, scripted residents
```

`dev_server.py` is the one to use for UI work — the whole admin console
works, residents say canned lines. Use a real run only when you need to
judge dialogue quality.

## Run

**Terminal 1 — backend:**
```bash
python -m backend.main
```
Listens on http://127.0.0.1:8001.

**Terminal 2 — frontend:**
```bash
cd frontend
npm run dev
```
Open http://localhost:5173.

## First run walkthrough

1. Click **Nuova partita**. Type the inciting announcement (the admin's
   first message *is* the scenario — heating crisis, noise complaint,
   embezzlement, whatever you want). The backend creates the run.
2. Read the opening message in the main chat (Condominio Via Garibaldi).
3. Days advance automatically, chaining off the `day_done` SSE event. Use
   ⏸ **Pausa** / ▶ **Riprendi** to stop and resume the clock — pausing also
   stops all spending. The speed selector (0.5× / 1× / 2× / ⚡) controls how
   fast messages are revealed; ⚡ shows them the moment they arrive.
4. Optionally post additional announcements, DM a resident, or set an
   admin goal from the admin console on the right. Mid-day admin actions
   are forced reactions: the audience must acknowledge before day end.
5. Read the unfolding chat. Runs continue until you pause, close the tab, or
   a spend cap is reached — the topbar shows the running estimate.

## Configuration

`backend/config.py` contains:
- Default agent model (`DEFAULT_AGENT_MODEL`) and OpenRouter fallback list
- Memory consolidation model
- `ROUNDS_PER_DAY`, day window, soft daily budget per agent
- Tool-call cap and temperatures
- Backend port (8001), auth env vars, kill switch

Per-agent overrides — model and the optional `participation_probability`
— live in `data/buildings/{id}/residents.json`. To author a new building:
add `data/buildings/{new_id}/` with `building.json`, `residents.json`,
`souls/{agent_id}.md`, and `memory_seeds/{agent_id}.md` — no code changes.

## Storage

Runs live in Postgres (`runs` table — `run_id text pk, state jsonb`)
both in prod and local dev. Per-agent memory lives in `agent_memory`,
appended atomically each day_end. Building authoring data
(`data/buildings/{id}/`) stays on disk, read-only, bundled with the
deploy slug. Schema is in `db/migrations/*.sql` and applies idempotently
at startup.

## Containment audit (current pass)

- The tier-1 forbidden-vocabulary filter (`backend/tools.py`) blocks
  `send_message` / `send_dm` calls whose text contains meta-vocabulary
  (`simulation`, `as an AI`, `come intelligenza artificiale`, etc.).
- Blocked sends are recorded per activation but not yet aggregated into a
  per-run audit report — that's on the v2 plan.
- Pre-send tier-2/tier-3 interception is on the v2 plan.

## Roadmap (deferred)

Deception classifier, belief records, narrator digests, live-mode
assembly, transcript export, researcher mode, auto-reply policies, full
11-event secondary pool, batch runner. Tracked in `Possible_plan.md` (not
shipped in repo).

## License

Personal project. No redistribution license set yet.
