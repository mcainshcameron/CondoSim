"""Trust-matrix updates from voting outcomes.

Previously also held the 4-KPI (Treasury/Health/Satisfaction/Reputation)
update logic — removed in favor of a simpler game focused on relationships
and emergent drama.
"""
from __future__ import annotations

from .events import bus
from .logging_utils import log
from .models import Motion, RunState


def apply_trust_from_votes(state: RunState, motion: Motion) -> list[dict]:
    """Aligned vote on a closed motion: +0.1. Opposed: -0.05."""
    deltas: list[dict] = []
    voters = list(motion.votes.items())
    resident_ids = {a.persona.id for a in state.agents}
    voter_pairs = [
        (a1, v1, a2, v2)
        for i, (a1, v1) in enumerate(voters)
        for a2, v2 in voters[i + 1 :]
        if a1 in resident_ids and a2 in resident_ids
    ]
    for a1, v1, a2, v2 in voter_pairs:
        if v1 == "abstain" or v2 == "abstain":
            continue
        delta = 0.10 if v1 == v2 else -0.05
        state.trust.setdefault(a1, {})[a2] = _clamp(state.trust.get(a1, {}).get(a2, 0.0) + delta)
        state.trust.setdefault(a2, {})[a1] = _clamp(state.trust.get(a2, {}).get(a1, 0.0) + delta)
        deltas.append({"from": a1, "to": a2, "delta": delta, "cause": "aligned_vote" if v1 == v2 else "opposed_vote"})
    if deltas:
        log("dials", f"motion {motion.id} trust deltas: {len(deltas)} pairs")
        bus().publish(state.run_id, "trust_updated", {"deltas": deltas})
    return deltas


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
