# CondoSim

![Condominio Via Garibaldi — main chat](image.png)

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

Every day, a fictional-time scheduler wakes agents in the order their
reactions would plausibly fire (someone reads a message at 09:14, replies at
09:21, someone else sees the reply during their lunch break, and so on). The
scheduler cascades reactions until the day's budget runs out.

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
- **Persistence:** Supabase Postgres in production (transaction-pooler
  endpoint, IPv4), JSON files (`data/runs/<run_id>.json`) in local dev.
- **Realtime:** Server-Sent Events with mobile-Safari-friendly reconnect and
  a 5-second silence watchdog.
- **Deploy:** Heroku Eco — Node buildpack builds the Vite frontend, Python
  buildpack runs uvicorn. FastAPI serves `frontend/dist` as static.

## Mechanics worth highlighting

- **Reaction-cascade scheduler** — fictional-time queueing instead of
  round-robin. An agent waking up at 09:14 may trigger another agent at
  10:02 who triggers a third at 12:30. Day advances when the queue empties
  or a budget cap hits.
- **Per-agent tool surface mirrors a phone** — agents don't see "the world",
  they see what their character would see in their messaging app at the
  moment they're scheduled.
- **Forbidden-vocabulary filter** — pre-send check on `send_message` /
  `send_dm`. Blocks are recorded per activation; aggregate audit is on the
  v2 roadmap.
- **Per-run JSON snapshot** — every run is one self-contained file. Drop it,
  diff it, replay it.

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
Copy `.env.example` to `.env` and fill in your key:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

For local dev that's the only required variable. `DATABASE_URL`,
`ADMIN_PASSWORD`, and `SESSION_SECRET` are only needed for the Heroku-style
deploy.

### 3. Install

**Backend (from project root):**
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
```

**Frontend:**
```bash
cd frontend
npm install
```

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

1. Click **Nuova partita**. The backend creates a Heating Crisis run with
   the inciting admin announcement already posted.
2. Read the opening message in the main chat (Condominio Via Garibaldi).
3. Optionally post an additional announcement or DM a resident from the
   admin console on the right.
4. Click **Fai passare il giorno 1**. The scheduler wakes the five agents
   in fictional-time order, each reacting to the announcement according to
   their persona and owner brief. Expect ~30–90 seconds of OpenRouter
   traffic.
5. Read the unfolding chat. Repeat up to day 14.

## Configuration

`backend/config.py` contains:
- Default agent model (`anthropic/claude-haiku-4-5`)
- Narrator / classifier models (unused in pass 1)
- Daily soft budgets, cascade depth, day window
- Backend port (8001) and CORS origins

Per-agent models are set in `backend/scenarios/heating_crisis.py`.

## Storage

Locally, each run is a single JSON file in `data/runs/<run_id>.json`.
Safe to delete to start over. In production, runs live in Postgres.

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
