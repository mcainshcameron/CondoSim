"""Forcing, the bonus drain, and the causality clamp.

Three defects live here and they all come from the same place: the bonus
drain was written as a rough afterthought to the planned rounds and never
got their clamps, their bound, or a single test. `grep -rn 'bonus' tests/`
returned one comment before this file existed.

1. The drain applied `min(..., day_end - 1)` *after* the floor, so once the
   transcript head passed `day_end - 6` the target moved backwards and the
   agent activated blind — every context reader filters
   `fictional_timestamp_minutes <= now`.
2. It never ported the planned path's owed-message clamp, so a forced agent
   could be stamped before the very message they were woken up to answer.
3. `cascaded` was stamped at SCHEDULE time, so an obligation the drain
   failed to discharge was recorded as discharged anyway — permanently.

All of it runs against the real DayLoop and a scripted LLM.
"""
from __future__ import annotations

from uuid import uuid4

from backend import scheduler, timeline
from backend.models import Message

from .fake_llm import reply, tool_call
from .helpers import append_message

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _inject(
    state,
    sender_id: str,
    sender_kind: str,
    text: str,
    minute: int,
    *,
    chat_id: str = "main",
    cascaded: bool = True,
    bookkeeping: bool = False,
) -> Message:
    """`helpers.append_message` with the fictional minute made positional.

    Everything in this module is about *where in the day* something landed,
    so the minute is never defaulted here.
    """
    return append_message(
        state,
        sender_id,
        sender_kind,
        text,
        chat_id=chat_id,
        minute=minute,
        cascaded=cascaded,
        bookkeeping=bookkeeping,
    )


def _mute(agent_id: str):
    """A responder where one resident always narrates instead of calling a
    tool. That is the real failure mode the nudge in agent.py exists for; a
    model that keeps doing it after the nudge produces nothing at all, so it
    can never acknowledge the administrator."""

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="Cosa è successo:\nniente.")
        if caller.startswith(f"agent:{agent_id}:"):
            return reply(content="Leggo la notifica e ci penso su.")
        if caller.endswith(":step0"):
            return reply(tool_call(
                "send_message",
                chat_id="Condominio Via Garibaldi",
                text=f"Prendo nota, {uuid4().hex[:6]}.",
            ))
        return reply(tool_call("done"))

    return responder


def _spy_activations(monkeypatch) -> list[dict]:
    """Record (agent_id, minute, transcript head at the moment of the call)
    for every activation, then call through to the real thing."""
    seen: list[dict] = []
    real = scheduler.activate_agent

    async def spy(state, agent_id, minutes, **kw):
        seen.append({
            "agent_id": agent_id,
            "minute": minutes,
            "head": timeline.latest_minute(state),
            "day": state.clock.day,
            "forced": bool(kw.get("forced_for_admin")),
        })
        return await real(state, agent_id, minutes, **kw)

    monkeypatch.setattr(scheduler, "activate_agent", spy)
    return seen


# ---------------------------------------------------------------------------
# Fix now #7 — the bonus drain must not run behind the head of the transcript
# ---------------------------------------------------------------------------


def test_bonus_target_never_moves_behind_the_transcript_head():
    """The arithmetic, pinned on its own.

    The old expression was `min(max(latest+5, day_end-60), day_end-1)`. Each
    case below is one the old form got wrong or right; the property that must
    hold for all of them is `target >= latest`.
    """
    day_end = scheduler.day_end_minutes(1)  # 1380 with DAY_END_HOUR=23

    # The reproduced failure: head already past the cap.
    assert scheduler._bonus_target_minute(1383, day_end) == 1383
    assert scheduler._bonus_target_minute(1382, day_end) == 1382
    # Exactly on the boundary — day_end-6 is the last head the cap can serve.
    assert scheduler._bonus_target_minute(day_end - 6, day_end) == day_end - 1
    assert scheduler._bonus_target_minute(day_end - 5, day_end) == day_end - 1
    assert scheduler._bonus_target_minute(day_end - 4, day_end) == day_end - 1
    # Nothing said all day: still lands in the last hour, not at the head.
    assert scheduler._bonus_target_minute(500, day_end) == day_end - 60

    for latest in range(day_end - 90, day_end + 40):
        target = scheduler._bonus_target_minute(latest, day_end)
        assert target >= latest, f"target {target} is behind head {latest}"


