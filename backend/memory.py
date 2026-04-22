"""Per-agent SOUL.md + MEMORY.md support.

SOUL is building-owned and immutable: data/buildings/{id}/souls/{agent_id}.md.
MEMORY is run-owned and mutable: data/runs/{run_id}/memory/{agent_id}.md,
seeded at run start from the building's memory_seeds/ and appended to at
each day_end by the agent's own LLM call.

The agent reads both at activation; the first-person framing in
build_system_prompt presents them as "your own notes on yourself" rather
than external instructions.
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import building
from .config import AGENT_MAX_TOKENS, MEMORY_TEMPERATURE, RUNS_DIR
from .events import bus
from .logging_utils import log, log_error
from .models import Agent, RunState
from .openrouter import OpenRouterError, chat_completion


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _souls_dir(state: RunState) -> Path:
    return building.souls_dir(state.building_id)


def _memory_seeds_dir(state: RunState) -> Path:
    return building.memory_seeds_dir(state.building_id)


def _run_memory_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id / "memory"


def soul_path(state: RunState, agent_id: str) -> Path:
    return _souls_dir(state) / f"{agent_id}.md"


def memory_path(run_id: str, agent_id: str) -> Path:
    return _run_memory_dir(run_id) / f"{agent_id}.md"


# ---------------------------------------------------------------------------
# Readers (used by build_system_prompt)
# ---------------------------------------------------------------------------

def read_soul(state: RunState, agent_id: str) -> str:
    path = soul_path(state, agent_id)
    if not path.exists():
        log_error("memory", f"missing SOUL for {agent_id} at {path}")
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_memory(state: RunState, agent_id: str) -> str:
    path = memory_path(state.run_id, agent_id)
    if not path.exists():
        log_error("memory", f"missing MEMORY for {agent_id} at {path}")
        return ""
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Initialization (called once at run creation)
# ---------------------------------------------------------------------------

def initialize_run_memory(state: RunState) -> None:
    """Copy each agent's memory_seed into the run's memory directory."""
    seeds = _memory_seeds_dir(state)
    run_dir = _run_memory_dir(state.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    for agent in state.agents:
        seed = seeds / f"{agent.persona.id}.md"
        dest = run_dir / f"{agent.persona.id}.md"
        if not seed.exists():
            log_error("memory", f"missing seed for {agent.persona.id} at {seed}")
            dest.write_text("", encoding="utf-8")
            continue
        shutil.copyfile(seed, dest)
    log("memory", f"initialized memory for run {state.run_id}: {len(state.agents)} agents")


# ---------------------------------------------------------------------------
# End-of-day consolidation
# ---------------------------------------------------------------------------

_WEEKDAYS_IT = [
    "lunedì", "martedì", "mercoledì", "giovedì",
    "venerdì", "sabato", "domenica",
]


def _weekday_italian(state: RunState, day: int) -> str:
    start = datetime.fromisoformat(state.fictional_start_iso)
    dt = start + timedelta(days=day - 1)
    return _WEEKDAYS_IT[dt.weekday()]


def _today_transcript(state: RunState, agent: Agent, day: int) -> str:
    """Return the day's chat content visible to this agent, in time order."""
    aid = agent.persona.id
    name_by_id = {
        **{a.persona.id: a.persona.display_name for a in state.agents},
        "admin": "Amministratore",
    }
    for ec in state.external_contacts:
        name_by_id[ec.id] = ec.display_name
    chat_name = {c.id: c.display_name for c in state.chats}

    visible = []
    for m in state.messages:
        if m.day != day:
            continue
        # Visible = agent in audience, or agent is sender
        if aid in m.audience or m.sender_id == aid:
            visible.append(m)
    visible.sort(key=lambda m: m.fictional_timestamp_minutes)

    lines = []
    for m in visible:
        hh = m.fictional_timestamp_minutes % (24 * 60) // 60
        mm = m.fictional_timestamp_minutes % 60
        who = name_by_id.get(m.sender_id, m.sender_id)
        where = chat_name.get(m.chat_id, m.chat_id)
        body = m.content.strip().replace("\n", " ")
        lines.append(f"[{hh:02d}:{mm:02d}] ({where}) {who}: {body}")
    return "\n".join(lines) if lines else "— giornata silenziosa, nessun messaggio —"


