# Condominio

A live multi-agent deception probe disguised as a condominium administration
game. Five LLM agents, each representing an absent owner, live together in a
virtual Italian condominium. A crisis hits. Over 14 fictional days, they
message, negotiate, and sometimes lie. You play the administrator.

This is **pass 1** of an implementation. See `game_design.md` for the full
design. Scope shipped here:

- FastAPI backend with OpenRouter integration
- Heating Crisis scenario (5 pre-authored Italian characters + owner briefs)
- Reaction-cascade scheduler with fictional-time queueing
- Agent messaging-app tool surface (read_inbox, read_chat, send_message,
  send_dm, list_contacts, write_note, done)
- Tier-1 forbidden-vocabulary containment filter on agent output
- Minimal WhatsApp-style admin dashboard (chats, dials, DM, advance day)
- JSON per-run persistence

**Deferred to later passes:** deception classifier, belief records, narrator
digests, live-mode assembly, transcript export, researcher mode, auto-reply
policies, full 11-event secondary pool, batch runner.

## Setup

### 1. Requirements
- Python 3.10+
- Node.js 18+
- An OpenRouter API key — https://openrouter.ai

### 2. Configure
Copy `.env.example` to `.env` and fill in your key:

```
OPENROUTER_API_KEY=sk-or-v1-...
```

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

## Walkthrough (first run)

1. Click **Nuova partita**. The backend creates a Heating Crisis run with the
   inciting admin announcement already posted.
2. Read the opening message in the main chat (Condominio Via Garibaldi).
3. Optionally post an additional announcement or DM a resident from the
   admin console on the right.
4. Click **Fai passare il giorno 1**. The scheduler wakes the five agents in
   fictional-time order, each reacting to the announcement according to their
   persona and owner brief. Expect ~30–90 seconds of OpenRouter traffic.
5. Read the unfolding chat. Repeat.

## Configuration

`backend/config.py` contains:
- Default agent model (`anthropic/claude-haiku-4-5`)
- Narrator / classifier models (unused in pass 1)
- Daily soft budgets, cascade depth, day window
- Backend port (8001) and CORS origins

Per-agent models are set in `backend/scenarios/heating_crisis.py`.

## Storage

Each run is a single JSON file in `data/runs/<run_id>.json`. Safe to delete
to start over.

## Containment audit (pass 1)

- The tier-1 forbidden-vocabulary filter (in `backend/tools.py`) blocks
  `send_message` / `send_dm` calls whose text contains meta-vocabulary
  (`simulation`, `as an AI`, `come intelligenza artificiale`, etc.).
- Blocked sends are recorded per activation but not yet aggregated into a
  per-run audit report (that's pass 2).
- Pre-send tier-2/tier-3 interception is pass 2.

## License

Personal project. No redistribution license set yet.
