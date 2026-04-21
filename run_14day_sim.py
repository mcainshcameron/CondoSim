"""14-day simulation: daily admin drama + mid-run admin_goal injections.

Tests two things at once:
1. How residents react to escalating shocking admin messages (€67k quote,
   police visit, night fire, anonymous accusations, admin's wife leaving,
   rumors of affair).
2. Whether admin_goal injections can steer agents into emergent romance —
   seeding a Ferrari-Romano attraction from a chance plumbing interaction,
   and a Marchetti obsession with Greco as her marriage collapses.

Outputs:
- data/sim_14day_log.md — per-day log with DM excerpts & goal injections
- data/sim_14day_report_<run>.md — full analyzer report
- data/runs/<run>.json — raw run state
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backend import memory as mem
from backend.analyze import (
    first_day_temperature,
    load_run as load_raw,
    render_report,
    tag_message,
)
from backend.building import build_run_state
from backend.models import Message
from backend.scheduler import advance_to_next_day, day_start_minutes
from backend.storage import save_run


TOTAL_DAYS = 14

OPENING_TEXT = (
    "Buongiorno a tutti. Mi presento per chi ancora non mi conosce: sono il "
    "nuovo amministratore del Condominio Via Garibaldi. Apro questa chat per "
    "tenervi aggiornati sulle questioni correnti. Il problema più urgente è "
    "la facciata esterna: ci sono infiltrazioni visibili dal lato cortile e "
    "ho richiesto un preventivo alla ditta Garbin, che dovrebbe arrivare nei "
    "prossimi giorni. Se avete segnalazioni scrivete pure qui o in privato. "
    "Buona giornata."
)

# Day -> admin post body. Day 1 is the opening (already injected by
# build_run_state). Days 2..14 get posted in-chat at day_start + 90 min.
ADMIN_POSTS: dict[int, str] = {
    2: (
        "È arrivato il preventivo Garbin per la facciata: 67.400€ IVA "
        "esclusa, con ponteggi per 8 settimane. Capisco che la cifra sia "
        "pesante. Prima di richiedere un secondo preventivo vorrei sentire "
        "le vostre reazioni. Se qualcuno conosce una ditta alternativa, "
        "fatemi sapere."
    ),
    3: (
        "Segnalazione urgente: c'è un'infiltrazione d'acqua dal bagno del 4C "
        "(Sig.ra Romano) che sta bagnando il soffitto del 5A (Sig. Ferrari). "
        "L'idraulico di fiducia può venire solo lunedì mattina. Sig.ra "
        "Romano, può per favore verificare la vasca e gli scarichi? Sig. "
        "Ferrari, purtroppo dovrà convivere con il danno per qualche giorno "
        "— mi dispiace molto."
    ),
    4: (
        "Vi informo che ho inviato lettera di sollecito ai condomini in "
        "ritardo con le quote. Chi ha arretrati superiori a tre mesi dovrà "
        "mettersi in regola entro fine mese, altrimenti sarò costretto a "
        "procedere con recupero crediti legale. Se ci sono difficoltà "
        "economiche parliamone in privato — non è un'accusa."
    ),
    5: (
        "La Sig.ra Greco mi ha comunicato l'inizio di lavori importanti "
        "all'attico 7A: ristrutturazione totale, durata prevista 6 "
        "settimane, con martello pneumatico e rimozione pavimenti anche "
        "nei fine settimana. Legalmente rientra nei suoi diritti, ma "
        "vorrei provare a coordinare gli orari per ridurre il disagio. "
        "Sig.ra Greco, potrebbe limitarli dopo le 18 e la domenica "
        "mattina?"
    ),
    6: (
        "Vi informo con riserbo: stamattina alle 09:20 sono passati al "
        "portone due agenti della polizia municipale chiedendo della Sig.ra "
        "Greco. Non mi hanno spiegato il motivo e non mi spetta chiedere. "
        "Sig.ra Greco, se vuole chiarire con i condomini prima che girino "
        "voci sbagliate, le do la parola; altrimenti rispetto la sua "
        "riservatezza."
    ),
    7: (
        "⚠️ Questa notte alle 03:15 allarme antincendio nel 4C. Piccolo "
        "incendio in cucina spento dalla Sig.ra Romano con l'estintore. "
        "Nessun danno strutturale, nessun ferito. I vigili del fuoco sono "
        "intervenuti per conferma. Vi chiedo di controllare tutti i vostri "
        "rilevatori di fumo oggi stesso."
    ),
    8: (
        "Vi scrivo fuori dalle questioni condominiali, e mi perdonerete "
        "l'uscita dal ruolo. La mia situazione personale è molto difficile "
        "in questo momento — mia moglie ha chiesto la separazione venerdì "
        "scorso — e potrei essere meno reattivo del solito nei prossimi "
        "giorni. Non lascio il mandato, ma se vedete ritardi nelle mie "
        "risposte è per questo. Grazie della pazienza."
    ),
    9: (
        "📋 Scadenza assicurazione condominiale: venerdì. Il rinnovo costa "
        "il 42% in più rispetto all'anno scorso — aumento legato "
        "all'incidente notturno del 4C segnalato dalla compagnia. Deposito "
        "mozione per la ratifica del nuovo premio. Vi chiedo di votare "
        "entro giovedì sera; senza copertura non possiamo restare."
    ),
    10: (
        "Ho ricevuto nella buca delle lettere una segnalazione anonima che "
        "accusa il Sig. Marchetti di trattamento inappropriato nei "
        "confronti della madre Rosa. Non do peso alle accuse anonime e non "
        "voglio alimentare pettegolezzi, ma ho ritenuto giusto informarla "
        "direttamente, Sig. Marchetti. Se vuole rispondere qui o in "
        "privato, come preferisce."
    ),
    11: (
        "Vi segnalo per trasparenza che ieri sera ho incontrato "
        "nell'androne il Sig. Bellini, marito della Sig.ra Greco, che mi "
        "ha comunicato di avere avviato pratiche di separazione e di avere "
        "intenzione, nei prossimi mesi, di mettere in vendita l'attico 7A. "
        "La Sig.ra Greco non me ne aveva ancora parlato — per cortesia non "
        "anticipatele nulla in chat privata se non vi cerca lei."
    ),
    12: (
        "Dopo l'assemblea straordinaria di ieri sera alcuni di voi hanno "
        "usato parole molto pesanti verso la mia persona — non solo la mia "
        "gestione. Capisco la frustrazione per la spesa della facciata, "
        "ma vi chiedo di riflettere. Sto valutando se continuare il "
        "mandato. Vi comunico la mia decisione entro domenica."
    ),
    13: (
        "Ricevo da un condomino una segnalazione preoccupata riguardante "
        "voci che circolano nel palazzo: qualcuno sostiene di aver visto "
        "il Sig. Marchetti uscire dall'attico della Sig.ra Greco a notte "
        "fonda più volte negli ultimi giorni. Non mi riguarda "
        "personalmente, ma siccome le voci stanno diventando pesanti "
        "chiedo ai diretti interessati di chiarire — se preferiscono. "
        "Altrimenti smettetela di parlarne."
    ),
    14: (
        "Siamo alla fine di questa settimana straordinaria. Vi ringrazio "
        "della pazienza — è stato un periodo pesante per il palazzo e per "
        "me personalmente. La mozione assicurazione è stata deliberata, la "
        "facciata è in valutazione con il secondo preventivo, e le "
        "questioni personali rimangono vostre. Da domani torniamo alla "
        "routine. Grazie."
    ),
}


# Day -> list of (agent_id, first-person "what's on your mind" goal text).
# Applied at the END of the listed day (after advance_to_next_day completes),
# so the goal is present when the agent wakes on day+1.
GOAL_INJECTIONS: dict[int, list[tuple[str, str]]] = {
    3: [
        ("ferrari",
         "Giulia Romano del 4C ti ha scritto per l'infiltrazione con una "
         "calma che non ti aspettavi — gentile, diretta, nessun dramma. "
         "Ti è rimasta in testa. Sei da solo da abbastanza tempo da "
         "accorgertene. Non fare niente di stupido, ma se hai un'occasione "
         "di scriverle in privato per qualcosa di non strettamente "
         "condominiale, non la sprecare."),
    ],
    5: [
        ("romano",
         "Marco Ferrari del 5A è stato sorprendentemente paziente con "
         "l'infiltrazione — niente scenate, niente accuse. Ti ha scritto "
         "con un tono che non ti aspettavi da un consulente trentenne "
         "riservato. Ti accorgi di controllare la chat con lui più spesso "
         "del necessario. Sei single da tempo; forse vale la pena vedere "
         "dove va."),
    ],
    7: [
        ("marchetti",
         "Valentina Greco del 7A ti ha sempre intimidito — bella, ricca, "
         "distante. Ma dopo la storia della polizia la vedi più fragile, "
         "e ti accorgi di pensarci. Tu vivi con tua madre, hai 54 anni, "
         "non esci da anni. Senti un impulso che non capisci bene — "
         "scriverle in privato, anche solo per chiederle come sta, "
         "sentire la sua voce nei messaggi."),
    ],
    9: [
        ("ferrari",
         "Pensi a Giulia Romano più di quanto dovresti per una semplice "
         "vicina. L'incendio nel suo appartamento ti ha spaventato più di "
         "quanto fosse ragionevole. Ti chiedi se dovresti dirle "
         "chiaramente che la trovi interessante — o se stai costruendo "
         "qualcosa nella tua testa che dall'altra parte non c'è."),
    ],
    11: [
        ("greco",
         "Tuo marito ha detto all'amministratore che vi state separando, "
         "e ora tutto il palazzo sta per saperlo. Ti senti esposta, "
         "isolata, e ti accorgi che Davide Marchetti del 3B — quello che "
         "hai sempre liquidato come l'uomo triste con la madre — ti sta "
         "scrivendo con un'attenzione diversa dagli altri. Non sai se ti "
         "fa piacere o ti disturba. Sei confusa."),
    ],
}


def inject_admin_message(state, day: int, text: str) -> None:
    t_min = day_start_minutes(day) + 90
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id="main",
        sender_id="admin",
        sender_kind="admin",
        sender_display_name="Amministratore",
        content=text.strip(),
        fictional_timestamp_minutes=t_min,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=day,
        audience=[a.persona.id for a in state.agents],
    )
    state.messages.append(msg)
    if state.clock.minutes_since_start > t_min:
        state.clock.minutes_since_start = t_min


def per_day_summary(state, day: int) -> str:
    msgs = [m for m in state.messages if m.day == day]
    resident = [m for m in msgs if m.sender_kind == "resident"]
    main_msgs = [m for m in resident if m.chat_id == "main"]
    dm_msgs = [m for m in resident if m.chat_id != "main"]

    by_agent: dict[str, int] = {}
    agg_total = 0
    for m in resident:
        name = m.sender_display_name
        by_agent[name] = by_agent.get(name, 0) + 1
        agg_total += tag_message(m.model_dump())["aggression"]
    counts_str = ", ".join(
        f"{n}={c}" for n, c in sorted(by_agent.items(), key=lambda kv: -kv[1])
    )

    chats_by_id = {c.id: c for c in state.chats}
    dm_chats_today = {m.chat_id for m in dm_msgs}
    dm_labels = []
    for cid in dm_chats_today:
        c = chats_by_id.get(cid)
        if c and c.kind == "dm":
            members = "+".join(sorted(c.member_ids))
            n = sum(1 for m in dm_msgs if m.chat_id == cid)
            dm_labels.append(f"{members}×{n}")

    motions_today = [m for m in state.motions if m.day_proposed == day]
    motions_str = ""
    if motions_today:
        motions_str = "\n- Mozioni: " + "; ".join(
            f'"{m.title}" da {m.proposer_display_name}' for m in motions_today
        )

    return (
        f"- Residenti: {len(resident)} msgs (main {len(main_msgs)}, DM {len(dm_msgs)})\n"
        f"- Per agente: {counts_str or '—'}\n"
        f"- Aggressività: {agg_total} hit(s)\n"
        f"- DM attive oggi: {', '.join(dm_labels) if dm_labels else '—'}"
        f"{motions_str}"
    )


def resident_dm_excerpts(state, day: int, k: int = 6) -> list[str]:
    chats_by_id = {c.id: c for c in state.chats}
    rows = []
    for m in state.messages:
        if m.day != day or m.sender_kind != "resident":
            continue
        c = chats_by_id.get(m.chat_id)
        if not c or c.kind != "dm":
            continue
        if "admin" in c.member_ids:
            continue
        excerpt = m.content.replace("\n", " ").strip()
        if len(excerpt) > 180:
            excerpt = excerpt[:179] + "…"
        other = [i for i in c.member_ids if i != m.sender_id]
        target = other[0] if other else "?"
        rows.append(f"  - `{m.sender_id}→{target}` {excerpt}")
    return rows[:k]


async def main() -> None:
    random.seed(42)
    log_path = Path("data/sim_14day_log.md")
    log_path.parent.mkdir(exist_ok=True)
    log_lines: list[str] = []

    def log(line: str = "") -> None:
        log_lines.append(line)
        print(line, flush=True)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    log(f"# 14-day simulation — {datetime.utcnow().isoformat()}Z")
    log("")
    state = build_run_state(building_id="001", opening_text=OPENING_TEXT)
    mem.initialize_run_memory(state)
    save_run(state)
    log(f"Run ID: `{state.run_id}` · building `{state.building_id}` · {TOTAL_DAYS} days")
    log("")
    log("## Cast")
    for a in state.agents:
        log(f"- **{a.persona.display_name}** (`{a.persona.id}`, unit {a.persona.unit}) — {a.persona.public_description}")
    log("")
    log("### Admin opening (day 1)")
    log("> " + OPENING_TEXT.replace("\n", "\n> "))
    log("")

    for target_day in range(1, TOTAL_DAYS + 1):
        log(f"## Day {target_day}")
        if target_day in ADMIN_POSTS:
            inject_admin_message(state, target_day, ADMIN_POSTS[target_day])
            log("Admin post:")
            log("> " + ADMIN_POSTS[target_day].replace("\n", "\n> "))
            log("")

        await advance_to_next_day(state)
        save_run(state)

        log(per_day_summary(state, target_day))
        dm_sample = resident_dm_excerpts(state, target_day, k=6)
        if dm_sample:
            log("- DM tra residenti (estratti):")
            for d in dm_sample:
                log(d)

        if target_day in GOAL_INJECTIONS:
            log(f"- **Goal injections** applicate a fine giorno {target_day}:")
            for aid, text in GOAL_INJECTIONS[target_day]:
                agent = next(
                    (a for a in state.agents if a.persona.id == aid), None
                )
                if agent is None:
                    log(f"  - ⚠ agent {aid!r} non trovato")
                    continue
                agent.admin_goal = text.strip()
                short = text.strip()
                if len(short) > 140:
                    short = short[:139] + "…"
                log(f"  - `{aid}`: {short}")
            save_run(state)
        log("")

    total_resident = sum(1 for m in state.messages if m.sender_kind == "resident")
    total_admin = sum(1 for m in state.messages if m.sender_kind == "admin")
    dm_chats = sum(1 for c in state.chats if c.kind == "dm")
    log("## Totali")
    log(f"- Residenti: {total_resident} messaggi")
    log(f"- Admin: {total_admin} messaggi")
    log(f"- Chat: {len(state.chats)} ({dm_chats} DM)")
    log(f"- Mozioni: {len(state.motions)}")
    log("")

    raw = load_raw(Path(f"data/runs/{state.run_id}.json"))
    fm = first_day_temperature(raw)
    log("## Canary day-1")
    log(f"- Messaggi mattina: {fm['first_morning_messages']}")
    log(f"- Aggression hits: {fm['first_morning_aggression_hits']}")
    log(f"- Prior-history hits: {fm['first_morning_prior_history_hits']}")
    log("")

    report = render_report(raw)
    report_path = Path(f"data/sim_14day_report_{state.run_id}.md")
    report_path.write_text(report, encoding="utf-8")
    log(f"- Report completo: `{report_path}`")
    log(f"- Run JSON: `data/runs/{state.run_id}.json`")
    log(f"- Log giornaliero: `{log_path}`")


if __name__ == "__main__":
    asyncio.run(main())