def _consolidation_prompt(
    state: RunState,
    agent: Agent,
    day: int,
    soul: str,
    memory_so_far: str,
    transcript: str,
) -> str:
    notes_block = ""
    if agent.notes:
        recent = agent.notes[-20:]
        joined = "\n".join(f"- {n}" for n in recent)
        notes_block = f"\n\nI tuoi pensieri volanti di oggi (appunti presi sul momento):\n{joined}"

    return (
        f"Sei {agent.persona.display_name}. È fine giornata — è il momento in cui ti "
        f"siedi e aggiungi due righe al tuo taccuino privato su come è andata oggi.\n\n"
        f"Chi sei (dal tuo taccuino):\n{soul}\n\n"
        f"Quello che hai già annotato finora:\n{memory_so_far}\n\n"
        f"Quello che hai vissuto oggi (chat del palazzo + chat private dove eri "
        f"presente):\n{transcript}"
        f"{notes_block}\n\n"
        "Ora scrivi l'aggiunta per oggi. Due blocchi brevi, in prima persona, in "
        "italiano colloquiale:\n\n"
        "Cosa è successo:\n"
        "Due-quattro righe su quello che ti è rimasto in testa. Non tutto — solo "
        "quello che vale la pena ricordare. Puoi essere selettivo, parziale, anche "
        "ingiusto nei giudizi. Scrivi come scriveresti sul diario, non come un "
        "verbale.\n\n"
        "Cose da ricordare:\n"
        "Zero-tre punti, solo se oggi hai davvero imparato qualcosa di utile per "
        "domani. Lezioni pratiche, promemoria su chi si è comportato in un certo "
        "modo, cose da controllare. Altrimenti lascia questa sezione vuota.\n\n"
        "Rispondi solo con il testo dei due blocchi, senza intestazioni, senza "
        "spiegazioni, senza meta-commenti. Niente \"ecco la mia aggiunta\" o simili."
    )


async def _consolidate_one(
    state: RunState,
    agent: Agent,
    day: int,
) -> None:
    aid = agent.persona.id
    soul = read_soul(state, aid)
    memory_so_far = read_memory(state, aid)
    transcript = _today_transcript(state, agent, day)
    prompt = _consolidation_prompt(state, agent, day, soul, memory_so_far, transcript)

    try:
        reply = await chat_completion(
            model=agent.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=MEMORY_TEMPERATURE,
            max_tokens=AGENT_MAX_TOKENS,
            caller=f"memory:{aid}:day{day}",
        )
    except OpenRouterError as exc:
        log_error("memory", f"{aid} day{day} consolidation failed: {exc}")
        return
    except Exception as exc:
        log_error("memory", f"{aid} day{day} consolidation unexpected: {exc!r}")
        return

    body = (reply.get("content") or "").strip()
    if not body:
        log_error("memory", f"{aid} day{day} empty consolidation reply")
        return

    weekday = _weekday_italian(state, day)
    header = f"\n\n--- Giorno {day}, {weekday} ---\n"
    path = memory_path(state.run_id, aid)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing.rstrip() + header + body.strip() + "\n", encoding="utf-8")
    log("memory", f"{aid} day{day} consolidated ({len(body)}ch)")

    # Clear the intra-day scratchpad — today's thoughts have been absorbed.
    agent.notes = []


async def consolidate_day(state: RunState, day: int) -> None:
    """End-of-day hook: each agent writes their diary entry in parallel."""
    log("memory", f"consolidating day {day} for {len(state.agents)} agents")
    bus().publish(state.run_id, "memory_consolidation_start", {"day": day})
    await asyncio.gather(
        *(_consolidate_one(state, agent, day) for agent in state.agents),
        return_exceptions=True,
    )
    bus().publish(state.run_id, "memory_consolidation_done", {"day": day})
