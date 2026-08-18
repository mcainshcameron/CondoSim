"""Bookkeeping messages, and the one definition of "the agent produced output".

Two defects met here, and they had to be fixed together.

`_close_motion_if_ready` posts the vote tally as `sender_kind="admin"`.
Unguarded, that admin line landed in the *voting resident's* sent-tracker:
a forced agent who cast the decisive vote and wrote nothing was recorded as
having acknowledged the administrator and dropped from
`pending_admin_reactions` with no WARNING — the exact silent-ignore the
acknowledgment guarantee exists to prevent. Guarding the tracker alone would
have traded that for the opposite bug, because the *only* thing suppressing
the next-morning mass force-activation was the same false credit stamping
the tally `cascaded`. Hence `Message.bookkeeping`.

`_close_motion_if_ready` has never fired in any saved run, so nothing below
should be assumed to have worked before.
"""
from __future__ import annotations

import pytest

from backend import scheduler
from backend.models import Message, Motion

from .fake_llm import reply, tool_call
from .helpers import append_message as _append


def _open_motion(state, votes: dict[str, str]) -> Motion:
    """A motion one vote short of the auto-close threshold (3 of 5)."""
    motion = Motion(
        id="m_testcase",
        title="Preventivo caldaia",
        description="Approviamo il preventivo per la caldaia.",
        proposer_id="conti",
        proposer_display_name="Sig.ra Conti",
        day_proposed=state.clock.day,
        proposed_at_fictional_min=state.clock.minutes_since_start,
        votes=dict(votes),  # type: ignore[arg-type]
    )
    state.motions.append(motion)
    return motion


def _loop_for_day_one(state) -> scheduler.DayLoop:
    """A DayLoop positioned at the start of day 1, without running it."""
    loop = scheduler.setup_day(state)
    assert loop is not None
    return loop


def _tally_messages(state) -> list[Message]:
    return [m for m in state.messages if m.bookkeeping]


# ---------------------------------------------------------------------------
# 1. The decisive vote must not fake an acknowledgment
# ---------------------------------------------------------------------------

async def test_decisive_vote_alone_does_not_acknowledge_the_admin(run_state, fake_llm):
    """greco is owed a reply, votes the motion closed, writes zero words.

    The tally that auto-close posts is authored by "admin", so it is not
    greco's output. He stays owed.
    """
    loop = _loop_for_day_one(run_state)
    _open_motion(run_state, {"conti": "yes", "ferrari": "yes"})
    loop.pending_admin_reactions = {"greco"}

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if not caller.startswith("agent:greco:"):
            return reply(tool_call("done"))
        if caller.endswith(":step0"):
            return reply(tool_call("vote", motion_id="m_testcase", choice="yes"))
        return reply(tool_call("done"))

    fake_llm.responder = responder
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    motion = run_state.motions[0]
    assert motion.status == "passed", "the third yes closed it"
    assert _tally_messages(run_state), "auto-close posted the tally"
    assert not [m for m in run_state.messages if m.sender_id == "greco"], (
        "greco authored nothing"
    )
    assert "greco" in loop.pending_admin_reactions, (
        "an admin-authored tally is not the resident acknowledging the admin"
    )


def test_auto_close_tally_never_enters_the_voters_sent_tracker(run_state):
    """Pinned at the tool layer, below the scheduler.

    `landed_output_count` filters by author too, so this is belt and braces
    — but `sent_messages_this_activation` is also what the scheduler reads to
    advance the clock and to stamp `cascaded`, and an admin line stamped
    cascaded by a resident's round is precisely what used to hide the
    next-morning mass force-activation.
    """
    from backend.tools import ToolContext, dispatch_tool

    _open_motion(run_state, {"conti": "yes", "ferrari": "yes"})
    ctx = ToolContext(
        state=run_state,
        agent_id="greco",
        current_fictional_minutes=run_state.clock.minutes_since_start + 10,
        forced_for_admin=True,
    )
    dispatch_tool(ctx, "vote", {"motion_id": "m_testcase", "choice": "yes"})

    assert run_state.motions[0].status == "passed"
    assert _tally_messages(run_state), "the tally was posted"
    assert ctx.sent_messages_this_activation == [], (
        "an admin-authored tally is not the resident's output"
    )
    assert ctx.landed_output_count() == 0
    assert all(not m.cascaded for m in _tally_messages(run_state)), (
        "the tally still has to pass through the next day-start seed"
    )


