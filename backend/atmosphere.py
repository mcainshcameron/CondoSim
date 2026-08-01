"""Ambient texture: what the building is like today, and what mood each
resident woke up in.

Both are derived, not generated — zero extra LLM calls, so this is free.

**World events.** v1's condo contained nothing but its own gossip, so every
run depended on the admin manufacturing drama. Real buildings supply their
own: the lift breaks, the water goes off, someone's parcel vanishes. The
day's event is *not* posted as a system message — that would read as an
announcement from nowhere. It's handed to every resident as something they
personally noticed, exactly like a real building. Whoever cares brings it up
in the chat; the others recognise it because it's true for them too.

**Mood.** SOUL is immutable identity, so it can't carry "grumpy today".
Mood is a thin weather layer over it, read off what actually happened to
that resident yesterday: how much they said, whether anyone answered,
whether the emoji they got back were warm or dismissive.
"""
from __future__ import annotations

import json
import random
from functools import lru_cache

from .config import DATA_DIR, WORLD_EVENT_PROBABILITY
from .logging_utils import log
from .models import Agent, RunState

POSITIVE_EMOJI = frozenset({"👍", "❤️", "😂", "🔥", "💯", "🙏"})
NEGATIVE_EMOJI = frozenset({"🙄", "😡"})


# ---------------------------------------------------------------------------
# World events
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_world_events() -> list[dict]:
    path = DATA_DIR / "world_events.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def pick_world_event(state: RunState) -> str | None:
    """Choose today's ambient event, or None for an uneventful day.

    Deterministic per (run, day) so a replayed day is identical, and events
    already used in this run are not repeated.
    """
    events = _load_world_events()
    if not events:
        state.world_event_today = None
        return None

    rng = random.Random(f"{state.run_id}:world:{state.clock.day}")
    if rng.random() >= WORLD_EVENT_PROBABILITY:
        state.world_event_today = None
        return None

    seen = set(state.world_events_seen)
    pool = [e for e in events if e.get("id") not in seen] or events
    chosen = rng.choice(pool)
    text = str(chosen.get("text", "")).strip()
    if not text:
        state.world_event_today = None
        return None

    state.world_event_today = text
    if chosen.get("id") and chosen["id"] not in state.world_events_seen:
        state.world_events_seen.append(chosen["id"])
    log("atmosphere", f"day {state.clock.day} world event: {text[:70]}")
    return text


# ---------------------------------------------------------------------------
# Mood
# ---------------------------------------------------------------------------

def mood_cue(state: RunState, agent: Agent) -> str:
    """One short second-person line about how this resident feels today.

    Returns "" on day 1 and whenever yesterday was too quiet to read
    anything into — an invented mood is worse than no mood.
    """
    day = state.clock.day
    if day <= 1:
        return ""
    aid = agent.persona.id
    yesterday = day - 1

    sent = 0
    positive = 0
    negative = 0
    for m in state.messages:
        if m.day != yesterday:
            continue
        if m.sender_id != aid:
            continue
        sent += 1
        for emoji, ids in (m.reactions or {}).items():
            if not ids:
                continue
            if emoji in POSITIVE_EMOJI:
                positive += len(ids)
            elif emoji in NEGATIVE_EMOJI:
                negative += len(ids)

    # Did anyone speak to them, or about them, yesterday?
    surname = agent.persona.display_name.split()[-1].lower()
    addressed = 0
    for m in state.messages:
        if m.day != yesterday or m.sender_id == aid:
            continue
        if aid not in (m.audience or []):
            continue
        if len(surname) >= 3 and surname in (m.content or "").lower():
            addressed += 1

    if sent == 0 and addressed == 0:
        return ""

    if negative > positive and negative >= 2:
        return (
            "Ieri più di uno ti ha risposto storto. Ti è rimasta la mosca al naso."
        )
    if positive >= 2 and positive > negative:
        return "Ieri ti hanno dato ragione. Ti senti sostenuto/a, e si sente."
    if addressed >= 2:
        return "Ieri ti hanno tirato in ballo più volte. Sei sul chi va là."
    if sent >= 5:
        return "Ieri hai scritto parecchio. Oggi sei un po' stanco/a di questa storia."
    if sent == 0 and addressed >= 1:
        return "Ieri sei rimasto/a in disparte a leggere. Qualcosa ti è rimasto dentro."
    return ""