async def test_no_activation_ever_runs_behind_the_head_including_the_drain(
    run_state, fake_llm, monkeypatch
):
    """The end-to-end invariant, across a full day that reaches the drain.

    Setup mirrors the reproduction: the administrator posts at 22:58, so the
    planned round's owed-message clamp stamps the forced agents at
    `latest_owed + 2..25` — past `day_end` — and the drain then has to cope
    with a transcript head beyond its own cap. One resident narrates instead
    of calling a tool, which is what keeps an obligation alive long enough
    for the drain to run at all.
    """
    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 1)
    day_end = scheduler.day_end_minutes(1)
    _inject(
        run_state, "admin", "admin",
        "Scusate l'ora: domani passa il tecnico per la caldaia.",
        day_end - 2, cascaded=False,
    )
    fake_llm.responder = _mute("marchetti")
    seen = _spy_activations(monkeypatch)

    await scheduler.advance_to_next_day(run_state)

    assert seen, "somebody activated"
    assert any(s["agent_id"] == "marchetti" for s in seen[5:]), (
        "the mute resident was retried in the bonus drain"
    )
    behind = [s for s in seen if s["minute"] < s["head"]]
    assert not behind, (
        "an agent activated behind the head of the transcript and therefore "
        f"could not see it: {behind}"
    )
    # And the transcript the drain produced is still a total order.
    ordered = timeline.in_order(run_state.messages)
    assert [m.fictional_timestamp_minutes for m in ordered] == sorted(
        m.fictional_timestamp_minutes for m in ordered
    )


async def test_bonus_drain_activates_after_the_message_it_owes_a_reply_to(
    run_state, fake_llm, monkeypatch
):
    """The owed-message clamp, ported from the planned rounds.

    The admin's message IS the head of the transcript here, stamped past
    `day_end` — which is exactly the shape the planned path produces when its
    own clamp pushes a forced agent to `latest_owed + 2..25`, and the shape
    that used to send the drain backwards. With zero planned rounds the drain
    is the only thing that can activate anyone, so every minute recorded here
    is one the drain chose.
    """
    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 0)
    for m in run_state.messages:
        m.cascaded = True
    owed_msg = _inject(
        run_state, "admin", "admin",
        "Chi ha le chiavi del contatore? Rispondete per favore.",
        scheduler.day_end_minutes(1) + 3, cascaded=False,
    )
    fake_llm.responder = _mute("marchetti")
    seen = _spy_activations(monkeypatch)

    await scheduler.advance_to_next_day(run_state)

    assert seen, "the drain ran"
    assert all(s["forced"] for s in seen)
    early = [s for s in seen if s["minute"] <= owed_msg.fictional_timestamp_minutes]
    assert not early, (
        "a forced agent was stamped at or before the message it was woken up "
        f"to answer, instead of picking up the phone shortly after it: {early}"
    )


# ---------------------------------------------------------------------------
# Worth doing E — an obligation is discharged on discharge, not on schedule
# ---------------------------------------------------------------------------


async def test_an_undischarged_obligation_is_handed_back_to_the_next_day(
    run_state, fake_llm, monkeypatch
):
    """Reproduces the silent drop: `schedule_reactions` stamps `cascaded`
    before anyone activates, so a resident who never answers is recorded as
    having been serviced and is never asked again for the rest of the run."""
    opening = run_state.messages[0]
    assert opening.sender_kind == "admin" and opening.cascaded is False

    fake_llm.responder = _mute("marchetti")
    await scheduler.advance_to_next_day(run_state)

    assert not [m for m in run_state.messages if m.sender_id == "marchetti"], (
        "the scripted resident produced nothing, as intended"
    )
    assert opening.cascaded is False, (
        "the admin message nobody answered must go back into the pool; "
        "stamped cascaded at schedule time it can never be retried"
    )

    # Day 2 with zero planned rounds: the only path to an activation is the
    # bonus drain, which runs on exactly what the day-start seed collected.
    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 0)
    seen = _spy_activations(monkeypatch)
    fake_llm.responder = None  # everybody cooperates now
    await scheduler.advance_to_next_day(run_state)

    assert run_state.clock.day == 2
    assert "marchetti" in {s["agent_id"] for s in seen}, (
        "the revived obligation seeded the next morning"
    )
    assert [m for m in run_state.messages if m.sender_id == "marchetti"], (
        "and the resident finally said something"
    )


async def test_a_discharged_obligation_is_not_handed_back(run_state, fake_llm, monkeypatch):
    """The other half: when everyone does answer, nothing is revived and the
    next morning is not force-activated."""
    await scheduler.advance_to_next_day(run_state)

    opening = run_state.messages[0]
    assert opening.cascaded is True, "answered, so it stays consumed"

    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 0)
    seen = _spy_activations(monkeypatch)
    await scheduler.advance_to_next_day(run_state)
    assert seen == [], f"nothing was owed, yet {sorted({s['agent_id'] for s in seen})} were forced"


def test_revive_is_bounded_by_day_age(run_state):
    """A model that never calls a tool must not force-activate its
    neighbours at prob=1.0 every morning until the spend cap trips."""
    loop = scheduler.setup_day(run_state)
    assert loop is not None
    opening = run_state.messages[0]
    opening.cascaded = True
    loop.pending_admin_reactions = {"marchetti"}

    fresh = opening.day + scheduler._MAX_RETRY_DAY_AGE
    assert loop._revive_undischarged_obligations(fresh) == [opening.id]
    assert opening.cascaded is False

    opening.cascaded = True
    stale = opening.day + scheduler._MAX_RETRY_DAY_AGE + 1
    assert loop._revive_undischarged_obligations(stale) == []
    assert opening.cascaded is True, "past the bound the obligation is dropped for good"


