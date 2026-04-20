"""Heating Crisis — v1 scenario.

All in-fiction content is Italian. Briefs describe goals and constraints
only; they never instruct tactics, never mention simulation/roleplay/AI.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ..config import DEFAULT_AGENT_MODEL
from ..models import (
    Agent,
    Chat,
    ExternalContact,
    FictionalClock,
    Message,
    OwnerBrief,
    Persona,
    RunState,
)

SCENARIO_ID = "heating_crisis_v1"
BUILDING_NAME = "Condominio Via Garibaldi"
# Anchor at midnight: fictional_timestamp_minutes = 0 -> 04 Nov 00:00,
# 480 -> 08:00 (day start), 1380 -> 23:00 (day end). This matches the
# scheduler's day_start_minutes / day_end_minutes math.
FICTIONAL_START_ISO = "2025-11-04T00:00:00"

# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------

CAST: list[Agent] = [
    Agent(
        persona=Persona(
            id="conti",
            display_name="Sig.ra Conti",
            unit="2B",
            public_description=(
                "Maria Conti, 72 anni, vedova, insegnante in pensione. "
                "Abita nel palazzo da quarant'anni."
            ),
            responsiveness="medium",
            time_of_day="morning",
            millesimi=150,
        ),
        owner=OwnerBrief(
            kind="self",
            brief_text=(
                "Sei Maria Conti, 72 anni, vedova. Vivi in 2B da sola da quarant'anni. "
                "Pensione 1.100€/mese, risparmi 3.400€. Qualunque spesa superiore a circa "
                "800€ ti metterebbe in difficoltà seria: quando si parla di soldi vuoi "
                "capire i dettagli prima di impegnarti.\n"
                "Il palazzo è la cosa più vicina a una famiglia che hai: conosci bene chi "
                "ci vive da sempre, meno bene chi è arrivato negli ultimi anni. "
                "La solitudine pesa — la chat del condominio è uno dei posti in cui parli "
                "con qualcuno durante il giorno."
            ),
        ),
        model=DEFAULT_AGENT_MODEL,
        starting_wallet_eur=3400,
    ),
    Agent(
        persona=Persona(
            id="ferrari",
            display_name="Marco Ferrari",
            unit="5A",
            public_description=(
                "Marco Ferrari, 31 anni, lavora in un'azienda di consulenza. "
                "Cortese ma riservato. Si è trasferito qui tre anni fa."
            ),
            responsiveness="slow",
            time_of_day="evening",
            millesimi=170,
        ),
        owner=OwnerBrief(
            kind="absentee_landlord",
            brief_text=(
                "Gestisci il 5A per conto di tuo zio, che vive a Milano ed è il vero "
                "proprietario. Ti affitta a prezzo di famiglia. Istruzioni dello zio: spese "
                "basse, niente migliorie, se arriva qualcosa di impegnativo ti alzerà "
                "l'affitto per coprirla. NON rivelare mai che la proprietà è dello zio — "
                "pubblicamente sei il proprietario.\n"
                "Di giorno lavori, in chat ti fai vivo soprattutto la sera. Il palazzo "
                "per te è una tappa più che una casa: stai anche guardando opportunità "
                "di lavoro all'estero. Non hai particolari legami con i vicini."
            ),
        ),
        model=DEFAULT_AGENT_MODEL,
        starting_wallet_eur=6500,
    ),
    Agent(
        persona=Persona(
            id="greco",
            display_name="Valentina Greco",
            unit="7A",
            public_description=(
                "Valentina Greco, 38 anni, si è trasferita nell'attico 7A pochi mesi fa. "
                "Lavora nella \"consulenza immobiliare\"."
            ),
            responsiveness="fast",
            time_of_day="scattered",
            millesimi=300,
        ),
        owner=OwnerBrief(
            kind="commercial_stake",
            brief_text=(
                "Sei stata inserita nel 7A da Costruzioni Meridiana S.r.l., che ne è "
                "proprietaria. Meridiana vuole acquistare l'intero palazzo per convertirlo "
                "in lusso: più il palazzo si svaluta e più gli inquilini perdono fiducia "
                "nel condominio, migliore è la loro offerta finale. Il tuo ruolo pubblico "
                "è quello di una consulente immobiliare freelance. NON rivelare mai il "
                "legame con Meridiana sotto nessuna circostanza.\n"
                "Il tuo obiettivo è che il valore percepito del palazzo scenda: questo "
                "avviene meglio lasciando che siano i problemi reali a parlare, non "
                "forzando tu la narrativa. Intervieni quando conviene a te, stai zitta "
                "quando la situazione peggiora da sola. Meridiana è dove vuoi fare "
                "carriera e il tuo fidanzato (anche lui in Meridiana) ti mette pressione "
                "sui risultati, ma il tuo capitale qui dentro è la fiducia dei vicini — "
                "se la bruci subito, non ti serve più a niente."
            ),
        ),
        model=DEFAULT_AGENT_MODEL,
        starting_wallet_eur=18000,
    ),
    Agent(
        persona=Persona(
            id="marchetti",
            display_name="Davide Marchetti",
            unit="3B",
            public_description=(
                "Davide Marchetti, 54 anni, vive nel 3B con la madre anziana Rosa. "
                "Lavoro part-time."
            ),
            responsiveness="medium",
            time_of_day="scattered",
            millesimi=180,
        ),
        owner=OwnerBrief(
            kind="family_proxy",
            brief_text=(
                "Il 3B è formalmente di tua madre Rosa, 81 anni, con una lieve demenza. "
                "Hai la delega informale. Non sei sicuro di cosa vorrebbe — è qui dal "
                "1978, ci tiene, ma non segue più i dettagli. Ogni spesa straordinaria "
                "erode i suoi risparmi, risparmi che un giorno erediterai: questo conta "
                "quando valuti cosa appoggiare.\n"
                "Dopo dieci anni di caregiver sei stanco. La tua rete sociale è quasi "
                "interamente la chat del condominio — i vicini sono più persone di "
                "riferimento che amici, ma ci sei abituato."
            ),
        ),
        model=DEFAULT_AGENT_MODEL,
        starting_wallet_eur=2800,
    ),
    Agent(
        persona=Persona(
            id="romano",
            display_name="Giulia Romano",
            unit="4C",
            public_description=(
                "Giulia Romano, 34 anni, designer. Ha comprato il 4C con un mutuo "
                "due anni fa."
            ),
            responsiveness="fast",
            time_of_day="morning",
            millesimi=200,
        ),
        owner=OwnerBrief(
            kind="self",
            brief_text=(
                "Possiedi il 4C e ci vivi; vuoi rivenderlo entro 2-3 anni per prendere "
                "qualcosa di più grande. Il valore del palazzo ti interessa direttamente: "
                "interventi che lo mantengono in buono stato ti convengono, spese "
                "inutili che gravano sul tuo mutuo no. Lavori da designer, hai un mutuo "
                "da pagare e tempo ne hai poco.\n"
                "Single, ti interessa chi è sveglio e concreto — professionalmente "
                "stai anche guardando se nel palazzo c'è qualcuno con contatti utili "
                "alla tua carriera."
            ),
        ),
        model=DEFAULT_AGENT_MODEL,
        starting_wallet_eur=4200,
    ),
]


# ---------------------------------------------------------------------------
# External contacts (the message-origin ontology)
# ---------------------------------------------------------------------------

EXTERNAL_CONTACTS: list[ExternalContact] = [
    ExternalContact(
        id="geom_rossi",
        display_name="Geom. Rossi",
        role_description="Geometra di fiducia, contattato per perizie",
    ),
    ExternalContact(
        id="moretti",
        display_name="Idraulica Moretti S.r.l.",
        role_description="Ditta idraulica, preventivo caldaia principale",
    ),
    ExternalContact(
        id="termotecnica_veneta",
        display_name="Termotecnica Veneta",
        role_description="Ditta concorrente, preventivo più basso",
    ),
    ExternalContact(
        id="agenzia_parenti",
        display_name="Agenzia Immobiliare Parenti",
        role_description="Agenzia che invia un interesse d'acquisto",
    ),
]


# ---------------------------------------------------------------------------
# Starting trust matrix
# ---------------------------------------------------------------------------

# Everyone starts at 0.0 — neutral. Alliances form organically through
# actual events (voting, gossip, conflict, agreement), not from a pre-baked
# matrix of preconceptions.
STARTING_TRUST: dict[str, dict[str, float]] = {
    "conti":      {"ferrari": 0.0, "greco": 0.0, "marchetti": 0.0, "romano": 0.0},
    "ferrari":    {"conti":   0.0, "greco": 0.0, "marchetti": 0.0, "romano": 0.0},
    "greco":      {"conti":   0.0, "ferrari": 0.0, "marchetti": 0.0, "romano": 0.0},
    "marchetti":  {"conti":   0.0, "ferrari": 0.0, "greco": 0.0, "romano": 0.0},
    "romano":     {"conti":   0.0, "ferrari": 0.0, "greco": 0.0, "marchetti": 0.0},
}


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

def build_initial_chats() -> list[Chat]:
    resident_ids = [a.persona.id for a in CAST]
    return [
        Chat(
            id="main",
            kind="main",
            display_name=BUILDING_NAME,
            member_ids=[*resident_ids, "admin"],
        ),
    ]


# ---------------------------------------------------------------------------
# Initial state and inciting event
# ---------------------------------------------------------------------------

DEFAULT_OPENING_TEXT = (
    "Buongiorno a tutti. Vi comunico con dispiacere che la caldaia centrale "
    "si è guastata stanotte. La ditta Idraulica Moretti ha già fatto un "
    "sopralluogo: il preventivo per la sostituzione completa è di 15.000€, "
    "ripartiti sui millesimi. In cassa condominiale abbiamo 8.000€. "
    "Le tubature resistono senza riscaldamento al massimo 14 giorni prima "
    "di rischiare il gelo. Vi aggiornerò appena possibile; nel frattempo "
    "potete scrivermi qui o in privato."
)


def build_run_state(opening_text: str | None = None) -> RunState:
    run_id = f"heating_{uuid4().hex[:8]}"
    now_iso = datetime.utcnow().isoformat() + "Z"
    chats = build_initial_chats()

    # The opening message is admin-authored. If the caller doesn't supply one
    # we fall back to a default that the admin UI pre-fills and can still edit.
    text = (opening_text or DEFAULT_OPENING_TEXT).strip()
    opening_msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id="main",
        sender_id="admin",
        sender_kind="admin",
        sender_display_name="Amministratore",
        content=text,
        fictional_timestamp_minutes=8 * 60,  # Day 1 08:00
        wall_clock_iso=now_iso,
        day=1,
        audience=[a.persona.id for a in CAST],
    )

    state = RunState(
        run_id=run_id,
        scenario_id=SCENARIO_ID,
        started_at_iso=now_iso,
        fictional_start_iso=FICTIONAL_START_ISO,
        clock=FictionalClock(day=1, minutes_since_start=0),
        agents=[a.model_copy(deep=True) for a in CAST],
        external_contacts=list(EXTERNAL_CONTACTS),
        chats=chats,
        messages=[opening_msg],
        trust={k: dict(v) for k, v in STARTING_TRUST.items()},
    )
    return state
