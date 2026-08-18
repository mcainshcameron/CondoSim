"""What goes into a prompt, and what happens when writing memory fails.

Three defects meet here, all of them variations on "text that nobody bounded
gets pasted into a paid request, on every activation, forever":

* the admin goal is inlined in the system prompt AND in the notification
  prompt, and both ship in the same request — twice the tokens, unbounded;
* `build_context_digest` inlines message bodies at full length;
* the consolidation prompt was the only one in the app exempt from
  `window_memory`, so the call that decides what a resident *remembers* read
  sixteen days of diary while the resident it writes for reads six.

Plus the silent half: `consolidate_day` used to discard the
`gather(return_exceptions=True)` list, so a DB failure *after* the paid call
lost a diary day with no log line anywhere.
"""
from __future__ import annotations

from backend import agent as agent_mod
from backend import memory as memory_mod
from backend.config import MEMORY_DAYS_IN_PROMPT
from backend.tools import ToolContext

from .fake_llm import reply, tool_call
from .helpers import append_message as _append


def _ctx(state, agent):
    return ToolContext(
        state=state,
        agent_id=agent.persona.id,
        current_fictional_minutes=state.clock.minutes_since_start + 9 * 60,
    )


async def _seed_diary(state, agent_id: str, days: int) -> None:
    """A diary with `days` day-entries under the biographical seed."""
    seed = "Sono qui da vent'anni e non mi fido di nessuno.\nProprietaria."
    entries = "".join(
        f"\n\n--- Giorno {d}, lunedì ---\n"
        f"Annotazione del giorno numero {d}, quella con il fatto {d}.\n"
        for d in range(1, days + 1)
    )
    await memory_mod._write_memory(state.run_id, agent_id, seed + entries)


# ---------------------------------------------------------------------------
# 1. The consolidation prompt is windowed like every other prompt
# ---------------------------------------------------------------------------

async def test_consolidation_prompt_is_windowed(run_state, fake_llm):
    """The one call that decides what a resident remembers must not read a
    diary the resident themself never sees."""
    agent = run_state.agents[0]
    aid = agent.persona.id
    total_days = MEMORY_DAYS_IN_PROMPT + 10
    await _seed_diary(run_state, aid, total_days)
    # Clear the silent-day guard, which skips the LLM call entirely.
    _append(run_state, "romano", "resident", "La caldaia perde ancora, qualcuno ha chiamato?")
    _append(run_state, aid, "resident", "Io ieri ho chiamato, non rispondono.")

    fake_llm.responder = lambda kwargs: reply(
        content="Cosa è successo:\nNiente.\n\nCose da ricordare:\n- Nulla."
    )
    await memory_mod._consolidate_one(run_state, agent, run_state.clock.day)

    prompt = fake_llm.calls[-1]["messages"][0]["content"]
    headers = memory_mod._DAY_HEADER_RE.findall(prompt)
    assert len(headers) == MEMORY_DAYS_IN_PROMPT, (
        f"the diary handed to the writer is not windowed: {len(headers)} entries"
    )
    assert "--- Giorno 1, lunedì ---" not in prompt, "the oldest day is elided"
    assert f"--- Giorno {total_days}, lunedì ---" in prompt, (
        "yesterday must survive the window"
    )
    assert "giorni precedenti" in prompt, "the elision marker explains the gap"
    assert "non mi fido di nessuno" in prompt, "the biographical seed is bedrock"


async def test_consolidation_prompt_is_untouched_on_a_short_run(run_state, fake_llm):
    """A run shorter than the window must read byte-identically to before."""
    agent = run_state.agents[0]
    aid = agent.persona.id
    await _seed_diary(run_state, aid, MEMORY_DAYS_IN_PROMPT - 1)
    _append(run_state, "romano", "resident", "La caldaia perde ancora, qualcuno ha chiamato?")
    _append(run_state, aid, "resident", "Io ieri ho chiamato, non rispondono.")

    fake_llm.responder = lambda kwargs: reply(content="Cosa è successo:\nNiente.")
    await memory_mod._consolidate_one(run_state, agent, run_state.clock.day)

    prompt = fake_llm.calls[-1]["messages"][0]["content"]
    assert "--- Giorno 1, lunedì ---" in prompt
    assert "giorni precedenti" not in prompt


# ---------------------------------------------------------------------------
# 2. Admin goal: bounded at BOTH sinks, because both ship in one request
# ---------------------------------------------------------------------------

