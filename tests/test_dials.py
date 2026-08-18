"""Trust-matrix signals (backend/dials.py).

The attack-by-name cases are not invented: every string marked "real" below is
copied verbatim out of a transcript in `data/runs/`. Replaying the detector
over all 24 saved runs fired 70 deltas, 23 of them pointed at the wrong person
— usually at the neighbour the sender was *agreeing with*. Those transcripts
are the only regression corpus this module has, so the ones that mattered live
here as tests.

Note the two directions this file has to hold at once: the false positives must
go to zero, and the genuine insults must keep firing. It is trivial to fix the
first by breaking the second.
"""
from __future__ import annotations

import pytest

from backend import dials
from backend.models import Message, Motion

# --- helpers -------------------------------------------------------------

def _attack(state, sender_id: str, text: str) -> list[dict]:
    """Run the detector against a clean trust matrix."""
    state.trust = {}
    return dials.on_message_attack(state, sender_id, text)


def _targets(deltas: list[dict]) -> set[str]:
    return {d["to"] for d in deltas}


def _msg(sender_id: str, content: str = "ciao") -> Message:
    return Message(
        id=f"m-{sender_id}",
        chat_id="main",
        sender_id=sender_id,
        sender_kind="resident" if sender_id != "admin" else "admin",
        sender_display_name=sender_id.title(),
        content=content,
        fictional_timestamp_minutes=600,
        seq=1,
        wall_clock_iso="2026-01-01T00:00:00Z",
        day=1,
    )


