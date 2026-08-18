"""Containment that fires on the wrong things, and chat names that collide.

Three defects, one shape: unanchored matching over Italian prose (RC3 of the
2026-08-01 review). A substring test that ignores what the sentence is *about*
refuses a resident for discussing a broken door phone; a first-match chat
lookup that ignores who is asking hands a resident somebody else's private
thread. Both failures are silent from the model's side — it gets a confident
in-fiction refusal and burns one of three `MAX_TOOL_CALLS_PER_ACTIVATION`
steps, each of which re-sends the whole ~10.4k-char system prompt.

Everything here runs against the real `ToolContext` and the real dispatcher:
the bugs lived in the gap between the predicate and its call sites, so testing
the predicate alone would have missed them.
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from backend import tools
from backend.models import Chat
from backend.tools import ToolContext, _content_rule_violation, dispatch_tool

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ctx(state, agent_id: str) -> ToolContext:
    return ToolContext(
        state=state,
        agent_id=agent_id,
        current_fictional_minutes=state.clock.minutes_since_start + 10,
    )


# ---------------------------------------------------------------------------
# The phone-fiction filter vs. the ambient world-event layer
# ---------------------------------------------------------------------------

# Sentences a resident writes about the building. Every one of these used to
# be refused — "fa le bizze", "problemi tecnici" and "problemi al telefono"
# were subject-free substrings, so they matched whatever was broken. The
# ambient layer hands residents exactly these topics (citofono_rotto,
# ascensore_fermo, corrente_saltata, riscaldamento_tarda, wifi_condominio),
# and CLAUDE.md is explicit that talking about them is the system working.
LEGITIMATE_BUILDING_TALK = [
    "il citofono fa le bizze da settimane, qualcuno ha chiamato il tecnico?",
    "l'ascensore fa le bizze di nuovo, terzo giorno",
    "problemi tecnici con l'ascensore, dice il tecnico",
    "il riscaldamento ha problemi tecnici da giorni, alle sette e' gelo",
    "la corrente fa le bizze nelle scale, le luci sono ancora spente",
    "ho telefonato al tecnico per i problemi del citofono",
    "ho problemi con la caldaia e con la bolletta della luce",
    "la chat va bene ma il citofono fa le bizze",
    "in chat non ci sono problemi, e' il portone che resta aperto",
    "la chat non mi da problemi, e' il riscaldamento il punto",
    # Replayed from run_28c12b45: a resident being sour about somebody ELSE's
    # excuse. Blocked before as a bare "problemi tecnici" hit, which cost the
    # whole activation over a line that makes no claim about their own phone.
    "i problemi tecnici capitano a chi non e' organizzato",
]

# The excuse the filter exists for: the platform never breaks, so blaming it
# is a lie the resident is not allowed to tell.
GENUINE_PHONE_FICTION = [
    "il mio telefono fa le bizze, ho perso la chat",
    "scusate, problemi al telefono, rispondo ora",
    "ho avuto problemi tecnici con la chat ieri sera",
    "la chat fa le bizze, non mi arriva niente",
    "il cellulare ha problemi, riprovo dopo",
    "non vedo la chat da stamattina",
    "ho perso la cronologia, potete ripetere?",
    # Both replayed from run_28c12b45, where the old substring list let them
    # through: neither says "problemi tecnici" or "problemi al telefono"
    # exactly. Anchoring on the noun instead of the phrasing catches them.
    "il telefono mi sta dando problemi assurdi e perdo pezzi di conversazione",
    "scusa, ho avuto problemi con il telefono stamattina e ho perso i tuoi messaggi",
]


@pytest.mark.parametrize("text", LEGITIMATE_BUILDING_TALK)
def test_building_talk_is_not_a_containment_breach(text):
    assert _content_rule_violation(text) is None


@pytest.mark.parametrize("text", GENUINE_PHONE_FICTION)
def test_phone_fiction_is_still_refused(text):
    rule = _content_rule_violation(text)
    assert rule is not None and rule[0] == "phone_fiction"


def test_meeting_rule_is_untouched():
    """Narrowing the phone list must not narrow the other half. The condo
    exists only in chat and that has no ambiguous-subject problem: there is
    no world event about meeting for coffee."""
    assert _content_rule_violation("ci vediamo domani in cortile") == ("meeting", "ci vediamo")
    assert _content_rule_violation("passa da me stasera") == ("meeting", "passa da me")


def test_no_world_event_text_trips_the_filter():
    """Residents quote and paraphrase the day's world event. If the event
    text itself is a containment breach, the day is unusable — check the
    whole file rather than the five the review happened to name."""
    with open(os.path.join(REPO_ROOT, "data", "world_events.json"), encoding="utf-8") as fh:
        events = json.load(fh)
    assert events, "world_events.json is empty — the check below would be vacuous"
    offenders = [(e["id"], _content_rule_violation(e["text"])) for e in events]
    assert [o for o in offenders if o[1] is not None] == []


async def test_citofono_message_actually_sends(run_state):
    """The end-to-end version of the review's reproduction: refused send,
    `ctx.done` set, activation discarded, zero output — for a sentence about
    the door phone."""
    ctx = _ctx(run_state, "greco")
    result = dispatch_tool(ctx, "send_message", {
        "chat_id": "main",
        "text": "il citofono fa le bizze da settimane, qualcuno ha chiamato il tecnico?",
    })
    assert result.startswith("Inviato in")
    assert ctx.done is False
    assert len(ctx.sent_messages_this_activation) == 1


async def test_phone_fiction_send_still_ends_the_activation(run_state):
    """The containment layer is narrowed, not removed: a real breach is still
    refused AND still closes the phone, so the model cannot tack a different
    tail onto the same lie and retry."""
    ctx = _ctx(run_state, "greco")
    result = dispatch_tool(ctx, "send_message", {
        "chat_id": "main",
        "text": "scusate il ritardo, il mio telefono fa le bizze e ho perso i messaggi",
    })
    assert "una bugia" in result
    assert ctx.done is True
    assert ctx.sent_messages_this_activation == []


# ---------------------------------------------------------------------------
# One refusal text, four call sites
# ---------------------------------------------------------------------------

async def test_every_refusal_site_speaks_the_same_italian(run_state):
    """The block was copy-pasted four times; two copies had drifted and three
    carried double-encoded accents. Pin that the sites route through
    `_refusal_text` — the drift is only visible when you compare them, which
    is exactly why nobody noticed the motion copy telling residents something
    different from the message copy."""
    text = "ci vediamo giu' al bar"
    ctx = _ctx(run_state, "greco")
    send = dispatch_tool(ctx, "send_message", {"chat_id": "main", "text": text})
    ctx = _ctx(run_state, "greco")
    dm = dispatch_tool(ctx, "send_dm", {"recipient_id": "romano", "text": text})
    ctx = _ctx(run_state, "greco")
    motion = dispatch_tool(ctx, "propose_motion", {
        "title": "Riunione", "description": text + " per parlarne",
    })
    tail = "Tu non incontri i vicini di persona — solo chat. Metti giù il telefono."
    assert send.endswith(tail)
    assert dm.endswith(tail)
    assert motion.endswith(tail)
    assert send.startswith("Non mandare questo messaggio:")
    assert dm.startswith("Non mandare questo messaggio:")
    assert motion.startswith("Non depositare questa mozione:")


def test_refusal_text_quotes_the_match_not_the_pattern():
    """`_first_blocked` returns what matched, not what matched it. A regex
    quoted back at a resident is exactly the runtime vocabulary this module
    exists to keep out of the fiction."""
    rule = _content_rule_violation("il mio telefono fa le bizze")
    assert rule == ("phone_fiction", "telefono fa le bizze")
    text = tools._refusal_text(*rule, "mandare questo messaggio")
    assert "telefono fa le bizze" in text
    for runtime_junk in ("(?:", "\\b", "re.compile", "[\\s"):
        assert runtime_junk not in text


# ---------------------------------------------------------------------------
# Same-named DMs must not shadow each other
# ---------------------------------------------------------------------------

def _dm(state, a: str, b: str, display_name: str) -> Chat:
    chat = Chat(
        id=f"dm_{a}_{b}",
        kind="dm",
        display_name=display_name,
        member_ids=[a, b],
        created_day=state.clock.day,
    )
    state.chats.append(chat)
    return chat


async def test_same_named_dms_resolve_per_member(run_state):
    """Every DM is created as `f"DM con {other_name}"`, so a run where three
    residents have written to the administrator holds three chats with the
    same display name. 9 of the 24 saved runs carry at least one duplicate.
    First-insertion-order resolution handed every one of them romano's."""
    romano_dm = _dm(run_state, "romano", "admin", "DM con Amministratore")
    greco_dm = _dm(run_state, "greco", "admin", "DM con Amministratore")
    conti_dm = _dm(run_state, "conti", "admin", "DM con Amministratore")

    assert tools._resolve_chat(run_state, "DM con Amministratore", "greco") is greco_dm
    assert tools._resolve_chat(run_state, "DM con Amministratore", "conti") is conti_dm
    assert tools._resolve_chat(run_state, "DM con Amministratore", "romano") is romano_dm
    # Case-insensitivity is a separate stage and needed the same treatment.
    assert tools._resolve_chat(run_state, "dm con amministratore", "greco") is greco_dm
    # No asking agent, or an agent in none of them: the old first-wins order.
    assert tools._resolve_chat(run_state, "DM con Amministratore") is romano_dm
    assert tools._resolve_chat(run_state, "DM con Amministratore", "ferrari") is romano_dm