async def test_oversized_admin_goal_is_clamped_in_both_prompt_sinks(run_state):
    """A 128 KB goal is ~50k input tokens per activation against a ~$0.000064
    baseline — a ~100x multiplier that trips the run cap in ~100 wake-ups and
    reports only `cost_cap_exceeded`."""
    agent = run_state.agents[0]
    ctx = _ctx(run_state, agent)

    agent.admin_goal = ""
    base_system = await agent_mod.build_system_prompt(run_state, agent)
    base_notify = agent_mod.build_notification_prompt(ctx, agent, "inbox", "")

    agent.admin_goal = "Il tetto perde e nessuno paga. " * 4000  # ~124 KB
    big_system = await agent_mod.build_system_prompt(run_state, agent)
    big_notify = agent_mod.build_notification_prompt(ctx, agent, "inbox", "")

    limit = agent_mod.ADMIN_GOAL_PROMPT_LIMIT
    slack = 200  # the framing lines that wrap the goal in each prompt
    assert len(big_system) - len(base_system) <= limit + slack, "system prompt sink"
    assert len(big_notify) - len(base_notify) <= limit + slack, "notification sink"
    # Both prompts go out in the SAME request: the pair is what actually gets
    # billed, and the pair is what has to stay bounded.
    assert len(big_system) + len(big_notify) < 25_000
    for prompt in (big_system, big_notify):
        assert "Il tetto perde e nessuno paga." in prompt, "the goal still lands"
        assert prompt.rstrip().endswith("…") or "…" in prompt, "the cut is marked"


async def test_ordinary_admin_goal_is_passed_through_verbatim(run_state):
    """The clamp is a backstop, not a rewrite: a goal of realistic length has
    to reach the model exactly as the admin typed it."""
    agent = run_state.agents[0]
    goal = (
        "Ti sei accorto che il conto corrente del condominio non torna: "
        "mancano circa 4.000 euro e nessuno sa dire dove siano finiti."
    )
    agent.admin_goal = goal
    system = await agent_mod.build_system_prompt(run_state, agent)
    notify = agent_mod.build_notification_prompt(_ctx(run_state, agent), agent, "inbox", "")
    assert goal in system
    assert goal in notify


# ---------------------------------------------------------------------------
# 3. The digest inlines bodies, so bodies need a bound too
# ---------------------------------------------------------------------------

async def test_digest_clamps_a_single_enormous_message(run_state):
    """MAIN_CHAT_CONTEXT bounds the message *count*; one announcement can
    still carry more text than a whole day of chat."""
    agent = run_state.agents[0]
    _append(run_state, "admin", "admin", "AVVISO. " * 5000)
    digest = agent_mod.build_context_digest(_ctx(run_state, agent), agent)
    assert len(digest) < agent_mod.DIGEST_BODY_LIMIT + 2000
    assert "AVVISO." in digest, "the start of the announcement is still readable"


async def test_digest_leaves_real_messages_alone(run_state):
    """The longest message across the 24 saved runs is 546 chars. The clamp
    must not change what a well-behaved transcript looks like."""
    agent = run_state.agents[0]
    body = "Guardate che il preventivo della caldaia non torna. " * 10  # 520 chars
    _append(run_state, "romano", "resident", body)
    digest = agent_mod.build_context_digest(_ctx(run_state, agent), agent)
    assert body.strip() in digest
    assert "…" not in digest


# ---------------------------------------------------------------------------
# 4. The building is data: name and city come from the run, not from Python
# ---------------------------------------------------------------------------

async def test_group_name_comes_from_the_chat_list(run_state):
    """Renaming the main chat used to leave the resident being told to write
    in a group `_resolve_chat` can no longer find — one of three tool steps
    burned on "quella chat non compare nel tuo telefono"."""
    main = next(c for c in run_state.chats if c.kind == "main")
    main.display_name = "Condominio Il Girasole"
    prompt = await agent_mod.build_system_prompt(run_state, run_state.agents[0])

    assert "del Condominio Il Girasole" in prompt, "the 'where you live' line"
    assert "gruppo condominiale \"Condominio Il Girasole\"" in prompt, "the phone line"
    assert "Condominio Via Garibaldi" not in prompt, "no hardcoded building left"


async def test_city_comes_from_building_json(run_state):
    prompt = await agent_mod.build_system_prompt(run_state, run_state.agents[0])
    assert ", a Milano." in prompt


