"""Trust-matrix updates from multiple in-world signals.

state.trust is nested: trust[speaker_id][listener_id] → float in [-1, 1].
All updates here are resident-to-resident only (the matrix exists only for
residents; admin and external contacts are not tracked).

Signals (per event, clamped):
  - aligned motion vote       sender ↔ voter        +0.10
  - opposed motion vote       sender ↔ voter        -0.05
  - positive reaction emoji   reactor → poster      +0.02
  - negative reaction emoji   reactor → poster      -0.04
  - forward another's msg     forwarder → poster    +0.01   ("worth sharing")
  - DM reply to someone       replier → partner     +0.02   ("keeping the thread alive")
  - attack-by-name in text    attacker → target     -0.05   (name + aggression lexicon in same msg)

Each signal publishes a `trust_updated` SSE event so the UI's alleanza panel
refreshes live.
"""
from __future__ import annotations

from .events import bus
from .logging_utils import log
from .models import Message, Motion, RunState


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

_POS_EMOJI = {
    "👍", "❤", "❤️", "🔥", "💯", "😊", "👏", "🙌", "🎉", "✨", "🤝",
}
_NEG_EMOJI = {
    "🙄", "😡", "😤", "👎", "💢", "😒",
}

# Reused from analyze.py — deliberately inlined to keep trust updates
# independent of the analyzer module.
_AGGRESSION_TERMS = [
    "stangata", "mazzate", "truffa", "ridicolo", "vergogna", "scandalo",
    "basta", "assurdo", "inaccettabile", "incompetente", "imbrogliato",
    "ladro", "disonesto", "nascondere", "sospetto", "bugie", "bugiardo",
    "denunciare", "avvocato", "tribunale", "schifo", "cafone", "maleducato",
    "arroganza", "fregatura", "furto", "patetico", "ossessione",
    "vittimismo", "vittimista", "ignorante", "pazzesco", "follia",
    "delirio", "squilibrato", "ubriaco",
]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _resident_ids(state: RunState) -> set[str]:
    return {a.persona.id for a in state.agents}


def _update(state: RunState, src: str, dst: str, delta: float) -> float:
    """Apply delta to trust[src][dst], clamped to [-1, 1]. Return the new value."""
    new = max(-1.0, min(1.0, state.trust.setdefault(src, {}).get(dst, 0.0) + delta))
    state.trust[src][dst] = new
    return new


def _publish(state: RunState, deltas: list[dict], cause_group: str) -> None:
    if not deltas:
        return
    log("dials", f"trust {cause_group}: {len(deltas)} deltas")
    bus().publish(state.run_id, "trust_updated", {
        "deltas": deltas,
        "cause_group": cause_group,
    })


# ---------------------------------------------------------------------------
# Signal: motion-close alignment (pre-existing)
# ---------------------------------------------------------------------------

def apply_trust_from_votes(state: RunState, motion: Motion) -> list[dict]:
    """Aligned vote on a closed motion: +0.10. Opposed: -0.05."""
    deltas: list[dict] = []
    voters = list(motion.votes.items())
    residents = _resident_ids(state)
    voter_pairs = [
        (a1, v1, a2, v2)
        for i, (a1, v1) in enumerate(voters)
        for a2, v2 in voters[i + 1:]
        if a1 in residents and a2 in residents
    ]
    for a1, v1, a2, v2 in voter_pairs:
        if v1 == "abstain" or v2 == "abstain":
            continue
        delta = 0.10 if v1 == v2 else -0.05
        cause = "aligned_vote" if v1 == v2 else "opposed_vote"
        _update(state, a1, a2, delta)
        _update(state, a2, a1, delta)
        deltas.append({"from": a1, "to": a2, "delta": delta, "cause": cause})
    _publish(state, deltas, "motion")
    return deltas


# ---------------------------------------------------------------------------
# Signal: emoji reaction
# ---------------------------------------------------------------------------

def on_reaction(state: RunState, reactor_id: str, target_msg: Message, emoji: str) -> list[dict]:
    """Reactor added `emoji` to target_msg. Small positive/negative nudge."""
    residents = _resident_ids(state)
    if reactor_id not in residents:
        return []
    if target_msg.sender_id == reactor_id:
        return []
    if target_msg.sender_id not in residents:
        return []
    emoji = (emoji or "").strip()
    if emoji in _POS_EMOJI:
        delta = 0.02
    elif emoji in _NEG_EMOJI:
        delta = -0.04
    else:
        return []  # neutral / ambiguous emoji — no signal
    _update(state, reactor_id, target_msg.sender_id, delta)
    deltas = [{
        "from": reactor_id, "to": target_msg.sender_id,
        "delta": delta, "cause": f"reaction:{emoji}",
    }]
    _publish(state, deltas, "reaction")
    return deltas


# ---------------------------------------------------------------------------
# Signal: forward
# ---------------------------------------------------------------------------

def on_forward(state: RunState, forwarder_id: str, original_sender_id: str) -> list[dict]:
    """Forwarding someone's message means you thought it was worth sharing.
    Small positive nudge regardless of the forwarder's comment tone."""
    residents = _resident_ids(state)
    if forwarder_id not in residents or original_sender_id not in residents:
        return []
    if forwarder_id == original_sender_id:
        return []
    _update(state, forwarder_id, original_sender_id, 0.01)
    deltas = [{
        "from": forwarder_id, "to": original_sender_id,
        "delta": 0.01, "cause": "forward",
    }]
    _publish(state, deltas, "forward")
    return deltas


# ---------------------------------------------------------------------------
# Signal: DM reply
# ---------------------------------------------------------------------------

def on_dm_reply(state: RunState, replier_id: str, partner_id: str) -> list[dict]:
    """Replying to a partner's DM keeps the thread alive — small +."""
    residents = _resident_ids(state)
    if replier_id not in residents or partner_id not in residents:
        return []
    _update(state, replier_id, partner_id, 0.02)
    deltas = [{
        "from": replier_id, "to": partner_id,
        "delta": 0.02, "cause": "dm_reply",
    }]
    _publish(state, deltas, "dm_reply")
    return deltas


# ---------------------------------------------------------------------------
# Signal: attack-by-name
# ---------------------------------------------------------------------------

def on_message_attack(state: RunState, sender_id: str, text: str) -> list[dict]:
    """If a message contains another resident's name AND an aggression term,
    penalize sender → target. Scans all residents; can produce multiple deltas
    when the sender attacks several neighbours in one message."""
    residents = _resident_ids(state)
    if sender_id not in residents or not text:
        return []
    low = text.lower()
    if not any(term in low for term in _AGGRESSION_TERMS):
        return []

    deltas: list[dict] = []
    for a in state.agents:
        if a.persona.id == sender_id:
            continue
        # Build identifier candidates: full display name, each name part
        # (excluding short particles like "sig.ra" or "di").
        name_lower = a.persona.display_name.lower()
        tokens = [t for t in name_lower.replace(".", " ").split() if len(t) >= 4]
        candidates = [name_lower] + tokens
        # Match on any candidate as a substring with word-ish boundaries
        hit = any(cand in low for cand in candidates)
        if not hit:
            continue
        _update(state, sender_id, a.persona.id, -0.05)
        deltas.append({
            "from": sender_id, "to": a.persona.id,
            "delta": -0.05, "cause": "attack_by_name",
        })

    _publish(state, deltas, "attack")
    return deltas
