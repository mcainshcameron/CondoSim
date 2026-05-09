"""Per-agent SOUL.md + MEMORY support.

SOUL is building-owned and immutable: data/buildings/{id}/souls/{agent_id}.md.
MEMORY is run-owned and mutable: stored in Postgres (table `agent_memory`),
seeded at run start from the building's memory_seeds/ files and appended to
at each day_end by the agent's own LLM call.

The agent reads both at activation; the first-person framing in
build_system_prompt presents them as "your own notes on yourself" rather
than external instructions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from . import building
from .config import AGENT_MAX_TOKENS, MEMORY_TEMPERATURE
from .db import pool
from .events import bus
from .logging_utils import log, log_error
from .models import Agent, RunState
from .openrouter import OpenRouterError, chat_completion, record_usage


# ---------------------------------------------------------------------------
# Paths (SOUL + seed files only — these stay on disk, bundled with the deploy)
# ---------------------------------------------------------------------------

def _souls_dir(state: RunState) -> Path:
    return building.souls_dir(state.building_id)


def _memory_seeds_dir(state: RunState) -> Path:
    return building.memory_seeds_dir(state.building_id)


def soul_path(state: RunState, agent_id: str) -> Path:
    return _souls_dir(state) / f"{agent_id}.md"


# ---------------------------------------------------------------------------
# Readers (used by build_system_prompt)
# ---------------------------------------------------------------------------

def read_soul(state: RunState, agent_id: str) -> str:
    path = soul_path(state, agent_id)
    if not path.exists():
        log_error("memory", f"missing SOUL for {agent_id} at {path}")
        return ""
    return path.read_text(encoding="utf-8").strip()


async def read_memory(state: RunState, agent_id: str) -> str:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "select content from agent_memory where run_id = $1 and agent_id = $2",
            state.run_id,
            agent_id,
        )
    if row is None:
        log_error("memory", f"missing MEMORY for {agent_id} in run {state.run_id}")
        return ""
    return (row["content"] or "").strip()


# ---------------------------------------------------------------------------
# Initialization (called once at run creation, after save_run)
# ---------------------------------------------------------------------------

async def initialize_run_memory(state: RunState) -> None:
    """Seed each agent's MEMORY row from their building memory_seed file."""
    seeds_dir = _memory_seeds_dir(state)
    rows: list[tuple[str, str, str]] = []
    for agent in state.agents:
        seed = seeds_dir / f"{agent.persona.id}.md"
        if not seed.exists():
            log_error("memory", f"missing seed for {agent.persona.id} at {seed}")
            content = ""
        else:
            content = seed.read_text(encoding="utf-8")
        rows.append((state.run_id, agent.persona.id, content))

    async with pool().acquire() as conn:
        await conn.executemany(
            """
            insert into agent_memory (run_id, agent_id, content)
            values ($1, $2, $3)
            on conflict (run_id, agent_id) do update
              set content = excluded.content,
                  updated_at = now()
            """,
            rows,
        )
    log("memory", f"initialized memory for run {state.run_id}: {len(rows)} agents")


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


def _visible_message_count_today(state: RunState, agent: Agent, day: int) -> int:
    """How many messages this agent saw on `day` (sent or received).

    Used as the silent-day guard before consolidation: when this is too low,
    asking the model to write a diary entry produces fabricated events
    rather than an honest "nothing happened". The fix is to not call the
    model in the first place, not to filter the result.
    """
    aid = agent.persona.id
    return sum(
        1 for m in state.messages
        if m.day == day and (aid in m.audience or m.sender_id == aid)
    )


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


SILENT_DAY_THRESHOLD = 2  # below this many visible messages, skip the LLM call


async def _consolidate_one(
    state: RunState,
    agent: Agent,
    day: int,
) -> None:
    aid = agent.persona.id

    # Silent-day guard: with too little input, the model cannot honestly
    # write "nothing happened" — it fills the slot with invented events
    # (a phone call from the geometra, a cellar mold problem, a new tenant
    # nobody mentioned). Once that fiction lands in MEMORY it bleeds into
    # every subsequent activation. Cheaper and more honest: append a fixed
    # marker and skip the call entirely.
    visible = _visible_message_count_today(state, agent, day)
    if visible < SILENT_DAY_THRESHOLD:
        weekday = _weekday_italian(state, day)
        addition = (
            f"\n\n--- Giorno {day}, {weekday} ---\n"
            f"Giornata silenziosa, niente da segnare.\n"
        )
        async with pool().acquire() as conn:
            await conn.execute(
                """
                update agent_memory
                   set content = rtrim(content, E' \\t\\n\\r') || $3,
                       updated_at = now()
                 where run_id = $1 and agent_id = $2
                """,
                state.run_id,
                aid,
                addition,
            )
        log("memory", f"{aid} day{day} silent (visible={visible}), skipped LLM consolidation")
        agent.notes = []
        return

    soul = read_soul(state, aid)
    memory_so_far = await read_memory(state, aid)
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
        record_usage(state, reply)
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
    addition = f"\n\n--- Giorno {day}, {weekday} ---\n{body.strip()}\n"

    # Atomic append in SQL: trim trailing whitespace, concat the day entry.
    # rtrim with the whitespace set strips any combination of spaces / tabs /
    # newlines that may have accumulated, mirroring Python's str.rstrip().
    async with pool().acquire() as conn:
        await conn.execute(
            """
            update agent_memory
               set content = rtrim(content, E' \\t\\n\\r') || $3,
                   updated_at = now()
             where run_id = $1 and agent_id = $2
            """,
            state.run_id,
            aid,
            addition,
        )
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