async def test_voter_keeps_their_remaining_tool_steps_after_an_auto_close(run_state, fake_llm):
    """The false credit also tripped implicit-done, cutting the turn of the
    very resident best placed to comment on the outcome they just caused."""
    loop = _loop_for_day_one(run_state)
    _open_motion(run_state, {"conti": "yes", "ferrari": "yes"})

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if not caller.startswith("agent:greco:"):
            return reply(tool_call("done"))
        if caller.endswith(":step0"):
            return reply(tool_call("vote", motion_id="m_testcase", choice="yes"))
        if caller.endswith(":step1"):
            return reply(tool_call(
                "send_message",
                chat_id="Condominio Via Garibaldi",
                text="Bene, allora procediamo con quel preventivo.",
            ))
        return reply(tool_call("done"))

    fake_llm.responder = responder
    # greco must actually take a turn for this to mean anything.
    loop.pending_admin_reactions = {"greco"}
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    steps = [c["caller"] for c in fake_llm.calls_for("greco")]
    assert any(c.endswith(":step1") for c in steps), (
        f"the activation continued past the vote, got {steps}"
    )
    assert [m for m in run_state.messages if m.sender_id == "greco"], (
        "and greco got to say something about the outcome"
    )
    assert "greco" not in loop.pending_admin_reactions, "words discharge the obligation"


# ---------------------------------------------------------------------------
# 2. A tally is bookkeeping and must not seed the next morning
# ---------------------------------------------------------------------------

async def test_auto_close_tally_is_flagged_bookkeeping(run_state, fake_llm):
    loop = _loop_for_day_one(run_state)
    _open_motion(run_state, {"conti": "yes", "ferrari": "yes"})
    loop.pending_admin_reactions = {"greco"}

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if caller.startswith("agent:greco:") and caller.endswith(":step0"):
            return reply(tool_call("vote", motion_id="m_testcase", choice="yes"))
        return reply(tool_call("done"))

    fake_llm.responder = responder
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    tallies = _tally_messages(run_state)
    assert len(tallies) == 1
    tally = tallies[0]
    assert tally.sender_kind == "admin", "it still belongs in the admin column"
    assert "Esito mozione" in tally.content
    assert tally.cascaded is False, (
        "the round must not have mistaken it for greco's own message"
    )


async def test_manual_close_tally_is_flagged_bookkeeping(run_state):
    """The UI's 'Chiudi votazione' path goes through main.py, not tools.py."""
    from backend.main import _append_admin_message

    msg = _append_admin_message(
        run_state, "main", "🗳️ Chiusa votazione: \"X\" — Respinta",
        [a.persona.id for a in run_state.agents], bookkeeping=True,
    )
    assert msg.bookkeeping is True
    assert msg.cascaded is False, (
        "the day-start seed still has to consume it; bookkeeping is what "
        "stops it seeding, not a pre-stamped cascaded flag"
    )


@pytest.mark.parametrize("bookkeeping,expect_forced", [(True, False), (False, True)])
async def test_day_start_seed_ignores_bookkeeping(run_state, fake_llm, monkeypatch,
                                                  bookkeeping, expect_forced):
    """A scoreboard left over from yesterday must not force-activate the cast.

    With zero planned rounds the ONLY way an agent can activate is the bonus
    drain, which runs exactly on what the day-start seed collected — so an
    activation here is direct proof the message seeded.
    """
    from backend.main import _append_admin_message

    # Everything already in the transcript has been dealt with; the tally is
    # the only fresh non-resident message.
    for m in run_state.messages:
        m.cascaded = True
    _append_admin_message(
        run_state, "main", "🗳️ Chiusa votazione: \"Preventivo caldaia\" — Respinta",
        [a.persona.id for a in run_state.agents], bookkeeping=bookkeeping,
    )

    monkeypatch.setattr(scheduler, "ROUNDS_PER_DAY", 0)
    activated: list[str] = []
    real_activate = scheduler.activate_agent

    async def spy(state, agent_id, minutes, **kw):
        activated.append(agent_id)
        return await real_activate(state, agent_id, minutes, **kw)

    monkeypatch.setattr(scheduler, "activate_agent", spy)
    await scheduler.advance_to_next_day(run_state)

    if expect_forced:
        assert set(activated) == {a.persona.id for a in run_state.agents}
    else:
        assert activated == [], (
            f"a vote tally force-activated {sorted(set(activated))}"
        )