def test_a_vote_tally_is_never_handed_back(run_state):
    """Bookkeeping must survive the revive path too.

    Reviving a scoreboard would rebuild exactly the next-morning mass
    force-activation that `Message.bookkeeping` was added to stop.
    """
    loop = scheduler.setup_day(run_state)
    assert loop is not None
    opening = run_state.messages[0]
    for m in run_state.messages:
        m.cascaded = True
    tally = _inject(
        run_state, "admin", "admin",
        '🗳️ Chiusa votazione: "Preventivo caldaia" — Respinta (2 sì, 3 no)',
        run_state.clock.minutes_since_start + 5, bookkeeping=True,
    )
    loop.pending_admin_reactions = {a.persona.id for a in run_state.agents}

    revived = loop._revive_undischarged_obligations(run_state.clock.day)
    assert tally.id not in revived, "nobody owes a scoreboard a reply"
    assert tally.cascaded is True
    # Contrast, in the same state and with the same audience: the
    # administrator's actual question does go back into the pool.
    assert opening.id in revived


async def test_a_failed_bonus_activation_keeps_the_agent_owed(run_state, fake_llm, monkeypatch):
    """The drain used to `discard(aid)` on any exception — dropping the
    obligation on a transient error with no WARNING anywhere, which is the
    silent-ignore the acknowledgment guarantee exists to prevent."""
    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 0)
    for m in run_state.messages:
        m.cascaded = True
    admin_msg = _inject(
        run_state, "admin", "admin", "Serve una risposta entro stasera.",
        scheduler.day_start_minutes(1) + 10, cascaded=False,
    )

    attempts: list[str] = []

    async def boom(state, agent_id, minutes, **kw):
        attempts.append(agent_id)
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(scheduler, "activate_agent", boom)
    await scheduler.advance_to_next_day(run_state)

    residents = {a.persona.id for a in run_state.agents}
    assert set(attempts) == residents, "every owed agent was tried"
    assert len(attempts) == len(residents) * scheduler._MAX_BONUS_ROUNDS, (
        "and retried across both bonus rounds rather than dropped on the "
        f"first failure, got {attempts}"
    )
    assert admin_msg.cascaded is False, "still unanswered, so tomorrow gets a turn"


# ---------------------------------------------------------------------------
# Hygiene — the mention boost fired on `continua` / `conteggio`
# ---------------------------------------------------------------------------


def test_mention_boost_ignores_lowercase_lookalikes(run_state):
    """65 of 245 measured mention hits were words like `conti`, `continua`
    and `conteggio` — 3.9% of Conti's turn-slots spent at prob=0.95 answering
    nobody. `Greco` and `Romano` are ordinary Italian words too."""
    conti = next(a for a in run_state.agents if a.persona.id == "conti")
    greco = next(a for a in run_state.agents if a.persona.id == "greco")
    romano = next(a for a in run_state.agents if a.persona.id == "romano")

    m = _inject(
        run_state, "ferrari", "resident",
        "i conti non tornano, continua a mancare il conteggio delle spese",
        run_state.clock.minutes_since_start + 5,
    )
    now = m.fictional_timestamp_minutes
    assert not scheduler._was_mentioned_recently(run_state, conti, 0, now)

    m2 = _inject(
        run_state, "ferrari", "resident",
        "un caffè greco e un piatto romano, niente di più",
        now + 1,
    )
    now = m2.fictional_timestamp_minutes
    assert not scheduler._was_mentioned_recently(run_state, greco, 0, now)
    assert not scheduler._was_mentioned_recently(run_state, romano, 0, now)


def test_mention_boost_still_catches_a_real_call_by_name(run_state):
    conti = next(a for a in run_state.agents if a.persona.id == "conti")
    greco = next(a for a in run_state.agents if a.persona.id == "greco")

    m = _inject(
        run_state, "ferrari", "resident",
        "@Greco, la Sig.ra Conti ha ragione sul preventivo.",
        run_state.clock.minutes_since_start + 5,
    )
    now = m.fictional_timestamp_minutes
    assert scheduler._was_mentioned_recently(run_state, conti, 0, now)
    assert scheduler._was_mentioned_recently(run_state, greco, 0, now)


def test_mention_pattern_is_compiled_once_per_agent():
    """The caller scans every message in the run, on every turn, for every
    agent — rebuilding the regex per message was the other half of the cost."""
    first = scheduler._mention_pattern("Sig.ra Conti")
    assert first is scheduler._mention_pattern("Sig.ra Conti")
    assert first.pattern == r"\bConti\b"
    # Degenerate names produce no matcher rather than a pattern that matches
    # half the alphabet.
    assert scheduler._mention_pattern("Li") is None
    assert scheduler._mention_pattern("") is None
