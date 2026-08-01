"""Run the simulation offline against a scripted LLM and report the numbers.

No API key, no database, no cost. Use this to sanity-check scheduling,
ordering and prompt size after a change, instead of reaching for
`run_smoketest.py` (which is slow and spends real credits).

    python scripts/simulate_offline.py --days 10
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Offline mode must be pinned before backend.config runs load_dotenv().
os.environ["DATABASE_URL"] = ""
os.environ.setdefault("OPENROUTER_API_KEY", "offline")
os.environ["RUN_COST_CAP_USD"] = "0"
os.environ["MONTHLY_COST_CAP_USD"] = "0"
os.environ.setdefault("VERBOSE_LOGGING", "0")

from backend import building, llm, memory, scheduler, storage, timeline  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402


async def main(days: int, opening: str) -> int:
    fake = FakeLLM()
    llm.set_transport(fake)

    state = building.build_run_state(building_id="001", opening_text=opening)
    await storage.save_run(state)
    await memory.initialize_run_memory(state)

    for _ in range(days):
        await scheduler.advance_to_next_day(state)
        if state.ended:
            break

    activations = fake.activation_calls()
    steps = [int(c["caller"].rsplit(":step", 1)[1]) for c in activations]
    distinct_activations = sum(1 for s in steps if s == 0)
    prompt_chars = [
        sum(len(str(m.get("content") or "")) for m in c["messages"])
        for c in activations
        if c["caller"].endswith(":step0")
    ]
    resident_msgs = [m for m in state.messages if m.sender_kind == "resident"]

    ordered = timeline.in_order(state.messages)
    minutes = [m.fictional_timestamp_minutes for m in ordered]
    seqs = [m.seq for m in ordered]
    monotonic = minutes == sorted(minutes)
    unique_seq = len(set(seqs)) == len(seqs)

    print(f"\n{'=' * 58}")
    print(f"  OFFLINE SIMULATION — {state.clock.day} day(s), building 001")
    print(f"{'=' * 58}")
    print(f"  Activations              {distinct_activations}")
    print(f"  LLM calls (activation)   {len(activations)}")
    print(f"  Calls per activation     {len(activations) / max(1, distinct_activations):.2f}")
    print(f"  LLM calls (total)        {state.metrics.llm_calls}")
    print(f"  Resident messages        {len(resident_msgs)}")
    print(f"  Msgs / day               {len(resident_msgs) / max(1, state.clock.day):.1f}")
    if prompt_chars:
        print(f"  Prompt chars  avg/max    {sum(prompt_chars) // len(prompt_chars)} / {max(prompt_chars)}")
    print(f"  Prompt tokens (reported) {state.metrics.prompt_tokens}")
    print(f"\n  Ordering: time monotonic {'OK' if monotonic else 'BROKEN'}"
          f"   seq unique {'OK' if unique_seq else 'BROKEN'}")
    if state.ended:
        print(f"  Run ended: {state.ended_reason}")
    print(f"{'=' * 58}\n")

    return 0 if (monotonic and unique_seq) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument(
        "--opening",
        default="Buongiorno a tutti. La caldaia centralizzata e' ferma da stanotte.",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.opening)))