async def test_send_by_display_name_lands_in_your_own_dm(run_state):
    """The consequence, through the real tool. The model only ever sees
    display names and `send_message`'s schema invites it to address an open
    private chat by name — resolution used to return a chat greco is not in,
    `_is_member` refused, and greco was told a thread on their own phone
    doesn't exist."""
    _dm(run_state, "romano", "admin", "DM con Amministratore")
    greco_dm = _dm(run_state, "greco", "admin", "DM con Amministratore")

    ctx = _ctx(run_state, "greco")
    result = dispatch_tool(ctx, "send_message", {
        "chat_id": "DM con Amministratore",
        "text": "sulle spese di ieri avrei una domanda",
    })
    assert result.startswith("Inviato in")
    assert [m.chat_id for m in ctx.sent_messages_this_activation] == [greco_dm.id]


async def test_main_chat_and_aliases_still_resolve(run_state):
    """Stage order is a tie-break inside a stage, not a re-ranking across
    them — an exact id must still beat a display name."""
    main = next(c for c in run_state.chats if c.kind == "main")
    assert tools._resolve_chat(run_state, "main", "greco") is main
    assert tools._resolve_chat(run_state, "gruppo", "greco") is main
    assert tools._resolve_chat(run_state, main.display_name, "greco") is main
    assert tools._resolve_chat(run_state, "chat che non esiste", "greco") is None
    assert tools._resolve_chat(run_state, "", "greco") is None


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------

def test_no_mojibake_in_backend_sources():
    """Seven agent-facing refusal strings had their accents double-encoded
    through latin-1, plus a stray non-breaking space left over from one of
    them. The model reads these strings, so they are not cosmetic.

    U+00C3 and U+00E2 are the lead bytes of every UTF-8-read-as-latin-1
    mangling of an Italian accent or an em dash; U+FFFD is a decode that
    already gave up; U+00A0 is the tail of a mangled "a-grave" and has no
    business in Italian prose either. Named by codepoint on purpose: spelled
    out, they would be indistinguishable from the bug and this file would
    fail its own check.
    """
    suspects = {
        chr(0x00C3): "UTF-8 read as latin-1 (accented vowel)",
        chr(0x00E2): "UTF-8 read as latin-1 (em dash)",
        chr(0xFFFD): "replacement character",
        chr(0x00A0): "non-breaking space",
    }
    offenders = []
    for path in glob.glob(os.path.join(REPO_ROOT, "backend", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for ch, why in suspects.items():
                    if ch in line:
                        offenders.append(os.path.basename(path) + ":" + str(lineno) + " " + why)
    assert offenders == []
