"""Transcript analyzer for condominium simulator runs.

Reads a run JSON from data/runs/, emits a markdown report covering:
- per-agent message stats and aggression markers
- tone-shift timeline (aggression density by day)
- preconception leaks (mentions of events absent from the chat corpus)
- containment leaks (forbidden meta-vocabulary)
- heuristic prompt-tuning suggestions

Usage:
    python -m backend.analyze data/runs/<run_id>.json
    python -m backend.analyze data/runs/<run_id>.json --out report.md

The output is designed to feed an iteration loop: run -> analyze -> adjust
prompts -> run again.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Lexicons (Italian)
# ---------------------------------------------------------------------------

# Words/phrases suggesting hostility, accusation, or escalation in Italian
# condominium chat. Matched case-insensitive, word-boundary where useful.
AGGRESSION_TERMS = [
    r"\bstangata\b", r"\bmazzate?\b", r"\btruffa\b", r"\bridicol[oai]\b",
    r"\bvergogn[aoe]\b", r"\bscandal[oi]\b", r"\bbasta\b", r"\bassurd[oai]\b",
    r"\binacettabile\b", r"\binaccettabile\b", r"\bincompetent[ei]\b",
    r"\bimbroglion[ei]\b", r"\bladr[oi]\b", r"\bdisonest[oi]\b",
    r"\bnascondere\b", r"\bnasconde\b", r"\bnascosto\b", r"\bsospett[oi]\b",
    r"\bdubbio\b", r"\bbugie\b", r"\bbugiard[oi]\b",
    r"\bmuto\b", r"\bsilenzio sospetto\b", r"\bsilenzio imbarazzante\b",
    r"\bdenunciar[eli]\b", r"\bavvocato\b", r"\btribunale\b",
    r"\bschifo\b", r"\bcafon[ei]\b", r"\bmaleduc\w+\b",
    r"\bnon firmo\b", r"\bnon voto\b", r"\bnon paghiamo\b", r"\bnon pagherò\b",
    r"\bbancomat\b", r"\bnascondino\b", r"\bspionaggio\b",
    r"\bfregatura\b", r"\bfregan\w+\b", r"\bfurto\b",
    r"\barroganz[ae]\b", r"\barrogant[ei]\b",
]
AGGRESSION_RE = re.compile("|".join(AGGRESSION_TERMS), re.IGNORECASE)

# Forbidden meta-vocabulary (subset of tools.py FORBIDDEN_TERMS)
CONTAINMENT_TERMS = [
    r"\bas an AI\b", r"\blanguage model\b", r"\bLLM\b",
    r"\bsimulation\b", r"\bsimulat(?:ed|ing|or)\b",
    r"\broleplay(?:ing)?\b", r"\bprompt\b", r"\bcharacter break\b",
    r"\bscenario\b", r"\bfictional\b",
    r"\bcome (?:una? )?intelligenza artificiale\b",
    r"\bmodello linguistico\b", r"\bsimulazione\b",
    r"\besperimento\b", r"\bfinzione\b",
]
CONTAINMENT_RE = re.compile("|".join(CONTAINMENT_TERMS), re.IGNORECASE)

# Phrases that typically invoke prior-history the agent may be inventing
PRIOR_HISTORY_TERMS = [
    r"\bultim[ao] volta\b", r"\bcome sempre\b", r"\bdi nuovo\b",
    r"\baltro rinvio\b", r"\balt[ro]a sorpresa\b", r"\baltri rinvii\b",
    r"\bgià l'altra volta\b", r"\bgià la volta scorsa\b",
    r"\bl'anno scorso\b", r"\bmesi fa\b", r"\bsettimane fa\b",
    r"\briunione (?:di )?(?:mercoled|gioved|venerd|luned|marted|sabato|domenica)",
    r"\briunione precedente\b", r"\briunione passata\b",
    r"\bdiscussion[ei] passat[ae]\b", r"\bquell[ao] volta\b",
    r"\blavori in facciata\b", r"\blavori facciata\b",
    r"\bbilancio precedente\b", r"\bpromesse (?:fatte|non mantenute)\b",
    r"\bci aveva detto\b", r"\baveva promesso\b",
]
PRIOR_HISTORY_RE = re.compile("|".join(PRIOR_HISTORY_TERMS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_run(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Per-message analysis
# ---------------------------------------------------------------------------

def _count_matches(pattern: re.Pattern, text: str) -> int:
    return len(pattern.findall(text or ""))


def tag_message(msg: dict) -> dict:
    content = msg.get("content", "") or ""
    return {
        "aggression": _count_matches(AGGRESSION_RE, content),
        "containment": _count_matches(CONTAINMENT_RE, content),
        "prior_history": _count_matches(PRIOR_HISTORY_RE, content),
        "chars": len(content),
        "questions": content.count("?"),
        "exclamations": content.count("!"),
    }


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def per_agent_stats(run: dict) -> dict[str, dict]:
    agents = {a["persona"]["id"]: a["persona"]["display_name"] for a in run["agents"]}
    by_agent: dict[str, dict] = {
        aid: {
            "display_name": name,
            "messages": 0,
            "chars": 0,
            "aggression_hits": 0,
            "containment_hits": 0,
            "prior_history_hits": 0,
            "questions": 0,
            "exclamations": 0,
            "dm_messages": 0,
            "main_messages": 0,
            "aggression_examples": [],
            "prior_history_examples": [],
            "containment_examples": [],
        }
        for aid, name in agents.items()
    }
    chats_by_id = {c["id"]: c for c in run.get("chats", [])}
    for m in run.get("messages", []):
        sid = m.get("sender_id")
        if sid not in by_agent:
            continue  # admin/external
        tags = tag_message(m)
        row = by_agent[sid]
        row["messages"] += 1
        row["chars"] += tags["chars"]
        row["aggression_hits"] += tags["aggression"]
        row["containment_hits"] += tags["containment"]
        row["prior_history_hits"] += tags["prior_history"]
        row["questions"] += tags["questions"]
        row["exclamations"] += tags["exclamations"]
        chat = chats_by_id.get(m.get("chat_id", ""), {})
        if chat.get("kind") == "main":
            row["main_messages"] += 1
        elif chat.get("kind") == "dm":
            row["dm_messages"] += 1
        if tags["aggression"] and len(row["aggression_examples"]) < 3:
            row["aggression_examples"].append(_excerpt(m))
        if tags["prior_history"] and len(row["prior_history_examples"]) < 3:
            row["prior_history_examples"].append(_excerpt(m))
        if tags["containment"] and len(row["containment_examples"]) < 3:
            row["containment_examples"].append(_excerpt(m))
    return by_agent


def _excerpt(msg: dict, limit: int = 180) -> str:
    c = (msg.get("content", "") or "").replace("\n", " ").strip()
    if len(c) > limit:
        c = c[: limit - 1] + "…"
    return f"day{msg.get('day', '?')} @min{msg.get('fictional_timestamp_minutes', '?')} — {c}"


def tone_timeline(run: dict) -> list[dict]:
    """Aggression-hit density per (day, time-of-day bucket)."""
    buckets: dict[tuple[int, str], dict] = defaultdict(lambda: {"msgs": 0, "agg": 0, "prior": 0})
    for m in run.get("messages", []):
        if m.get("sender_kind") != "resident":
            continue
        day = m.get("day", 0)
        mins = m.get("fictional_timestamp_minutes", 0)
        hour = (mins % (24 * 60)) // 60
        if hour < 12:
            bucket = "morning"
        elif hour < 18:
            bucket = "afternoon"
        else:
            bucket = "evening"
        tags = tag_message(m)
        row = buckets[(day, bucket)]
        row["msgs"] += 1
        row["agg"] += tags["aggression"]
        row["prior"] += tags["prior_history"]
    # Sort by (day, bucket order)
    order = {"morning": 0, "afternoon": 1, "evening": 2}
    rows = []
    for (day, bucket), v in sorted(buckets.items(), key=lambda kv: (kv[0][0], order[kv[0][1]])):
        rows.append({
            "day": day,
            "bucket": bucket,
            "messages": v["msgs"],
            "aggression_hits": v["agg"],
            "prior_history_hits": v["prior"],
        })
    return rows


def first_day_temperature(run: dict) -> dict:
    """How hostile was day 1 in the first 4 hours? If it's hot early, the game
    is starting pre-loaded rather than escalating organically."""
    early = [
        m for m in run.get("messages", [])
        if m.get("day") == 1
        and m.get("sender_kind") == "resident"
        and m.get("fictional_timestamp_minutes", 0) < 12 * 60  # before noon
    ]
    agg_hits = sum(tag_message(m)["aggression"] for m in early)
    prior_hits = sum(tag_message(m)["prior_history"] for m in early)
    return {
        "first_morning_messages": len(early),
        "first_morning_aggression_hits": agg_hits,
        "first_morning_prior_history_hits": prior_hits,
    }


def motions_summary(run: dict) -> list[dict]:
    rows = []
    for mo in run.get("motions", []):
        votes = mo.get("votes", {}) or {}
        rows.append({
            "id": mo.get("id"),
            "title": mo.get("title"),
            "proposer": mo.get("proposer_display_name"),
            "status": mo.get("status"),
            "day_proposed": mo.get("day_proposed"),
            "yes": sum(1 for v in votes.values() if v == "yes"),
            "no": sum(1 for v in votes.values() if v == "no"),
            "abstain": sum(1 for v in votes.values() if v == "abstain"),
        })
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_report(run: dict) -> str:
    out: list[str] = []
    rid = run.get("run_id", "?")
    scen = run.get("building_id") or run.get("scenario_id", "?")
    clock = run.get("clock", {})
    n_msgs = len(run.get("messages", []))
    n_agents = len(run.get("agents", []))

    out.append(f"# Run analysis — {rid}")
    out.append(f"Scenario: `{scen}` · days played: {clock.get('day', '?')} · messages: {n_msgs} · agents: {n_agents}")
    out.append("")

    # First-morning temperature — the key "are they starting pre-loaded?" metric
    fm = first_day_temperature(run)
    out.append("## Day 1 morning temperature")
    out.append(
        f"- Resident messages before noon on day 1: **{fm['first_morning_messages']}**"
    )
    out.append(
        f"- Aggression-keyword hits in that window: **{fm['first_morning_aggression_hits']}**"
    )
    out.append(
        f"- Prior-history/preconception hits in that window: **{fm['first_morning_prior_history_hits']}**"
    )
    if fm["first_morning_aggression_hits"] > 2 or fm["first_morning_prior_history_hits"] > 1:
        out.append("")
        out.append(
            "> ⚠ Agents are coming in hot or citing events that haven't happened. "
            "Expect preconceptions baked into briefs/system prompt. See suggestions below."
        )
    out.append("")

    # Per-agent table
    out.append("## Per-agent stats")
    out.append("")
    out.append("| Agent | Msgs | Main | DM | Chars | Q | ! | Aggression | Prior-history | Containment |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    stats = per_agent_stats(run)
    for aid, row in sorted(stats.items(), key=lambda kv: -kv[1]["messages"]):
        out.append(
            f"| {row['display_name']} | {row['messages']} | {row['main_messages']} | "
            f"{row['dm_messages']} | {row['chars']} | {row['questions']} | "
            f"{row['exclamations']} | {row['aggression_hits']} | "
            f"{row['prior_history_hits']} | {row['containment_hits']} |"
        )
    out.append("")

    # Aggression / prior-history examples
    for aid, row in sorted(stats.items(), key=lambda kv: -kv[1]["aggression_hits"]):
        if not (row["aggression_examples"] or row["prior_history_examples"] or row["containment_examples"]):
            continue
        out.append(f"### {row['display_name']} — flagged excerpts")
        if row["aggression_examples"]:
            out.append("**Aggression / escalation:**")
            for ex in row["aggression_examples"]:
                out.append(f"- {ex}")
        if row["prior_history_examples"]:
            out.append("**Cited prior history:**")
            for ex in row["prior_history_examples"]:
                out.append(f"- {ex}")
        if row["containment_examples"]:
            out.append("**Containment leak:**")
            for ex in row["containment_examples"]:
                out.append(f"- {ex}")
        out.append("")

    # Tone timeline
    out.append("## Tone timeline (aggression-hits per bucket)")
    out.append("")
    out.append("| Day | Bucket | Msgs | Aggression | Prior-history |")
    out.append("| ---: | --- | ---: | ---: | ---: |")
    for row in tone_timeline(run):
        out.append(
            f"| {row['day']} | {row['bucket']} | {row['messages']} | "
            f"{row['aggression_hits']} | {row['prior_history_hits']} |"
        )
    out.append("")

    # Motions
    mots = motions_summary(run)
    if mots:
        out.append("## Motions")
        out.append("")
        out.append("| Code | Title | Proposer | Status | Day | Yes | No | Abstain |")
        out.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: |")
        for m in mots:
            out.append(
                f"| {m['id']} | {m['title']} | {m['proposer']} | {m['status']} | "
                f"{m['day_proposed']} | {m['yes']} | {m['no']} | {m['abstain']} |"
            )
        out.append("")

    # Heuristic suggestions
    out.append("## Suggestions")
    suggestions = build_suggestions(run, stats, fm)
    if not suggestions:
        out.append("- No automatic flags triggered. Eyeball the timeline for pacing.")
    for s in suggestions:
        out.append(f"- {s}")
    out.append("")

    return "\n".join(out)


def build_suggestions(run: dict, stats: dict, fm: dict) -> list[str]:
    tips: list[str] = []
    if fm["first_morning_aggression_hits"] >= 3:
        tips.append(
            "Day-1 morning aggression is high. Cross-check each aggressive speaker "
            "against their SOUL in `data/buildings/{building_id}/souls/{id}.md` — if the "
            "character trait profile explains the heat (e.g. a hot-tempered narcissist), "
            "it's by design. If a measured character was aggressive, audit that SOUL "
            "for mood-coloring adjectives (brontolon*, vittimist*, pessimist*) or "
            "pre-loaded emotional states."
        )
    if fm["first_morning_prior_history_hits"] >= 2:
        tips.append(
            "Agents are inventing prior events (meetings, past disputes). Confirm the "
            "'Cosa sai del palazzo fino a oggi' section of the system prompt is present "
            "and explicit about not fabricating history."
        )
    heavy = [r for r in stats.values() if r["messages"] > 25]
    if heavy:
        names = ", ".join(r["display_name"] for r in heavy)
        tips.append(
            f"High message volume agents: {names}. Consider tightening per-day soft "
            "budget or raising the saturation-damp threshold."
        )
    any_containment = any(r["containment_hits"] for r in stats.values())
    if any_containment:
        tips.append(
            "Containment leak detected — an agent surfaced meta-vocabulary. Inspect "
            "the flagged excerpt and add the offending phrase to FORBIDDEN_TERMS in tools.py."
        )
    # Detect admin-ping spirals: many resident questions on day 1 with no admin reply
    day1_resident_qs = sum(
        (m.get("content", "") or "").count("?")
        for m in run.get("messages", [])
        if m.get("day") == 1 and m.get("sender_kind") == "resident"
    )
    day1_admin_msgs = sum(
        1 for m in run.get("messages", [])
        if m.get("day") == 1 and m.get("sender_kind") == "admin"
    )
    if day1_resident_qs >= 8 and day1_admin_msgs <= 1:
        tips.append(
            f"Day 1 had {day1_resident_qs} resident question-marks but only "
            f"{day1_admin_msgs} admin message(s). Agents are piling on while admin is "
            "silent. Verify the pending-admin-reply damper in scheduler.py is "
            "triggering (look at '_looks_like_admin_ping' hits)."
        )
    if not tips:
        tips.append(
            "Nothing concerning flagged. Good sign — but low hit counts can also mean "
            "the heuristics missed; skim the raw transcript to verify."
        )
    return tips


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze a condominium-simulator run.")
    parser.add_argument("path", help="Path to a data/runs/<run>.json file")
    parser.add_argument("--out", help="Write report here (default: stdout)")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    run = load_run(path)
    report = render_report(run)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
