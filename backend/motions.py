"""How a motion is counted — one rule, shared by both close paths.

A motion can close two ways: a resident's `vote` tool call trips
`tools._close_motion_if_ready`, or the administrator presses "Chiudi
votazione" and `main.api_close_motion` runs. Those two paths used to count
differently. The tool path tallied raw heads (`total // 2 + 1`); the API
path applied the quorum plus the 500/1000 millesimi majority. The divergence
only showed up once everybody had voted — greco + ferrari yes (470
millesimi), conti no, two abstained: the tool path declared the motion
*approvata*, the API path *respinta*. And the tool path won by construction,
because `api_close_motion` short-circuits on `status != "open"`, so the
weaker rule silently decided every contested motion.

Millesimi are the real voting unit of an Italian condominium and they live
on `Persona`, so the API path's rule is the correct one. It lives here, and
both call sites go through `tally_motion`. Each path keeps its own wording,
its own SSE publish and its own trust deltas — only the arithmetic is
shared.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Motion, RunState

# Seconda convocazione: the assembly is validly constituted with at least a
# third of the participants. For the five-flat cast that is 2, which is the
# literal the API path has always used.
QUORUM_MIN_ATTENDING = 2

# 500/1000 — a majority of the building by ownership share, not by head.
# Building millesimi tables are authored to total 1000 (see
# `data/buildings/{id}/residents.json`), which is also what the outcome note
# prints back to the admin.
MILLESIMI_MAJORITY = 500


@dataclass(frozen=True)
class MotionTally:
    """The counted result of a motion. Says nothing about *when* to close."""

    yes_ids: tuple[str, ...]
    no_ids: tuple[str, ...]
    abstain_ids: tuple[str, ...]
    yes_millesimi: int
    no_millesimi: int
    quorum_ok: bool
    passed: bool

    @property
    def headcount_yes(self) -> int:
        return len(self.yes_ids)

    @property
    def headcount_no(self) -> int:
        return len(self.no_ids)

    @property
    def abstain_count(self) -> int:
        return len(self.abstain_ids)

    @property
    def attending(self) -> int:
        """Abstentions are present but do not count toward the quorum —
        the same reading the API path has always applied."""
        return self.headcount_yes + self.headcount_no

    @property
    def cast(self) -> int:
        return self.headcount_yes + self.headcount_no + self.abstain_count

    @property
    def outcome(self) -> str:
        return "passed" if self.passed else "failed"


def tally_motion(state: RunState, motion: Motion) -> MotionTally:
    """Count a motion under the quorum + millesimi rule.

    Votes cast by ids that are not residents of this run are ignored for the
    millesimi weighting (they contribute 0), so a stale vote can never carry
    the building.
    """
    yes_ids = tuple(aid for aid, v in motion.votes.items() if v == "yes")
    no_ids = tuple(aid for aid, v in motion.votes.items() if v == "no")
    abstain_ids = tuple(aid for aid, v in motion.votes.items() if v == "abstain")

    millesimi_by_id = {a.persona.id: a.persona.millesimi for a in state.agents}
    yes_millesimi = sum(millesimi_by_id.get(aid, 0) for aid in yes_ids)
    no_millesimi = sum(millesimi_by_id.get(aid, 0) for aid in no_ids)

    quorum_ok = len(yes_ids) + len(no_ids) >= QUORUM_MIN_ATTENDING
    passed = (
        quorum_ok
        and len(yes_ids) > len(no_ids)
        and yes_millesimi >= MILLESIMI_MAJORITY
    )
    return MotionTally(
        yes_ids=yes_ids,
        no_ids=no_ids,
        abstain_ids=abstain_ids,
        yes_millesimi=yes_millesimi,
        no_millesimi=no_millesimi,
        quorum_ok=quorum_ok,
        passed=passed,
    )


def motion_is_decided(state: RunState, motion: Motion) -> bool:
    """Whether voting has gone far enough that the motion can be closed now.

    This is the *timing* question, deliberately separate from `tally_motion`'s
    *outcome* question: the admin may close a motion whenever they like, but
    the auto-close on the tool path must not fire while a resident who has not
    voted yet could still change the result. So: everyone has voted, or no
    combination of the outstanding votes can flip the current outcome.

    The old head-count shortcut ("a strict majority of residents") was fine
    while heads decided the result; under the millesimi rule it is not. Three
    residents holding 400 millesimi are a majority of heads and still cannot
    carry the building, and closing there would lock in *respinta* before the
    two owners who could carry it had voted.
    """
    residents = [a.persona.id for a in state.agents]
    if not residents:
        return False
    pending = [aid for aid in residents if aid not in motion.votes]
    if not pending:
        return True

    tally = tally_motion(state, motion)
    millesimi_by_id = {a.persona.id: a.persona.millesimi for a in state.agents}
    pending_millesimi = sum(millesimi_by_id.get(aid, 0) for aid in pending)

    # Approval is locked once the yes camp's head lead exceeds the number of
    # votes still out there: every remaining vote could be a no and it still
    # carries. (Millesimi only ever grow on the yes side from here.)
    if tally.passed and tally.headcount_yes - tally.headcount_no > len(pending):
        return True
    # Rejection is locked once the yes camp can no longer satisfy *both*
    # conditions even if every outstanding vote goes their way.
    if tally.headcount_yes + len(pending) <= tally.headcount_no:
        return True
    return tally.yes_millesimi + pending_millesimi < MILLESIMI_MAJORITY
