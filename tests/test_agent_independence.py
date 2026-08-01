"""Guards for the three fixes that came out of the voice eval.

All offline, against the in-memory store. See scripts/eval_voices.py for the
measurement these were derived from.
"""
from __future__ import annotations

import pytest

from backend import agent as agent_mod
from backend import llm, scheduler, tools
from backend.tools import ToolContext

from .fake_llm import FakeLLM, reply, tool_call


@pytest.fixture
async def ctx(run_state):
    """A ToolContext for the first resident, positioned mid-morning on day 1."""
    a = run_state.agents[0]
    return ToolContext(
        state=run_state,
        agent_id=a.persona.id,
        current_fictional_minutes=9 * 60,
    )


# --------------------------------------------------------------------------
# Self-repetition
# --------------------------------------------------------------------------

async def test_identical_resend_is_refused(ctx):
    """A resident whose point got no reply used to just post it again — the
    eval caught a byte-identical message sent twice in one day."""
    text = "4800 sono troppi, servono altri preventivi prima di decidere."
    first = tools.tool_send_message(ctx, "main", text)
    assert "Inviato" in first

    second = tools.tool_send_message(ctx, "main", text)
    assert "già scritto" in second
    sent = [m for m in ctx.state.messages if m.sender_id == ctx.agent_id]
    assert len(sent) == 1, "the duplicate must not reach the chat"


async def test_resend_with_extra_emoji_is_still_a_repeat(ctx):
    """Re-posting the same sentence with three more 🙄 on the end is still
    re-posting it — normalization strips emoji and punctuation."""
    tools.tool_send_message(ctx, "main", "Mah, e chi lo controlla il preventivo?")
    again = tools.tool_send_message(ctx, "main", "Mah, e chi lo controlla il preventivo?! 🙄🙄🙄")
    assert "già scritto" in again


async def test_short_interjections_may_repeat(ctx):
    """"ok" / "mah" / "non mi convince" recur naturally in real chat; only
    substantive messages are deduped."""
    assert "Inviato" in tools.tool_send_message(ctx, "main", "mah")
    assert "Inviato" in tools.tool_send_message(ctx, "main", "mah")


async def test_self_repeat_does_not_end_the_activation(ctx):
    """Unlike a content-rule violation, a repeat must leave the agent free to
    rephrase or take it to a DM — ending the turn would recreate the silence
    this was meant to fix."""
    text = "Il preventivo va confrontato con almeno altre due offerte."
    tools.tool_send_message(ctx, "main", text)
    ctx.done = False
    tools.tool_send_message(ctx, "main", text)
    assert ctx.done is False


async def test_repeat_is_scoped_to_the_chat_and_day(ctx):
    """The same sentence to a different person is a different act."""
    text = "Secondo me qui c'è qualcosa che non torna con questi conti."
    tools.tool_send_message(ctx, "main", text)
    other = next(a for a in ctx.state.agents if a.persona.id != ctx.agent_id)
    dm = tools.tool_send_dm(ctx, other.persona.display_name, text)
    assert "Inviato" in dm


# --------------------------------------------------------------------------
# The options menu drives which tools get used at all
# --------------------------------------------------------------------------

async def test_notification_prompt_offers_the_private_channel(run_state):
    """A 5-day eval produced ZERO private messages and ZERO motions while this
    block offered only group message / reaction / put the phone down. The
    model picks from what is listed, so the private channel has to be on the
    menu."""
    a = run_state.agents[0]
    ctx = ToolContext(state=run_state, agent_id=a.persona.id, current_fictional_minutes=9 * 60)
    prompt = agent_mod.build_notification_prompt(ctx, a, "inbox", "")
    assert "messaggio privato a un vicino" in prompt
    assert "mozione" in prompt


async def test_notification_prompt_pushes_own_agenda(run_state):
    """Independence: residents must not need the admin to hand them a topic."""
    a = run_state.agents[0]
    ctx = ToolContext(state=run_state, agent_id=a.persona.id, current_fictional_minutes=9 * 60)
    prompt = agent_mod.build_notification_prompt(ctx, a, "inbox", "")
    assert "Non aspettare che sia l'amministratore a darti il tema" in prompt


# --------------------------------------------------------------------------
# A turn spent narrating is not a turn spent choosing silence
# --------------------------------------------------------------------------

async def test_narrating_instead_of_calling_a_tool_gets_one_retry(run_state):
    """The model that answers in prose ("Osservo la notifica…", sometimes in
    English) used to lose its whole activation silently. In a 5-day eval that
    left two residents on 1 message each while a third sent 10."""
    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="Cosa è successo:\nNiente.\n\nCose da ricordare:\n- Nulla.")
        if caller.endswith(":step0"):
            return reply(content="Osservo la notifica e valuto la situazione.")
        return reply(tool_call(
            "send_message",
            chat_id="Condominio Via Garibaldi",
            text="ma quanto dura sta manutenzione?",
        ))

    fake = FakeLLM(responder=responder)
    previous = llm.set_transport(fake)
    try:
        await scheduler.advance_to_next_day(run_state)
    finally:
        llm.set_transport(previous)

    resident_msgs = [m for m in run_state.messages if m.sender_kind == "resident"]
    assert resident_msgs, "the nudged retry should have produced real messages"

    nudges = [
        m for c in fake.activation_calls() for m in c["messages"]
        if "non hai toccato il telefono" in str(m.get("content") or "")
    ]
    assert nudges, "the corrective nudge should appear in the retry prompt"


async def test_nudge_fires_at_most_once_per_activation(run_state):
    """A model that will not call a tool must not be retried forever — one
    nudge, then the phone goes down."""
    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="Cosa è successo:\nNiente.\n\nCose da ricordare:\n- Nulla.")
        return reply(content="Continuo a pensare senza toccare il telefono.")

    fake = FakeLLM(responder=responder)
    previous = llm.set_transport(fake)
    try:
        await scheduler.advance_to_next_day(run_state)
    finally:
        llm.set_transport(previous)

    # An agent may legitimately activate several times a day (ROUNDS_PER_DAY
    # plus up to two bonus drain rounds), so the daily call count is not the
    # invariant. What matters is that a single activation never gets past the
    # one nudge: steps must only ever be 0 (first try) and 1 (the retry).
    steps = {str(c["caller"]).rsplit(":step", 1)[1] for c in fake.activation_calls()}
    assert steps <= {"0", "1"}, f"activation went past one nudge: {sorted(steps)}"


async def test_system_prompt_forbids_inventing_building_facts(run_state):
    """The eval watched the cast invent a "new lift" and an electricity bill
    with precise figures, then reason from them as established fact."""
    a = run_state.agents[0]
    prompt = await agent_mod.build_system_prompt(run_state, a)
    assert "i fatti nuovi sul palazzo non li inventi tu" in prompt
