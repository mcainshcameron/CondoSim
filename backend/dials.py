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
  - attack-by-name in text    attacker → target     -0.05   (name + aggression lexicon in same clause)

Each signal publishes a `trust_updated` SSE event so the UI's alleanza panel
refreshes live.
"""
from __future__ import annotations

import re

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

# Sentence-ish split for the attack detector. The trailing `(?:\s|$)` is
# load-bearing, not tidiness: Italian honorifics carry a period *inside* the
# name ("Sig.ra Conti"), and a bare `[.!?;]` split would cut it in half and
# strand the surname in a clause of its own. Requiring whitespace after the
# punctuation also leaves "3.5" and "art.1102" intact.
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;…]+(?:\s|$)")

# Saying "concordo con Ferrari" and then being angry at someone else is the
# single most common shape in the saved runs, and the old detector scored it
# as an attack on Ferrari. If one of these sits immediately in front of the
# name, the aggression in the clause is aimed past the person named, not at
# them. Kept short and literal — a general stance classifier is a different
# (and much more expensive) job.
_AGREEMENT_MARKERS = (
    "concordo con",
    "d'accordo con",
    "ha ragione",
    "come dice",
    "sono con",
)
_AGREEMENT_WINDOW_CHARS = 30

_NAME_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


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


def publish_deltas(state: RunState, deltas: list[dict], cause_group: str) -> None:
    """Stream a batch of trust deltas to SSE subscribers.

    Public because `main.api_close_motion` has to split the mutation from the
    publish: every mutating endpoint saves before it publishes, and this event
    used to escape that rule by firing from inside `apply_trust_from_votes`.
    """
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

def apply_trust_from_votes(
    state: RunState, motion: Motion, *, publish: bool = True
) -> list[dict]:
    """Aligned vote on a closed motion: +0.10. Opposed: -0.05.

    `publish=False` returns the deltas without streaming them, for callers
    that must persist the mutation before any subscriber can see it. They own
    the `publish_deltas(state, deltas, "motion")` call afterwards.
    """
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
    if publish:
        publish_deltas(state, deltas, "motion")
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
    publish_deltas(state, deltas, "reaction")
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
    publish_deltas(state, deltas, "forward")
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
    publish_deltas(state, deltas, "dm_reply")
    return deltas


# ---------------------------------------------------------------------------
# Signal: attack-by-name
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase and fold the typographic apostrophe onto the ASCII one, so
    "d’accordo con" (what the models actually emit) matches the marker list."""
    return text.lower().replace("’", "'")


def _name_pattern(display_name: str) -> re.Pattern[str]:
    """Alternation over the full display name and its long name parts, each
    fenced so it cannot match inside a longer word.

    The fence is `(?<!\\w)`/`(?!\\w)` rather than `\\b` because a candidate can
    begin or end on a non-word character. `\\bsig\\.ra conti\\b` is fine, but the
    same construction around a name ending in a period matches nothing at all:
    `\\b` after "." needs an adjacent word character and the next char is a
    space. The lookarounds say what we actually mean — "not glued to a longer
    word" — for every candidate shape.

    Candidates are tried longest-first so "Sig.ra Conti" is consumed whole
    and the agreement-marker window is measured from the honorific, not from
    the surname buried inside it.
    """
    cached = _NAME_PATTERN_CACHE.get(display_name)
    if cached is not None:
        return cached
    low = _normalize(display_name)
    # Name parts, minus honorifics and particles: "Sig.ra Conti" → ["conti"].
    parts = [t for t in low.replace(".", " ").split() if len(t) >= 4]
    candidates = sorted({low, *parts}, key=len, reverse=True)
    alternation = "|".join(re.escape(c) for c in candidates)
    pat = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")
    _NAME_PATTERN_CACHE[display_name] = pat
    return pat


def _attacks_name(display_name: str, hostile_clauses: list[str]) -> bool:
    """True when the name appears in a clause that also carries aggression,
    and is not being agreed with at that spot."""
    pat = _name_pattern(display_name)
    for clause in hostile_clauses:
        for m in pat.finditer(clause):
            window = clause[max(0, m.start() - _AGREEMENT_WINDOW_CHARS):m.start()]
            if any(marker in window for marker in _AGREEMENT_MARKERS):
                continue
            return True
    return False


def on_message_attack(state: RunState, sender_id: str, text: str) -> list[dict]:
    """If a message names another resident in the same clause as an aggression
    term, penalize sender → target. Scans all residents; can produce multiple
    deltas when the sender attacks several neighbours in one message.

    Clause scope, not message scope, is what makes this a signal rather than
    noise. "Concordo con Ferrari. Basta perdite di tempo." is one of the most
    common shapes in the transcripts, and reading the whole message as one bag
    of words scored it as an attack on the person being *agreed with* —
    inverting the sign of the very signal the alleanza panel is built to show.
    """
    residents = _resident_ids(state)
    if sender_id not in residents or not text:
        return []
    low = _normalize(text)
    if not any(term in low for term in _AGGRESSION_TERMS):
        return []

    # Only clauses that carry aggression can host an attack; a name anywhere
    # else in the message is just a name.
    clauses = (part.strip() for part in _CLAUSE_SPLIT_RE.split(low))
    hostile_clauses = [
        c for c in clauses
        if any(term in c for term in _AGGRESSION_TERMS)
    ]

    deltas: list[dict] = []
    for a in state.agents:
        if a.persona.id == sender_id:
            continue
        if not _attacks_name(a.persona.display_name, hostile_clauses):
            continue
        _update(state, sender_id, a.persona.id, -0.05)
        deltas.append({
            "from": sender_id, "to": a.persona.id,
            "delta": -0.05, "cause": "attack_by_name",
        })

    publish_deltas(state, deltas, "attack")
    return deltas
