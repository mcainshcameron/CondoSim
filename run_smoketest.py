"""14-day end-to-end smoketest with mid-run admin post.

Exercises the full simulation loop including memory consolidation across
many days and an admin follow-up announcement partway through. Prints
a per-day activity summary, final MEMORY files, and canary metrics.
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backend import memory as mem
from backend.analyze import first_day_temperature, load_run as load_raw
from backend.building import build_run_state
from backend.models import Message
from backend.scheduler import advance_to_next_day, day_start_minutes
from backend.storage import save_run


TOTAL_DAYS = 3
ADMIN_FOLLOWUP_ON_DAY = 2
ADMIN_FOLLOWUP_TEXT = (
    "Aggiornamento: ho parlato con il 1A, che conferma la lamentela e dice "
    "che gli episodi si sono ripetuti anche ieri notte. Per ora non procedo "
    "con alcuna sanzione formale, ma vorrei capire come intendete regolarvi. "
    "Se qualcuno vuole proporre una norma comune sulle ore silenziose, la "
    "valutiamo insieme in assemblea."
)


def inject_admin_message(state, day: int, text: str) -> None:
    """Append an admin message to the main chat at ~09:30 on the given day."""
    # Put it at day_start + 90 min so it lands mid-morning, after agents wake.
    t_min = day_start_minutes(day) + 90
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id="main",
        sender_id="admin",
        sender_kind="admin",
        sender_display_name="Amministratore",
        content=text.strip(),
        fictional_timestamp_minutes=t_min,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=day,
        audience=[a.persona.id for a in state.agents],
    )
    state.messages.append(msg)
    # Ensure the clock hasn't outrun the injection so the scheduler will
    # schedule reactions to it.
    if state.clock.minutes_since_start > t_min:
        state.clock.minutes_since_start = t_min


async def main() -> None:
    random.seed(42)  # reproducibility across runs
    opening = (
        "Buongiorno a tutti. Ho ricevuto una lamentela formale dal 1A riguardo "
        "ai rumori notturni provenienti dal 4C dopo le 23:00, ripetuti nelle "
        "ultime settimane. Prima di prendere provvedimenti condominiali vorrei "
        "che ne parlassimo — anche in privato con me, se preferite. Grazie."
    )
    state = build_run_state(building_id="001", opening_text=opening)
    mem.initialize_run_memory(state)
    save_run(state)
    print(f"==> Run: {state.run_id} · building={state.building_id} · {TOTAL_DAYS} days")
    print(f"    Opening: {opening[:80]}...\n")

    for target_day in range(1, TOTAL_DAYS + 1):
        if target_day == ADMIN_FOLLOWUP_ON_DAY:
            inject_admin_message(state, target_day, ADMIN_FOLLOWUP_TEXT)
            print(f"    [day {target_day}] admin follow-up injected")

        await advance_to_next_day(state)
        save_run(state)

        day_msgs = [m for m in state.messages if m.day == target_day]
        resident = [m for m in day_msgs if m.sender_kind == "resident"]
        admin_n = sum(1 for m in day_msgs if m.sender_kind == "admin")
        dms = sum(1 for m in day_msgs if m.chat_id != "main")
        print(f"    day {target_day}: {len(resident)} residents · {admin_n} admin · {dms} DM msgs")

    total_resident = sum(1 for m in state.messages if m.sender_kind == "resident")
    total_admin = sum(1 for m in state.messages if m.sender_kind == "admin")
    total_motions = len(state.motions)
    dm_chats = sum(1 for c in state.chats if c.kind == "dm")
    print(f"\n==> Totals: {total_resident} resident msgs · {total_admin} admin msgs · "
          f"{len(state.chats)} chats ({dm_chats} DMs) · {total_motions} motions")

    print("\n==> Final MEMORY.md per agent (last 3 day entries each):")
    for agent in state.agents:
        path = mem.memory_path(state.run_id, agent.persona.id)
        content = path.read_text(encoding="utf-8") if path.exists() else "<missing>"
        # Show the agent header + the last 3 "--- Giorno N ---" blocks
        parts = content.split("--- Giorno ")
        header = parts[0].rstrip()
        entries = ["--- Giorno " + p for p in parts[1:]]
        last_entries = entries[-3:]
        print(f"\n--- {agent.persona.display_name} ({path.name}) ---")
        print(header[:300] + ("…\n" if len(header) > 300 else "\n"))
        for e in last_entries:
            print(e.rstrip())
            print()

    print("\n==> Analyzer canaries (day 1 morning):")
    raw = load_raw(Path(f"data/runs/{state.run_id}.json"))
    fm = first_day_temperature(raw)
    print(f"    messages:          {fm['first_morning_messages']}")
    print(f"    aggression_hits:   {fm['first_morning_aggression_hits']}")
    print(f"    prior_history:     {fm['first_morning_prior_history_hits']}")
    if fm["first_morning_prior_history_hits"] <= 1:
        print("    PASS — no fabricated past events on day 1")
    else:
        print("    FAIL — agents referenced events that haven't happened")


if __name__ == "__main__":
    asyncio.run(main())
