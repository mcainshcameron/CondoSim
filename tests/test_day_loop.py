"""End-to-end day loop, offline.

Runs the real scheduler, activation loop, tools and consolidation against a
scripted LLM. This is the regression check that `scripts/run_smoketest.py`
could never be — it takes milliseconds and costs nothing.
"""
from __future__ import annotations


from backend import scheduler, timeline
from backend.models import Message

from .fake_llm import reply, tool_call


async def test_a_day_runs_and_produces_messages(run_state, fake_llm):
    before = len(run_state.messages)
    await scheduler.advance_to_next_day(run_state)

    assert len(run_state.messages) > before, "residents said something"
    assert run_state.clock.day == 1
    assert fake_llm.call_count > 0


async def test_transcript_stays_in_total_order(run_state, fake_llm):
    """The invariant the UI depends on: sorting the transcript can never
    reorder what the player already read."""
    for _ in range(3):
        await scheduler.advance_to_next_day(run_state)

    ordered = timeline.in_order(run_state.messages)
    minutes = [m.fictional_timestamp_minutes for m in ordered]
    seqs = [m.seq for m in ordered]

    assert minutes == sorted(minutes), "fictional time never goes backwards"
    assert len(set(seqs)) == len(seqs), "seq is unique across the run"
    # Generation order and display order agree.
    assert seqs == sorted(seqs)


async def test_no_duplicate_message_ids(run_state, fake_llm):
    for _ in range(2):
        await scheduler.advance_to_next_day(run_state)
    ids = [m.id for m in run_state.messages]
    assert len(set(ids)) == len(ids)


async def test_activation_costs_one_llm_call(run_state, fake_llm):
    """The latency fix. v1 spent a second full round trip purely so the model
    could say `done`; the loop now exits as soon as output has landed."""
    await scheduler.advance_to_next_day(run_state)

    activations = fake_llm.activation_calls()
    steps = [c["caller"].rsplit(":step", 1)[1] for c in activations]
    assert steps, "at least one activation happened"
    assert all(s == "0" for s in steps), (
        f"every activation should finish on step 0, got steps {sorted(set(steps))}"
    )


async def test_recent_chat_is_prefetched_into_the_prompt(run_state, fake_llm):
    """Agents should see actual message text without spending a read_chat
    round trip."""
    await scheduler.advance_to_next_day(run_state)

    prompt = "\n".join(
        str(m.get("content") or "")
        for c in fake_llm.activation_calls()
        for m in c["messages"]
    )
    assert "caldaia" in prompt, "the admin's opening text reached the agent verbatim"
    assert "Condominio Via Garibaldi" in prompt


async def test_admin_message_mid_activation_never_lands_earlier(run_state, fake_llm):
    """Reproduces the original 'admin post jumps above replies' bug.

    While an agent is mid-activation, `state.clock` still lags behind the
    messages that agent is producing. An admin post that derived its stamp
    from the clock alone used to sort *before* messages already streamed to
    the browser.
    """
    from backend.main import _append_admin_message

    injected: list[Message] = []

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="Cosa è successo:\nniente.")
        # Simulate the admin hitting send at exactly this moment.
        if not injected:
            injected.append(
                _append_admin_message(
                    run_state, "main", "Avviso a metà giornata.",
                    [a.persona.id for a in run_state.agents],
                )
            )
        if caller.endswith(":step0"):
            return reply(
                tool_call("send_message", chat_id="Condominio Via Garibaldi", text="Ricevuto.")
            )
        return reply(tool_call("done"))

    fake_llm.responder = responder
    await scheduler.advance_to_next_day(run_state)

    assert injected, "the admin message was actually injected"
    admin_msg = injected[0]
    key = timeline.sort_key(admin_msg)
    earlier = [
        m for m in run_state.messages
        if timeline.sort_key(m) < key and m.id != admin_msg.id
    ]
    # Everything sorted before the admin post must have existed before it was
    # created — i.e. nothing generated afterwards slipped underneath it.
    assert all(m.seq < admin_msg.seq for m in earlier)

    ordered = timeline.in_order(run_state.messages)
    minutes = [m.fictional_timestamp_minutes for m in ordered]
    assert minutes == sorted(minutes)


async def test_containment_refusal_does_not_end_activation_silently(run_state, fake_llm):
    """A blocked send must let the agent try again, otherwise a forced
    activation can close the phone having produced nothing."""
    attempts = {"n": 0}

    def responder(kwargs):
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(content="niente")
        if not caller.startswith("agent:conti:"):
            return reply(tool_call("done"))
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Meta-vocabulary: refused by the containment filter.
            return reply(
                tool_call(
                    "send_message",
                    chat_id="Condominio Via Garibaldi",
                    text="Questa e' una simulazione, non credo a niente.",
                )
            )
        return reply(
            tool_call("send_message", chat_id="Condominio Via Garibaldi", text="Va bene, aspetto.")
        )

    fake_llm.responder = responder
    await scheduler.advance_to_next_day(run_state)

    conti_msgs = [m for m in run_state.messages if m.sender_id == "conti"]
    assert attempts["n"] >= 2, "the agent got a second chance after the refusal"
    assert conti_msgs, "and eventually said something clean"
    assert not any("simulazione" in m.content.lower() for m in conti_msgs)


async def test_memory_grows_each_day(run_state, fake_llm):
    from backend import memory as memory_mod

    await scheduler.advance_to_next_day(run_state)
    after_day1 = await memory_mod.read_memory(run_state, "conti")
    await scheduler.advance_to_next_day(run_state)
    after_day2 = await memory_mod.read_memory(run_state, "conti")

    assert "Giorno 1" in after_day1
    assert len(after_day2) > len(after_day1)
    assert "Giorno 2" in after_day2


async def test_run_state_survives_a_save_load_round_trip(run_state, fake_llm):
    from backend.storage import load_run, save_run

    await scheduler.advance_to_next_day(run_state)
    await save_run(run_state)
    reloaded = await load_run(run_state.run_id)

    assert reloaded is not None
    assert len(reloaded.messages) == len(run_state.messages)
    before = [(m.id, m.seq) for m in timeline.in_order(run_state.messages)]
    after = [(m.id, m.seq) for m in timeline.in_order(reloaded.messages)]
    assert before == after, "order is preserved across persistence"
