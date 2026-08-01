"""World events, mood cues and MEMORY windowing."""
from __future__ import annotations

from backend import atmosphere, memory, scheduler
from backend.models import Message


def _msg(sender, day, minute, content="ciao", reactions=None, audience=()):
    return Message(
        id=f"m{sender}{day}{minute}",
        chat_id="main",
        sender_id=sender,
        sender_kind="resident",
        sender_display_name=sender.title(),
        content=content,
        fictional_timestamp_minutes=minute,
        seq=minute,
        wall_clock_iso="2026-01-01T00:00:00Z",
        day=day,
        audience=list(audience),
        reactions=reactions or {},
    )


# --- world events --------------------------------------------------------

def test_world_event_is_deterministic_per_day(run_state):
    run_state.clock.day = 3
    first = atmosphere.pick_world_event(run_state)
    run_state.world_events_seen = []
    second = atmosphere.pick_world_event(run_state)
    assert first == second, "same run + same day must replay identically"


def test_world_events_do_not_repeat_within_a_run(run_state):
    seen = []
    for day in range(1, 15):
        run_state.clock.day = day
        event = atmosphere.pick_world_event(run_state)
        if event:
            seen.append(event)
    assert len(seen) == len(set(seen)), "no event fires twice in one run"


def test_world_event_is_not_posted_as_a_message(run_state, fake_llm):
    """It reaches residents through the prompt, not as a message from
    nowhere — a system announcement would break the chat fiction."""
    before = len(run_state.messages)
    run_state.clock.day = 2
    atmosphere.pick_world_event(run_state)
    assert len(run_state.messages) == before


async def test_world_event_reaches_the_agent_prompt(run_state, fake_llm, monkeypatch):
    # Force an event; the day loop picks it at day start.
    monkeypatch.setattr(atmosphere, "WORLD_EVENT_PROBABILITY", 1.0)
    await scheduler.advance_to_next_day(run_state)

    event = run_state.world_event_today
    assert event, "an event was selected for the day"
    prompt = "\n".join(
        str(m.get("content") or "")
        for c in fake_llm.activation_calls()
        for m in c["messages"]
    )
    assert event in prompt, "every resident sees the same building fact"


async def test_all_residents_see_the_same_world_event(run_state, fake_llm, monkeypatch):
    """Shared fact, not a per-agent prompt — that's what lets neighbours
    corroborate each other instead of each inventing their own weather."""
    monkeypatch.setattr(atmosphere, "WORLD_EVENT_PROBABILITY", 1.0)
    await scheduler.advance_to_next_day(run_state)

    event = run_state.world_event_today
    prompts_by_agent = {}
    for call in fake_llm.activation_calls():
        aid = call["caller"].split(":")[1]
        prompts_by_agent.setdefault(aid, []).append(
            "\n".join(str(m.get("content") or "") for m in call["messages"])
        )
    assert len(prompts_by_agent) >= 2, "more than one resident woke up"
    for aid, prompts in prompts_by_agent.items():
        assert any(event in p for p in prompts), f"{aid} did not see the event"


# --- mood ----------------------------------------------------------------

def test_no_mood_on_day_one(run_state):
    run_state.clock.day = 1
    agent = run_state.agents[0]
    assert atmosphere.mood_cue(run_state, agent) == ""


def test_no_mood_invented_from_a_silent_day(run_state):
    run_state.clock.day = 3
    agent = run_state.agents[0]
    assert atmosphere.mood_cue(run_state, agent) == "", (
        "a resident with no activity yesterday gets no fabricated mood"
    )


def test_negative_reactions_sour_the_mood(run_state):
    run_state.clock.day = 3
    agent = run_state.agents[0]
    aid = agent.persona.id
    run_state.messages.append(
        _msg(aid, 2, 600, reactions={"🙄": ["greco", "romano"]})
    )
    cue = atmosphere.mood_cue(run_state, agent)
    assert cue and "storto" in cue


def test_positive_reactions_lift_the_mood(run_state):
    run_state.clock.day = 3
    agent = run_state.agents[0]
    aid = agent.persona.id
    run_state.messages.append(
        _msg(aid, 2, 600, reactions={"👍": ["greco", "romano", "ferrari"]})
    )
    cue = atmosphere.mood_cue(run_state, agent)
    assert cue and "sostenuto" in cue


def test_heavy_talker_gets_tired(run_state):
    run_state.clock.day = 3
    agent = run_state.agents[0]
    aid = agent.persona.id
    for i in range(6):
        run_state.messages.append(_msg(aid, 2, 600 + i))
    cue = atmosphere.mood_cue(run_state, agent)
    assert cue and "stanco" in cue


# --- memory windowing ----------------------------------------------------

def test_window_keeps_seed_and_recent_days():
    seed = "Mi chiamo Maria. Abito qui da quarant'anni."
    body = "".join(f"\n\n--- Giorno {d}, lunedì ---\nGiorno {d} successo.\n" for d in range(1, 11))
    windowed = memory.window_memory(seed + body, keep_days=3)

    assert "Mi chiamo Maria" in windowed, "biographical seed is always kept"
    assert "Giorno 10 successo" in windowed
    assert "Giorno 8 successo" in windowed
    assert "Giorno 1 successo" not in windowed, "old days dropped from the prompt"
    assert "7 giorni precedenti" in windowed, "the gap is acknowledged, not hidden"


def test_window_is_a_noop_for_short_runs():
    seed = "Mi chiamo Maria."
    body = "\n\n--- Giorno 1, lunedì ---\nPrimo giorno.\n"
    content = seed + body
    assert memory.window_memory(content, keep_days=6) == content


def test_window_disabled_returns_input():
    content = "qualsiasi cosa"
    assert memory.window_memory(content, keep_days=0) == content