async def test_missing_city_just_drops_the_clause(run_state, monkeypatch):
    """`city` is optional, so a building authored without it must still
    produce a sentence."""
    monkeypatch.setattr(
        agent_mod.building, "building_scene", lambda bid: ("Condominio Senza Nome", "")
    )
    agent = run_state.agents[0]
    prompt = await agent_mod.build_system_prompt(run_state, agent)
    assert f"Vivi in {agent.persona.unit} del Condominio Via Garibaldi." in prompt
    assert ", a " not in prompt.splitlines()[0]


async def test_unreadable_building_json_does_not_kill_the_activation(run_state, monkeypatch):
    """Prompt assembly happens mid-run: a bad building.json must degrade to a
    missing city, not to a dead turn."""
    monkeypatch.setattr(
        agent_mod.building, "load_building",
        lambda bid: (_ for _ in ()).throw(ValueError("boom")),
    )
    prompt = await agent_mod.build_system_prompt(run_state, run_state.agents[0])
    assert "Condominio Via Garibaldi" in prompt, "the chat list still names the group"


# ---------------------------------------------------------------------------
# 5. Failures after the paid call must be loud
# ---------------------------------------------------------------------------

async def test_append_failure_after_consolidation_is_logged_and_keeps_the_notes(
    run_state, fake_llm, monkeypatch
):
    agent = run_state.agents[0]
    aid = agent.persona.id
    agent.notes = ["la caldaia perde", "Ferrari non risponde mai"]
    _append(run_state, "romano", "resident", "La caldaia perde ancora, qualcuno ha chiamato?")
    _append(run_state, aid, "resident", "Io ieri ho chiamato, non rispondono.")

    async def boom(run_id, agent_id, addition):
        raise RuntimeError("connection reset by peer")

    errors: list[str] = []
    monkeypatch.setattr(memory_mod, "_append_memory", boom)
    monkeypatch.setattr(memory_mod, "log_error", lambda tag, msg: errors.append(msg))

    fake_llm.responder = lambda kwargs: reply(content="Cosa è successo:\nQualcosa.")
    await memory_mod._consolidate_one(run_state, agent, run_state.clock.day)

    assert any("append failed" in e and aid in e for e in errors), errors
    assert agent.notes, "the day was never absorbed — today's notes must survive"


async def test_consolidate_day_logs_a_crashed_agent(run_state, fake_llm, monkeypatch):
    """`return_exceptions=True` is correct; discarding the list it returns is
    not — that is how a diary day disappeared with no log output at all."""
    victim = run_state.agents[0].persona.id
    errors: list[str] = []

    async def one(state, agent, day):
        if agent.persona.id == victim:
            raise RuntimeError("pool exhausted")

    monkeypatch.setattr(memory_mod, "_consolidate_one", one)
    monkeypatch.setattr(memory_mod, "log_error", lambda tag, msg: errors.append(msg))

    await memory_mod.consolidate_day(run_state, run_state.clock.day)

    assert any(victim in e and "pool exhausted" in e for e in errors), errors
    assert len(errors) == 1, "only the agent that actually failed is reported"


# ---------------------------------------------------------------------------
# 6. The tool log line is served by an unauthenticated endpoint
# ---------------------------------------------------------------------------

async def test_tool_logging_never_carries_chat_text(run_state, fake_llm, monkeypatch):
    """/api/debug/logs hands the ring buffer to anyone in open-beta mode."""
    secret = "il consigliere ha preso una mazzetta da quattromila euro"
    fake_llm.responder = lambda kwargs: reply(
        tool_call("send_message", chat_id="Condominio Via Garibaldi", text=secret)
    )
    lines: list[str] = []
    monkeypatch.setattr(agent_mod, "log", lambda tag, msg: lines.append(msg))

    agent = run_state.agents[0]
    await agent_mod.activate_agent(
        run_state, agent.persona.id, run_state.clock.minutes_since_start + 9 * 60
    )

    assert [m for m in run_state.messages if m.content == secret], "the send landed"
    tool_lines = [ln for ln in lines if "tool=send_message" in ln]
    assert tool_lines, f"the tool call should still be logged: {lines}"
    assert all(secret not in ln for ln in lines), "chat text reached the log buffer"
    assert f"text={len(secret)}ch" in tool_lines[0], (
        f"the shape of the call is what's useful: {tool_lines[0]}"
    )
