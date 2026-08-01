"""Score how well the residents hold their own voices, against the real LLM.

`simulate_offline.py` proves the mechanics work; the fake LLM cannot tell you
whether the residents still sound like five different people. This does, and
cheaply enough to run before and after a prompt change (~$0.01 for 3 days on
DeepSeek V4 Flash).

    python scripts/eval_voices.py --days 3 --label before
    python scripts/eval_voices.py --days 3 --label after   # same --run-id!

`--run-id` is pinned by default so the scheduler's participation rolls are
identical between two runs (see scheduler._seed_for) — otherwise you are
comparing prompt changes against schedule noise and cannot tell them apart.

Metrics, all derived from the saved transcript:

  assistant-speak  Polite-helpdesk register bleeding through the persona
                   ("resto a disposizione", "grazie per la comunicazione").
                   This is the "assistant axis" drift documented for
                   long-running persona agents. Lower is better.
  echo             Pairwise content-word overlap between DIFFERENT residents
                   in the same chat on the same day. Round-robin shows each
                   agent what the previous ones just said, which invites
                   mirroring their stance and wording. Lower is better.
  distinctiveness  Share of a resident's content words that no other resident
                   used. Higher is better.
  voice violations SOUL-declared speech rules the resident broke — Conti says
                   she uses no emoji at all, Ferrari "niente emoji gratuite".
                   A generic prompt rule that pushes emoji on everyone shows
                   up here. Lower is better.
  length           Median message length. The SOULs and the brevity examples
                   both ask for WhatsApp-short; drift is usually upward.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# In-memory store; no Postgres needed. Must be set before backend.config runs
# load_dotenv(). The API key is NOT stubbed — this eval needs the real model.
os.environ["DATABASE_URL"] = ""
os.environ.setdefault("VERBOSE_LOGGING", "0")

from backend import building, memory, scheduler, storage  # noqa: E402
from backend.analyze import PRIOR_HISTORY_RE  # noqa: E402

# --------------------------------------------------------------------------
# Scoring vocabulary
# --------------------------------------------------------------------------

# Polite-helpdesk register. These are things a neighbour on WhatsApp does not
# say and a customer-service bot does.
ASSISTANT_TERMS = [
    r"\bresto a disposizione\b", r"\bsono a disposizione\b",
    r"\brimango a disposizione\b", r"\ba vostra disposizione\b",
    r"\bgrazie per (?:la comunicazione|l'avviso|l'informazione|il chiarimento)\b",
    r"\bnon esitate\b", r"\bnon esiti\b",
    r"\bcordiali saluti\b", r"\bdistinti saluti\b",
    r"\bvi terr[òo] aggiornati\b", r"\bti terr[òo] aggiornato\b",
    r"\bper qualsiasi (?:dubbio|necessit[àa]|chiarimento|cosa)\b",
    r"\bspero di essere stat[oa] (?:utile|chiaro)\b",
    r"\bmi adeguo\b", r"\bprendo atto\b",
    r"\bin merito a (?:quanto|ci[òo])\b",
    r"\bcomprendo (?:perfettamente|le vostre)\b",
    r"\bcerto(?:amente)?, (?:capisco|comprendo)\b",
    r"\bsono d'accordo con (?:quanto|la)\b",
]
ASSISTANT_RE = re.compile("|".join(ASSISTANT_TERMS), re.IGNORECASE)

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF❤️]"
)

# Italian function words — excluded before measuring overlap, otherwise every
# pair of messages "echoes" simply by speaking Italian.
STOP = set("""
a ad ai al all alla alle allo anche c che chi ci co coi col come con cosa cui
d da dai dal dall dalla dalle dallo degli dei del dell della delle dello di do
dov dove e ed gli ha hai hanno ho i il in io l la le lei li lo loro ma me mi
mia mie miei mio ne negli nei nel nell nella nelle nello no noi non nostro o
per perche perché pero però pi più po po' qua qual quale quando quanto quel
quella quelle quelli quello questa queste questi questo qui sa sara sarà se
sei si sia siamo sono su sua sue sui sul sull sulla sulle suo suoi ti tra tu
tuo un una uno vi voi ce se ci gia già solo anche tutto tutti tutta piu essere
fare detto ecco poi mai ogni sempre nulla niente cosi così bene male
""".split())

WORD_RE = re.compile(r"[a-zàèéìòùA-ZÀÈÉÌÒÙ']{3,}")

# Speech rules each SOUL states outright. Only rules explicit enough to score.
VOICE_RULES = {
    # "zero faccine ridicole (non so nemmeno come si usano...)"
    "conti": {"max_emoji_rate": 0.0, "rule": "SOUL: usa zero emoji"},
    # "Niente emoji gratuite. Messaggi brevi."
    "ferrari": {"max_emoji_rate": 0.15, "rule": "SOUL: niente emoji gratuite"},
}


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text)} - STOP


def score(state, label: str) -> dict:
    msgs = [m for m in state.messages if m.sender_kind == "resident" and (m.content or "").strip()]
    by_agent: dict[str, list[str]] = defaultdict(list)
    for m in msgs:
        by_agent[m.sender_id].append(m.content)

    names = {a.persona.id: a.persona.display_name for a in state.agents}

    # --- assistant-speak -------------------------------------------------
    assistant_hits, assistant_examples = 0, []
    for m in msgs:
        found = ASSISTANT_RE.findall(m.content)
        if found:
            assistant_hits += len(found)
            if len(assistant_examples) < 6:
                assistant_examples.append(
                    f"{names.get(m.sender_id, m.sender_id)}: "
                    f"{' '.join(m.content.split())[:80]}")

    # --- invented prior history ------------------------------------------
    # Scored with analyze.py's own regex so this means what the project says
    # it means. NOTE: ambient world events (atmosphere.py) are legitimate
    # shared facts — a resident discussing the electricity bill on the day
    # data/world_events.json fires it is CORRECT, not fabrication. What this
    # catches is invented *past*: "una settimana fa nel gruppo…" on day 2.
    prior_hits, prior_examples = 0, []
    for m in msgs:
        found = PRIOR_HISTORY_RE.findall(m.content)
        if found:
            prior_hits += len(found)
            if len(prior_examples) < 6:
                prior_examples.append(
                    f"day{m.day} {names.get(m.sender_id, m.sender_id)}: "
                    f"{' '.join(m.content.split())[:80]}")

    # --- echoing: same chat, same day, different residents ---------------
    groups: dict[tuple, list] = defaultdict(list)
    for m in msgs:
        groups[(m.day, m.chat_id)].append(m)
    sims, echo_examples = [], []
    for key, group in groups.items():
        for a, b in combinations(group, 2):
            if a.sender_id == b.sender_id:
                continue
            wa, wb = words(a.content), words(b.content)
            if len(wa) < 3 or len(wb) < 3:
                continue
            j = len(wa & wb) / len(wa | wb)
            sims.append(j)
            if j >= 0.34 and len(echo_examples) < 6:
                echo_examples.append(
                    f"day{key[0]} j={j:.2f}\n"
                    f"         {names.get(a.sender_id, a.sender_id)}: {' '.join(a.content.split())[:70]}\n"
                    f"         {names.get(b.sender_id, b.sender_id)}: {' '.join(b.content.split())[:70]}")

    # --- distinctiveness --------------------------------------------------
    vocab = {aid: set().union(*[words(t) for t in texts]) if texts else set()
             for aid, texts in by_agent.items()}
    distinct = {}
    for aid, v in vocab.items():
        others = set().union(*[w for k, w in vocab.items() if k != aid]) if len(vocab) > 1 else set()
        distinct[aid] = len(v - others) / max(1, len(v))

    # --- SOUL voice-rule violations ---------------------------------------
    violations = []
    for aid, texts in by_agent.items():
        if not texts:
            continue
        rate = sum(1 for t in texts if EMOJI_RE.search(t)) / len(texts)
        rule = VOICE_RULES.get(aid)
        if rule and rate > rule["max_emoji_rate"]:
            violations.append(
                f"{names.get(aid, aid)}: emoji in {rate:.0%} of messages "
                f"({rule['rule']})")

    # --- initiative: does anyone do anything the admin didn't prompt? -----
    # A building where every message is a reply to the admin in the main chat
    # is five help-desks, not five neighbours. DMs, motions and forwards are
    # the tools an agent uses to pursue its OWN agenda.
    chats_by_id = {c.id: c for c in state.chats}
    dm_msgs = [m for m in msgs if getattr(chats_by_id.get(m.chat_id), "kind", "") != "main"]
    main_msgs = [m for m in msgs if m.chat_id not in {c.id for c in state.chats
                                                     if c.kind != "main"}]
    # Peer-directed = main-chat message naming another resident (not admin).
    peer_names = {a.persona.display_name.split()[-1].lower() for a in state.agents}
    peer_directed = sum(
        1 for m in main_msgs
        if any(n in m.content.lower() for n in peer_names)
    )
    lengths = [len(m.content) for m in msgs]
    return {
        "label": label,
        "messages": len(msgs),
        "dm_messages": len(dm_msgs),
        "dm_threads": len({m.chat_id for m in dm_msgs}),
        "peer_directed": peer_directed,
        "motions": len(getattr(state, "motions", []) or []),
        "days": state.clock.day,
        "assistant_hits": assistant_hits,
        "assistant_per_100": 100 * assistant_hits / max(1, len(msgs)),
        "assistant_examples": assistant_examples,
        "prior_hits": prior_hits,
        "prior_examples": prior_examples,
        "echo_mean": statistics.mean(sims) if sims else 0.0,
        "echo_pairs_over_34": sum(1 for s in sims if s >= 0.34),
        "echo_pairs_total": len(sims),
        "echo_examples": echo_examples,
        "distinct": distinct,
        "distinct_mean": statistics.mean(distinct.values()) if distinct else 0.0,
        "violations": violations,
        "median_len": statistics.median(lengths) if lengths else 0,
        "p90_len": (sorted(lengths)[int(0.9 * len(lengths))] if lengths else 0),
        "by_agent_counts": {names.get(k, k): len(v) for k, v in by_agent.items()},
        "emoji_rate": {
            names.get(aid, aid): sum(1 for t in ts if EMOJI_RE.search(t)) / max(1, len(ts))
            for aid, ts in by_agent.items()
        },
    }


def report(s: dict) -> None:
    print(f"\n{'=' * 66}")
    print(f"  VOICE EVAL — {s['label']}  ({s['messages']} msgs over {s['days']} days)")
    print(f"{'=' * 66}")
    print(f"  assistant-speak   {s['assistant_hits']} hits "
          f"({s['assistant_per_100']:.1f} per 100 msgs)     [lower better]")
    print(f"  echo mean         {s['echo_mean']:.3f}  "
          f"({s['echo_pairs_over_34']}/{s['echo_pairs_total']} pairs >= 0.34)  [lower better]")
    print(f"  invented history  {s['prior_hits']} hits                          [lower better]")
    print(f"  distinctiveness   {s['distinct_mean']:.3f}                        [higher better]")
    print(f"  voice violations  {len(s['violations'])}                            [lower better]")
    print(f"  median length     {s['median_len']:.0f} chars   p90 {s['p90_len']:.0f}")
    print("\n  --- initiative (own agenda vs answering the admin) ---")
    print(f"  DM messages       {s['dm_messages']} across {s['dm_threads']} threads   [higher better]")
    print(f"  peer-directed     {s['peer_directed']} main-chat msgs naming a neighbour")
    print(f"  motions filed     {s['motions']}")
    print(f"\n  messages per resident: {s['by_agent_counts']}")
    print("  emoji rate: " + ", ".join(f"{k} {v:.0%}" for k, v in s['emoji_rate'].items()))
    if s["violations"]:
        print("\n  VOICE VIOLATIONS")
        for v in s["violations"]:
            print(f"    ✗ {v}")
    if s["assistant_examples"]:
        print("\n  ASSISTANT-SPEAK EXAMPLES")
        for e in s["assistant_examples"]:
            print(f"    · {e}")
    if s["prior_examples"]:
        print("\n  INVENTED-HISTORY EXAMPLES")
        for e in s["prior_examples"]:
            print(f"    · {e}")
    if s["echo_examples"]:
        print("\n  ECHO EXAMPLES")
        for e in s["echo_examples"]:
            print(f"    · {e}")
    print(f"{'=' * 66}\n")


async def main(days: int, opening: str, run_id: str, label: str, dump: str | None) -> int:
    state = building.build_run_state(building_id="001", opening_text=opening)
    # Pin the id: scheduler._seed_for(run_id, day, round) drives participation,
    # so a fixed id means before/after see the SAME schedule.
    state.run_id = run_id
    await storage.save_run(state)
    await memory.initialize_run_memory(state)

    for _ in range(days):
        await scheduler.advance_to_next_day(state)
        if state.ended:
            print(f"  run ended early: {state.ended_reason}")
            break

    s = score(state, label)
    report(s)

    if dump:
        from backend import timeline
        lines = []
        for m in timeline.in_order(state.messages):
            if not (m.content or "").strip():
                continue
            who = next((a.persona.display_name for a in state.agents
                        if a.persona.id == m.sender_id), m.sender_id)
            lines.append(f"[d{m.day} {m.fictional_timestamp_minutes:>6}] "
                         f"{m.chat_id:<12} {who:<18} {' '.join(m.content.split())}")
        Path(dump).write_text("\n".join(lines), encoding="utf-8")
        print(f"  transcript -> {dump}")

    print(f"  estimated spend this eval: ${state.metrics.estimated_cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--run-id", default="run_evalfixed",
                    help="pinned so before/after share a schedule")
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--dump", default=None, help="write the transcript here")
    ap.add_argument(
        "--opening",
        default=("Buongiorno a tutti. Da lunedì l'ascensore sarà fermo per "
                 "manutenzione straordinaria: il preventivo è di 4.800 euro, "
                 "ripartiti per millesimi. Fatemi sapere cosa ne pensate."),
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(
        main(args.days, args.opening, args.run_id, args.label, args.dump)))