def _motion(votes: dict[str, str]) -> Motion:
    return Motion(
        id="mo-1",
        title="Revoca dell'amministratore",
        description="Si propone la revoca.",
        proposer_id="conti",
        proposer_display_name="Sig.ra Conti",
        day_proposed=2,
        proposed_at_fictional_min=1500,
        votes=votes,
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture what dials publishes on the SSE bus, without a subscriber."""
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        def publish(self, run_id, event_type, data):
            events.append((event_type, data))

    monkeypatch.setattr(dials, "bus", lambda: _FakeBus())
    return events


# --- attack-by-name: the two wrong-signed cases from the saved runs -------

def test_agreement_then_anger_at_a_third_party_is_not_an_attack(run_state):
    """real, run_e531ee92. The anger is at the administrator; Ferrari is the
    ally being endorsed. The old bag-of-words read penalised romano → ferrari."""
    assert _attack(run_state, "romano", "Concordo con Ferrari. Basta perdite di tempo.") == []


def test_continua_does_not_match_conti(run_state):
    """real, run_0c355245. "continua" contains "conti"; Sig.ra Conti is not
    mentioned anywhere in this message. Ferrari is mentioned, but in a clause
    that carries no aggression — he is being told to go ahead."""
    text = (
        "Ma il tema non è il salto nel buio, è chi continua a voler pagare per "
        "farsi gestire male. Se vogliamo il cambiamento, basta perdere tempo "
        "con le scuse. Ferrari, procedi."
    )
    assert _attack(run_state, "romano", text) == []


def test_bare_surname_never_matches_inside_a_longer_word(run_state):
    """The whole "conti"/"continua" family, plus the aggression term sitting
    right next to it so only the name anchoring can save us."""
    for text in (
        "Basta, questa storia continua da mesi e nessuno fa niente.",
        "Non possiamo continuare così, è assurdo.",
        "Il conteggio dei millesimi è ridicolo.",
    ):
        assert _attack(run_state, "romano", text) == [], text


# --- attack-by-name: what must still fire --------------------------------

def test_direct_insult_still_fires(run_state):
    """real, run_28c12b45. Name and insult in one clause, no hedging."""
    deltas = _attack(run_state, "ferrari", "Marchetti, il tuo livore costante è patetico.")
    assert _targets(deltas) == {"marchetti"}
    assert deltas[0]["delta"] == -0.05
    assert deltas[0]["cause"] == "attack_by_name"
    assert run_state.trust["ferrari"]["marchetti"] == pytest.approx(-0.05)


def test_honorific_display_name_still_fires(run_state):
    """real, run_28c12b45. "Sig.ra Conti" carries a period *inside* the name:
    a naive clause split on [.!?;] would cut it in half and lose the target."""
    text = (
        "Prove? Le prove sono sotto gli occhi di tutti, Sig.ra Conti, basta "
        "volerle guardare."
    )
    assert _targets(_attack(run_state, "marchetti", text)) == {"conti"}


def test_first_name_still_fires(run_state):
    """real, run_5cd1a537. Residents address each other by first name as often
    as by surname, so both parts of the display name have to resolve."""
    text = "Davide, la tua ossessione per i bilanci non rende il palazzo più vivibile."
    assert _targets(_attack(run_state, "romano", text)) == {"marchetti"}


def test_one_message_can_attack_two_neighbours(run_state):
    deltas = _attack(run_state, "greco", "Ferrari e Marchetti, basta con queste bugie.")
    assert _targets(deltas) == {"ferrari", "marchetti"}
    assert len(deltas) == 2


# --- attack-by-name: clause scoping and agreement markers ----------------

def test_aggression_in_a_different_clause_does_not_attach_to_a_name(run_state):
    """real, run_c9df435b. The apology and the exasperation are different
    sentences; only the apology names Ferrari."""
    text = (
        "Amministratore, è assurdo aspettare fino a lunedì. Ferrari, mi scuso "
        "per il disagio, risolviamo subito."
    )
    assert _attack(run_state, "romano", text) == []


@pytest.mark.parametrize("text", [
    "Concordo con la Sig.ra Conti, basta scuse.",
    "Sono d'accordo con Marco Ferrari, questa è una vergogna.",
    "Ha ragione Davide Marchetti, basta prenderci in giro.",
    "Come dice Giulia Romano, è tutto inaccettabile.",
    "Io sono con Valentina Greco, questa è una truffa.",
])
def test_agreement_marker_before_the_name_suppresses_the_delta(run_state, text):
    """Same clause, real aggression — but aimed past the person named."""
    assert _attack(run_state, "conti", text) == []


def test_typographic_apostrophe_still_reads_as_agreement(run_state):
    """The models emit U+2019, not the ASCII apostrophe, roughly half the time."""
    assert _attack(run_state, "conti", "Sono d’accordo con Marco Ferrari, che vergogna.") == []


def test_agreement_suppresses_only_the_name_it_precedes(run_state):
    """Endorsing one neighbour while insulting another must keep the insult.
    This is the guard against fixing the false positives by muting the module."""
    deltas = _attack(run_state, "romano", "Concordo con Ferrari. Marchetti, basta paranoie.")
    assert _targets(deltas) == {"marchetti"}


# --- attack-by-name: guards ----------------------------------------------

def test_no_aggression_term_means_no_signal(run_state):
    assert _attack(run_state, "romano", "Ferrari, hai visto l'avviso in bacheca?") == []


def test_sender_is_never_penalised_toward_themselves(run_state):
    assert _attack(run_state, "greco", "Valentina Greco non ci sta, è uno scandalo.") == []


def test_non_resident_senders_are_ignored(run_state):
    assert _attack(run_state, "admin", "Marchetti, è ridicolo.") == []
    assert _attack(run_state, "romano", "") == []


def test_attack_publishes_one_sse_event(run_state, captured):
    _attack(run_state, "ferrari", "Marchetti, sei un incompetente.")
    assert [e[0] for e in captured] == ["trust_updated"]
    assert captured[0][1]["cause_group"] == "attack"


def test_nothing_is_published_when_no_delta_fires(run_state, captured):
    _attack(run_state, "romano", "Concordo con Ferrari. Basta perdite di tempo.")
    assert captured == []


# --- signal: motion-vote alignment ---------------------------------------

def test_aligned_votes_bond_and_opposed_votes_split(run_state):
    run_state.trust = {}
    deltas = dials.apply_trust_from_votes(run_state, _motion({
        "conti": "yes", "ferrari": "yes", "romano": "no", "greco": "abstain",
    }))
    assert len(deltas) == 3, "abstainers form no pair"
    # Symmetric: both directions move by the same amount.
    assert run_state.trust["conti"]["ferrari"] == pytest.approx(0.10)
    assert run_state.trust["ferrari"]["conti"] == pytest.approx(0.10)
    assert run_state.trust["conti"]["romano"] == pytest.approx(-0.05)
    assert run_state.trust["romano"]["conti"] == pytest.approx(-0.05)
    assert "greco" not in run_state.trust


def test_admin_votes_do_not_enter_the_resident_matrix(run_state):
    run_state.trust = {}
    deltas = dials.apply_trust_from_votes(run_state, _motion({
        "conti": "yes", "ferrari": "yes", "admin": "yes",
    }))
    assert len(deltas) == 1
    assert "admin" not in run_state.trust
    assert not any("admin" in row for row in run_state.trust.values())


# --- signal: emoji reaction ----------------------------------------------

def test_positive_and_negative_reactions_have_different_weights(run_state):
    run_state.trust = {}
    dials.on_reaction(run_state, "greco", _msg("ferrari"), "👍")
    assert run_state.trust["greco"]["ferrari"] == pytest.approx(0.02)
    run_state.trust = {}
    dials.on_reaction(run_state, "greco", _msg("ferrari"), "🙄")
    assert run_state.trust["greco"]["ferrari"] == pytest.approx(-0.04)


def test_neutral_self_and_admin_reactions_produce_nothing(run_state):
    run_state.trust = {}
    assert dials.on_reaction(run_state, "greco", _msg("ferrari"), "🤔") == []
    assert dials.on_reaction(run_state, "greco", _msg("greco"), "👍") == []
    assert dials.on_reaction(run_state, "greco", _msg("admin"), "👍") == []
    assert run_state.trust == {}


# --- signals: forward and DM reply ---------------------------------------

def test_forwarding_someones_message_is_a_small_positive(run_state):
    run_state.trust = {}
    dials.on_forward(run_state, "marchetti", "conti")
    assert run_state.trust["marchetti"]["conti"] == pytest.approx(0.01)
    assert dials.on_forward(run_state, "marchetti", "marchetti") == []
    assert dials.on_forward(run_state, "marchetti", "admin") == []


def test_dm_reply_keeps_the_thread_alive(run_state):
    run_state.trust = {}
    dials.on_dm_reply(run_state, "greco", "marchetti")
    assert run_state.trust["greco"]["marchetti"] == pytest.approx(0.02)
    assert dials.on_dm_reply(run_state, "greco", "admin") == []


# --- clamping ------------------------------------------------------------

def test_trust_is_clamped_to_the_unit_interval(run_state):
    run_state.trust = {}
    for _ in range(40):
        dials.on_message_attack(run_state, "ferrari", "Marchetti, sei un incompetente.")
    assert run_state.trust["ferrari"]["marchetti"] == pytest.approx(-1.0)
    for _ in range(200):
        dials.on_forward(run_state, "ferrari", "marchetti")
    assert run_state.trust["ferrari"]["marchetti"] == pytest.approx(1.0)