# ---------------------------------------------------------------------------
# 3. One predicate for "produced output" — a 👍 is not an answer to the admin
# ---------------------------------------------------------------------------

async def test_forced_agent_that_only_reacts_keeps_its_steps_and_stays_owed(
    run_state, fake_llm
):
    """The split definition cut the turn after one call with two of three
    tool steps unused, then re-forced the agent every remaining round."""
    loop = _loop_for_day_one(run_state)
    _append(run_state, "romano", "resident",
            "Il preventivo della caldaia mi sembra gonfiato, sinceramente.")
    loop.pending_admin_reactions = {"greco"}

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if not caller.startswith("agent:greco:"):
            return reply(tool_call("done"))
        if caller.endswith(":step0"):
            return reply(tool_call(
                "react_to_message",
                chat="Condominio Via Garibaldi",
                message_excerpt="Il preventivo della caldaia mi sembra gonfiato",
                emoji="👍",
            ))
        return reply(tool_call("done"))

    fake_llm.responder = responder
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    steps = [c["caller"] for c in fake_llm.calls_for("greco")]
    assert any(c.endswith(":step1") for c in steps), (
        f"the reaction must not trigger implicit-done under force, got {steps}"
    )
    reacted = [m for m in run_state.messages if m.reactions]
    assert reacted, "the reaction actually landed"
    assert "greco" in loop.pending_admin_reactions, (
        "a 👍 does not discharge an obligation to the administrator"
    )


async def test_forced_agent_can_react_then_write_in_the_same_activation(run_state, fake_llm):
    """The payoff of keeping the steps: the same wake-up produces words."""
    loop = _loop_for_day_one(run_state)
    _append(run_state, "romano", "resident",
            "Il preventivo della caldaia mi sembra gonfiato, sinceramente.")
    loop.pending_admin_reactions = {"greco"}

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if not caller.startswith("agent:greco:"):
            return reply(tool_call("done"))
        if caller.endswith(":step0"):
            return reply(tool_call(
                "react_to_message",
                chat="Condominio Via Garibaldi",
                message_excerpt="Il preventivo della caldaia mi sembra gonfiato",
                emoji="👍",
            ))
        return reply(tool_call(
            "send_message",
            chat_id="Condominio Via Garibaldi",
            text="Concordo con Romano, chiediamo un secondo preventivo.",
        ))

    fake_llm.responder = responder
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    assert [m for m in run_state.messages if m.sender_id == "greco"]
    assert "greco" not in loop.pending_admin_reactions


async def test_reaction_still_ends_an_ordinary_turn(run_state, fake_llm):
    """Outside a forced activation a reaction IS output — the loop must not
    spend a second full round trip just to hear `done`."""
    loop = _loop_for_day_one(run_state)
    _append(run_state, "romano", "resident",
            "Il preventivo della caldaia mi sembra gonfiato, sinceramente.")
    # Nobody is owed anything: clear the opening admin message.
    for m in run_state.messages:
        m.cascaded = True

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if caller.endswith(":step0"):
            return reply(tool_call(
                "react_to_message",
                chat="Condominio Via Garibaldi",
                message_excerpt="Il preventivo della caldaia mi sembra gonfiato",
                emoji="👍",
            ))
        return reply(tool_call("done"))

    fake_llm.responder = responder
    await loop._run_one_round(0, scheduler.ROUNDS_PER_DAY, is_first_round=True)

    steps = [c["caller"] for c in fake_llm.activation_calls()]
    assert steps, "somebody took a turn"
    assert all(c.endswith(":step0") for c in steps), (
        f"an unforced reaction should be implicit done, got {steps}"
    )
