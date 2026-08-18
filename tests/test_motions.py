"""Motions: one tally, one resolver.

Two defects from the 2026-08-01 review, both in code with zero coverage:

- A motion could close two ways and the two ways counted differently. The
  residents' `vote` tool tallied raw heads; the admin's "Chiudi votazione"
  button applied the quorum and the 500/1000 millesimi majority. `greco +
  ferrari` hold 470 millesimi between them, so their two yes votes against
  conti's one no closed as *approvata* through the tool and would have closed
  as *respinta* through the API — and the tool path won by construction,
  because `api_close_motion` short-circuits on an already-closed motion.
- `tool_vote`'s title fallback matched any motion, open or closed, and took
  the oldest. "amministratore" in run_0c355245 matched both open motions and
  landed the vote on *Mozione di sfiducia* rather than *Nomina nuovo
  amministratore*, then reported success naming the wrong one.

`_close_motion_if_ready` has never executed in any of the 24 saved runs —
several of them still hold 5/5-yes motions with `status: "open"`, meaning the
function postdates them. Nothing below can be treated as a regression test for
observed behaviour; it is the first behaviour this code has ever been pinned to.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import storage
from backend.main import app, limiter
from backend.models import Motion, RunState
from backend.motions import motion_is_decided, tally_motion
from backend.tools import ToolContext, dispatch_tool

# Millesimi as authored in data/buildings/001/residents.json. The whole point
# of the shared rule is that these, not head counts, decide a motion.
MILLESIMI = {"conti": 150, "ferrari": 170, "greco": 300, "marchetti": 180, "romano": 200}


@pytest.fixture
def client():
    was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        with TestClient(app) as c:
            yield c
    finally:
        limiter.enabled = was_enabled


def _ctx(state: RunState, agent_id: str) -> ToolContext:
    return ToolContext(
        state=state,
        agent_id=agent_id,
        current_fictional_minutes=state.clock.minutes_since_start + 10,
    )


def _motion(state: RunState, motion_id: str, title: str, status: str = "open") -> Motion:
    motion = Motion(
        id=motion_id,
        title=title,
        description="Testo della mozione.",
        proposer_id="greco",
        proposer_display_name="Sig. Greco",
        day_proposed=state.clock.day,
        proposed_at_fictional_min=state.clock.minutes_since_start,
        status=status,  # type: ignore[arg-type]
    )
    state.motions.append(motion)
    return motion


def _stored(run_id: str) -> RunState:
    """Read the run straight out of the offline store. The API tests need to
    plant votes without going through `vote`, which would auto-close the
    motion and make `api_close_motion` a no-op — the exact short-circuit that
    let the two tallies diverge unnoticed."""
    return RunState.model_validate_json(storage._MEM_RUNS[run_id])


def _restore(state: RunState) -> None:
    storage._MEM_RUNS[state.run_id] = state.model_dump_json()


def _plant_motion_with_votes(client, votes: dict[str, str], title: str = "Preventivo caldaia") -> tuple[str, str]:
    resp = client.post(
        "/api/runs",
        json={"opening_text": "La caldaia e' ferma da stanotte.", "building_id": "001"},
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    state = _stored(run_id)
    motion = _motion(state, "m_planted", title)
    motion.votes = dict(votes)  # type: ignore[assignment]
    _restore(state)
    return run_id, motion.id


# ---------------------------------------------------------------------------
# The 470-millesimi case — the two paths must now agree
# ---------------------------------------------------------------------------

async def test_the_building_millesimi_table_is_what_these_tests_assume(run_state):
    """Every number below (470, 500, 670) comes off this table, and the
    outcome note prints "/1000" as a literal. Re-authoring residents.json
    would otherwise turn the interesting cases into vacuous ones."""
    assert {a.persona.id: a.persona.millesimi for a in run_state.agents} == MILLESIMI
    assert sum(MILLESIMI.values()) == 1000

# greco (300) + ferrari (170) = 470. A head-count majority of those who voted,
# and still short of the 500/1000 the building needs to decide anything.
CONTESTED = {
    "greco": "yes",
    "ferrari": "yes",
    "conti": "no",
    "marchetti": "abstain",
    "romano": "abstain",
}


async def test_470_millesimi_does_not_carry_the_building(run_state):
    motion = _motion(run_state, "m_shared", "Preventivo caldaia")
    motion.votes = dict(CONTESTED)  # type: ignore[assignment]
    tally = tally_motion(run_state, motion)
    assert tally.headcount_yes == 2 and tally.headcount_no == 1
    assert tally.yes_millesimi == 470
    assert tally.quorum_ok is True, "three residents voted; the assembly is constituted"
    assert tally.passed is False
    assert tally.outcome == "failed"


async def test_tool_path_closes_the_contested_motion_as_failed(run_state):
    """The vote tool used to call this one 'approvata' on a bare head count."""
    motion = _motion(run_state, "m_tool", "Preventivo caldaia")
    for agent_id, choice in CONTESTED.items():
        ctx = _ctx(run_state, agent_id)
        result = dispatch_tool(ctx, "vote", {"motion_id": "m_tool", "choice": choice})
        assert "Voto registrato" in result

    assert motion.status == "failed"
    assert "respinta" in result, "the last voter is told the outcome that actually applies"
    tally_line = [m for m in run_state.messages if m.content.startswith("📋 [Esito mozione]")]
    assert len(tally_line) == 1
    assert "respinta" in tally_line[0].content


async def test_tool_path_tally_line_stays_bookkeeping(run_state):
    """A scoreboard is not the administrator talking. Without the flag the
    tally is quoted back to residents as admin speech and force-activates the
    whole cast the next morning."""
    _motion(run_state, "m_book", "Preventivo caldaia")
    for agent_id, choice in CONTESTED.items():
        dispatch_tool(_ctx(run_state, agent_id), "vote", {"motion_id": "m_book", "choice": choice})
    tally_line = next(m for m in run_state.messages if m.content.startswith("📋 [Esito mozione]"))
    assert tally_line.sender_kind == "admin"
    assert tally_line.bookkeeping is True


def test_api_path_closes_the_contested_motion_as_failed(client):
    run_id, motion_id = _plant_motion_with_votes(client, CONTESTED)
    resp = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")
    assert resp.status_code == 200, resp.text
    motion = resp.json()["motion"]
    assert motion["status"] == "failed"
    assert "470/1000 millesimi" in motion["outcome_note"]
    assert "Respinta" in motion["outcome_note"]


# A real majority must still pass, or the fix has simply broken voting.
CLEAR_MAJORITY = {
    "greco": "yes",
    "ferrari": "yes",
    "romano": "yes",
    "conti": "no",
    "marchetti": "no",
}


async def test_tool_path_still_passes_a_real_majority(run_state):
    motion = _motion(run_state, "m_pass", "Preventivo caldaia")
    for agent_id, choice in CLEAR_MAJORITY.items():
        dispatch_tool(_ctx(run_state, agent_id), "vote", {"motion_id": "m_pass", "choice": choice})
    assert motion.status == "passed"
    assert tally_motion(run_state, motion).yes_millesimi == 670


def test_api_path_still_passes_a_real_majority(client):
    run_id, motion_id = _plant_motion_with_votes(client, CLEAR_MAJORITY)
    resp = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")
    assert resp.json()["motion"]["status"] == "passed"


async def test_a_single_vote_never_carries_a_motion(run_state):
    """Quorum: one resident voting alone is not an assembly, however many
    millesimi they hold. greco alone is 300 — under the threshold anyway —
    so use the vote nobody could out-weigh."""
    motion = _motion(run_state, "m_quorum", "Preventivo caldaia")
    motion.votes = {"greco": "yes", "romano": "yes"}  # type: ignore[assignment]
    assert tally_motion(run_state, motion).passed is True, "500/1000 exactly is a majority"
    motion.votes = {"greco": "yes"}  # type: ignore[assignment]
    tally = tally_motion(run_state, motion)
    assert tally.quorum_ok is False
    assert tally.passed is False


async def test_abstentions_close_the_motion_once_yes_can_no_longer_reach_500(run_state):
    """Rejection is locked the moment the millesimi still outstanding cannot
    reach the majority, whoever holds them. greco (300) abstaining is what
    settles this one: marchetti + romano are 380 between them and the two
    of them voting yes would still not carry the building."""
    motion = _motion(run_state, "m_abst", "Preventivo caldaia")
    for agent_id in ("conti", "ferrari"):
        dispatch_tool(_ctx(run_state, agent_id), "vote", {"motion_id": "m_abst", "choice": "abstain"})
    assert motion.status == "open", "820 millesimi still able to decide it"
    dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "m_abst", "choice": "abstain"})
    assert motion.status == "failed"
    assert motion.outcome_note == "0 sì, 0 no, 3 astenuti"


async def test_motion_stays_open_while_the_result_can_still_change(run_state):
    """The auto-close is about timing, not verdict: it must not slam the door
    on residents who could still decide the thing."""
    motion = _motion(run_state, "m_open", "Preventivo caldaia")
    dispatch_tool(_ctx(run_state, "conti"), "vote", {"motion_id": "m_open", "choice": "yes"})
    assert motion_is_decided(run_state, motion) is False
    assert motion.status == "open"
    dispatch_tool(_ctx(run_state, "ferrari"), "vote", {"motion_id": "m_open", "choice": "yes"})
    assert motion.status == "open", "320/1000 with three residents still to vote"


# ---------------------------------------------------------------------------
# Which motion did the agent mean?
# ---------------------------------------------------------------------------

async def test_exact_code_wins(run_state):
    _motion(run_state, "m_aaa", "Nomina nuovo amministratore")
    _motion(run_state, "m_bbb", "Preventivo caldaia")
    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "m_bbb", "choice": "yes"})
    assert "Preventivo caldaia" in result
    assert run_state.motions[0].votes == {}
    assert run_state.motions[1].votes == {"greco": "yes"}


async def test_title_match_prefers_the_newest_open_motion_over_a_closed_one(run_state):
    """run_0c355245's collision, with the older match already closed. The old
    resolver ignored `status` and took the first insertion-order hit, so the
    vote went to a motion that was over."""
    closed = _motion(run_state, "m_old", "Sfiducia all'amministratore", status="failed")
    _motion(run_state, "m_mid", "Rifacimento facciata")
    newest = _motion(run_state, "m_new", "Nomina nuovo amministratore")

    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "amministratore", "choice": "yes"})
    assert "Nomina nuovo amministratore" in result
    assert newest.votes == {"greco": "yes"}
    assert closed.votes == {}


async def test_two_open_motions_matching_the_same_word_refuse_and_name_both(run_state):
    """The politically opposite act. "amministratore" matches a no-confidence
    motion and an appointment; guessing either way is worse than asking, so
    the refusal carries both codes and the agent can retry precisely."""
    sfiducia = _motion(run_state, "m_sfid", "Mozione di sfiducia all'amministratore")
    nomina = _motion(run_state, "m_nom", "Nomina nuovo amministratore")

    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "amministratore", "choice": "yes"})
    assert "m_sfid" in result and "m_nom" in result
    assert sfiducia.votes == {} and nomina.votes == {}
    assert "Voto registrato" not in result


async def test_title_matching_only_a_closed_motion_says_so(run_state):
    """"Non trovo" is a lie when the motion is right there in the transcript;
    the agent needs to know it has closed, not go looking for a typo."""
    _motion(run_state, "m_done", "Preventivo caldaia", status="passed")
    _motion(run_state, "m_live", "Rifacimento facciata")
    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "caldaia", "choice": "yes"})
    assert result == 'La mozione "Preventivo caldaia" è già chiusa.'


async def test_exact_code_of_a_closed_motion_says_so(run_state):
    _motion(run_state, "m_done", "Preventivo caldaia", status="failed")
    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "m_done", "choice": "yes"})
    assert "già chiusa" in result


async def test_unknown_title_lists_what_is_open(run_state):
    _motion(run_state, "m_live", "Rifacimento facciata")
    result = dispatch_tool(_ctx(run_state, "greco"), "vote", {"motion_id": "ascensore", "choice": "yes"})
    assert "Non trovo" in result
    assert "m_live" in result
