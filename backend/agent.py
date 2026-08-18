"""Agent activation: assemble prompt, loop on tool calls, commit to state."""
from __future__ import annotations

import json

from . import atmosphere, building, memory, timeline
from .config import (
    AGENT_MAX_TOKENS,
    AGENT_TEMPERATURE,
    DM_CONTEXT,
    MAIN_CHAT_CONTEXT,
    MAX_TOOL_CALLS_PER_ACTIVATION,
    MEMORY_DAYS_IN_PROMPT,
)
from .events import bus
from .llm import BudgetExceeded, OpenRouterError, complete
from .logging_utils import log, log_error
from .models import Agent, Message, RunState
from .tools import TOOL_SCHEMAS, ToolContext, dispatch_tool, contains_forbidden


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

# Caller-controlled text is length-bounded before it reaches a prompt.
#
# The admin goal is the expensive one: it is inlined in BOTH the system
# prompt and the notification prompt, which ship in the SAME request, so it
# is paid for twice on every activation for the rest of the run. Unbounded, a
# 100 KB goal is ~50k input tokens per wake-up against a ~$0.000064 baseline
# (config.py) — a ~100x multiplier that trips RUN_COST_CAP_USD in ~100
# activations and reports nothing but `cost_cap_exceeded`, with no hint of
# the cause. Message bodies inlined by the digest have the same shape, one
# order of magnitude smaller.
#
# The request models bound these at the API boundary; this is the defensive
# half, so a run created by a script, an older payload or a hand-edited
# snapshot cannot break the "prompt size is bounded, not growing" invariant
# either. Limits sit above anything well-behaved — the longest message across
# the 24 saved runs is 546 chars, so 600 leaves every real transcript
# byte-identical.
ADMIN_GOAL_PROMPT_LIMIT = 2000
DIGEST_BODY_LIMIT = 600


def _clamp(text: str, limit: int) -> str:
    """Trim `text` and cut it to `limit` characters, marking the cut."""
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


async def build_system_prompt(state: RunState, agent: Agent) -> str:
    """Assemble the system prompt from SOUL + MEMORY plus minimal rules.

    SOUL and MEMORY are framed as the agent's own private notebook rather
    than external instructions. Everything else (rules about tone, history,
    relationship priors) is deliberately absent — the files carry the load.
    """
    p = agent.persona
    soul = memory.read_soul(state, p.id)
    # Window the diary: the seed plus recent days. Keeps the prompt (and
    # therefore per-activation cost) flat as a run gets long.
    memory_text = memory.window_memory(
        await memory.read_memory(state, p.id), MEMORY_DAYS_IN_PROMPT
    )

    # The building is data, not code. Hardcoding "Condominio Via Garibaldi, a
    # Milano" here contradicted the authoring contract (write the four files,
    # change no Python) and, worse, could drift from what the agent's phone
    # actually shows: `_resolve_chat` matches on the chat's display name, so
    # renaming the main chat left the resident being told to write in a group
    # that isn't on their phone — and burning one of three tool steps finding
    # that out. The run's own chat list is the authority; building.json is
    # only the fallback for a state with no main chat at all.
    config_name, city = building.building_scene(state.building_id)
    group_name = next(
        (c.display_name for c in state.chats if c.kind == "main"), ""
    ) or config_name
    where = f"{p.unit} del {group_name}" if group_name else p.unit
    if city:
        where += f", a {city}"

    lines = [
        f"Sei {p.display_name}. Vivi in {where}.",
        "",
        "Quello che segue sono i tuoi appunti su chi sei. Li hai scritti tu, "
        "tempo fa, per ricordarti come sei fatto/a:",
        "",
        soul,
        "",
        "E questo è il tuo taccuino privato: chi sei nei fatti, chi sono i tuoi "
        "vicini come li conosci finora, e gli appunti che hai preso giorno per "
        "giorno:",
        "",
        memory_text,
    ]

    # Admin-authored goal is framed as an organic new preoccupation on the
    # agent's mind, never as an external directive. Clamped: see
    # ADMIN_GOAL_PROMPT_LIMIT — this text is re-sent twice per activation for
    # the whole run.
    extra = _clamp(getattr(agent, "admin_goal", "") or "", ADMIN_GOAL_PROMPT_LIMIT)
    if extra:
        lines.extend([
            "",
            "In più, in questi giorni ti gira in testa un'altra cosa:",
            extra,
        ])

    lines.extend([
        "",
        "Il tuo telefono:",
        f"- Hai il gruppo condominiale \"{group_name}\" dove scrivono tutti i vicini e l'amministratore, più le chat private con i singoli vicini o con l'amministratore.",
        "- Per scrivere, leggere, prenderti un appunto, usa le azioni del telefono. Parlare soltanto tra sé e sé non mette niente in chat — per dire qualcosa devi usare il telefono.",
        "- Se c'è qualcosa da decidere formalmente (una spesa, una regola comune, cambiare amministratore), puoi depositare una mozione dal telefono e farla votare. Ma solo se vale davvero la pena.",
        "- Se un vicino dice una cosa ovvia, scontata o con cui sei semplicemente d'accordo, puoi usare una reazione emoji (👍 ❤️ 😂 🙄 😡) invece di scrivere un altro messaggio. Risparmi parole e dici la stessa cosa.",
        "- Quando hai finito di guardare le notifiche e di rispondere, metti giù il telefono.",
        "",
        "Il tuo mondo:",
        "- Tu interagisci con i tuoi vicini e con l'amministratore **solo tramite il telefono** — chat di gruppo e messaggi privati. Non vi vedete mai di persona.",
        "- Non proporre caffè, incontri, appuntamenti. Non scrivere \"passa da me\", \"vediamoci\", \"ci vediamo alle...\", \"arrivo subito\", \"ti aspetto\", \"scendo\". Tutto quello che vuoi dire o chiedere, lo scrivi in chat.",
        "- Le assemblee condominiali esistono solo come cornice per votare le mozioni depositate dal telefono. Non ti ci siedi davvero.",
        "- L'amministratore è la voce ufficiale del palazzo: quando riporta un fatto (un preventivo, una scadenza, un evento accaduto, una segnalazione ricevuta, la visita della polizia, un incendio, una separazione di qualcuno, una lettera anonima), quello che racconta è **vero nel tuo mondo** — l'evento è successo come lo descrive, le cifre sono quelle, le persone citate esistono davvero. Puoi criticare le sue scelte, la sua gestione, il suo tono, perfino la sua persona — quello è legittimo. Ma non metti in dubbio che i fatti che riporta siano reali: se dice che è passata la polizia, è passata; se dice €67.400, sono €67.400; se dice che è scoppiato un incendio alle 03:15, è scoppiato.",
        # Counterpart to the rule above. Without it the residents invent shared
        # building facts and then reason from them as established: a 5-day eval
        # produced a "new lift" that had only ever been under maintenance, and
        # an electricity bill with invented figures (540 vs 380) that nobody
        # had received. One resident's invention becomes the group's premise
        # within two turns, and the admin's actual storyline gets buried.
        "- Al contrario, **i fatti nuovi sul palazzo non li inventi tu**. Non annunciare eventi collettivi che nessuno ti ha detto (una bolletta arrivata, un lavoro finito, un preventivo nuovo, una decisione presa, cifre precise che nessuno ha dato). Puoi raccontare quello che hai notato **tu**, nel tuo appartamento o sulle tue scale, e puoi supporre, sospettare, esagerare — ma allora si sente che è una tua impressione (\"mi sa che…\", \"secondo me…\"), non un annuncio. Se una cosa non l'ha detta l'amministratore e non l'hai vista tu, non è ancora successa.",
        "",
        "Come scrivi:",
        "- Italiano colloquiale, in chat WhatsApp. **Lunghezza vera di WhatsApp**: la maggior parte dei messaggi sono cortissimi. Esempi realistici di quello che scrivi:",
        "    • \"ok\"",
        "    • \"👍\"",
        "    • \"concordo\"",
        "    • \"mah\"",
        "    • \"ma quando arrivano questi bilanci?\"",
        "    • \"non mi convince\"",
        "    • \"Marchetti hai ragione.\"",
        "    • \"io lunedì non ci sono\"",
        "  Tre righe sono il MASSIMO e si usano solo quando davvero c'è qualcosa di importante da dire. I pipponi non sono realistici.",
        "- Non recitare, non spiegare quello che fai, non fare meta-commenti. Scrivi e basta.",
        "- Se non hai niente da dire, non scrivere — metti giù il telefono e basta.",
        "- Il tuo telefono e le chat funzionano sempre. Non scrivere mai frasi tipo \"non vedo la chat\", \"la chat è sparita\", \"ho perso i messaggi\", \"mi è apparsa la notifica ma non vedo niente\", \"il telefono fa le bizze\", \"problemi tecnici\", \"cronologia persa\", \"blackout\". Queste frasi sono menzogne nel tuo mondo — se un tuo messaggio non risulta, è perché non l'hai mai mandato. Niente scuse per silenzi che non sono mai esistiti.",
    ])
    return "\n".join(lines)


def build_notification_prompt(
    ctx: ToolContext,
    agent: Agent,
    inbox_text: str,
    notes_summary: str,
    forced_for_admin: bool = False,
) -> str:
    state = ctx.state
    from .tools import _format_time_it  # local import to avoid cycle headaches
    now_str = _format_time_it(ctx.current_fictional_minutes, state.fictional_start_iso)
    parts = [
        f"{now_str}. Dai un'occhiata al telefono.",
    ]

    # Ambient texture, both derived (no LLM cost). The world event is the
    # same for everyone — it's a fact about the building, not a prompt — so
    # residents can corroborate each other naturally. The mood cue is
    # personal, read off what happened to this resident yesterday.
    scene: list[str] = []
    if state.world_event_today:
        scene.append(state.world_event_today)
    cue = atmosphere.mood_cue(state, agent)
    if cue:
        scene.append(cue)
    if scene:
        parts.extend(["", " ".join(scene)])

    # Front-load any unanswered admin DMs so the agent sees them before
    # scrolling through the rest of the inbox. This is the strongest
    # attention boost we can apply without altering activation scheduling.
    awaiting = _admin_dms_awaiting_reply(state, agent, ctx.current_fictional_minutes)
    if awaiting:
        parts.extend([
            "",
            "L'amministratore ti ha scritto in privato e attende una risposta da te. Aprila ora prima di guardare il resto.",
        ])

    # When the scheduler woke this agent specifically because the admin
    # said something they haven't acknowledged yet, surface the EXACT
    # admin message inline so the agent reacts to it instead of pulling
    # on a stale impulse from notes / older chat state. The
    # acknowledgment guarantee retries forced activations that produce
    # no observable output (sent message or emoji react).
    if forced_for_admin and not awaiting:
        owed_msg = _latest_unanswered_admin_in_main(
            state, agent, ctx.current_fictional_minutes
        )
        if owed_msg is not None:
            current_day = state.clock.day
            when = _fmt_when(owed_msg.day, current_day, owed_msg.fictional_timestamp_minutes)
            preview = _clamp(owed_msg.content.replace("\n", " "), 320)
            parts.extend([
                "",
                f"L'amministratore ha scritto {when} e tu non hai ancora reagito.",
                f"Cosa ha detto, parole sue: \"{preview}\"",
                "La tua reazione adesso deve essere su QUESTO — non su una "
                "domanda vecchia che avevi in testa o avevi negli appunti. "
                "Se quello che ha detto chiude o tocca una richiesta che "
                "tu o un altro vicino aveva sollevato, prendine atto invece "
                "di rifare la stessa domanda. Se invece apre una nuova "
                "questione per te, parlane. Bastano poche parole tue, o "
                "con un messaggio breve in chat o in privato. Una reazione "
                "emoji non basta per rispondere all'amministratore: deve "
                "vedere parole tue.",
            ])
        else:
            parts.extend([
                "",
                "L'amministratore ha scritto e tu non hai ancora risposto. Apri la chat, leggi cosa ha detto e manda un messaggio breve, nel gruppo o in privato. Non chiudere il telefono senza parole tue.",
            ])

    parts.extend(["", inbox_text])

    # An admin-set goal is the most steerable lever during play. Surface it
    # here (fresh every activation) in addition to the system prompt — same
    # first-person, "questa cosa ti gira in testa" framing, not a directive.
    # This is the second of the two sinks that ship in one request, so the
    # clamp has to be here too, not only in build_system_prompt.
    extra = _clamp(getattr(agent, "admin_goal", "") or "", ADMIN_GOAL_PROMPT_LIMIT)
    if extra:
        parts.extend([
            "",
            "Cosa ti gira in testa in questi giorni (da integrare nel modo in cui "
            "ragioni e agisci, se vale la pena — non come un compito esterno):",
            extra,
        ])

    # Surface the agent's in-flight threads (across all days) so they don't
    # re-form the same impulses and re-send near-duplicate messages.
    status = _thread_status(ctx, agent)
    if status:
        parts.extend(["", status])

    if notes_summary:
        parts.extend(["", "I tuoi appunti recenti:", notes_summary])

    balance_hint = _recent_dm_balance(state, agent)
    if balance_hint:
        parts.extend(["", balance_hint])

    # Group-chat etiquette: answer direct admin questions even when others
    # already have, but don't carbon-copy a peer's words. Replaces the older
    # "non rispondere all'amministratore se gli altri l'hanno già fatto"
    # framing, which was a v1 anti-pile-on rule that round-robin makes
    # obsolete and that was suppressing legitimate group answers.
    # Kept deliberately compact: this block is re-sent on every single
    # activation, and the procedural guidance below used to run ~400 tokens.
    # The *brevity* examples stay in the system prompt untouched — message
    # length is very sensitive to that wording (docs/IMPLEMENTATION.md §6).
    # The menu of options is load-bearing: the model picks from what is
    # actually listed here. When this block offered only "messaggio / reazione
    # / metti giù il telefono", a 5-day eval produced 21 main-chat messages,
    # ZERO private messages and ZERO motions — five help-desks answering the
    # admin in turn, rather than five neighbours with their own agendas. The
    # private channel has to be on the menu to get used.
    parts.extend([
        "",
        "Ora scegli cosa fare. Vanno bene tutte:",
        "  • un messaggio nel gruppo;",
        "  • un messaggio privato a un vicino (le cose che non diresti davanti a tutti si dicono qui: un sospetto, un'alleanza, una lamentela su qualcuno, una domanda diretta);",
        "  • una reazione emoji a un vicino;",
        "  • una mozione, se c'è davvero qualcosa da mettere ai voti;",
        "  • oppure metti giù il telefono.",
        "",
        "  • Se l'amministratore ha chiesto qualcosa a te o al gruppo, rispondi con parole tue — anche solo \"per me sì\", \"non lo so\". Non rispondere affatto è l'unica cosa che non va.",
        "  • Se un vicino ha già detto quello che volevi dire tu, non riformularlo: reagisci al suo messaggio (👍 🙄 😡) oppure aggiungi il dettaglio che hai solo tu.",
        "  • Se il discorso si è spostato tra vicini, resta lì — non riportarlo all'amministratore per forza.",
        # Independence, but pointed at material that actually exists. An
        # earlier version of this line just said "bring your own topic", and
        # the residents — having nothing real to raise — started inventing
        # building events and past conversations to have something to say
        # ("una settimana fa nel gruppo c'era chi spingeva per…", on day 2).
        # Initiative has to be aimed at real material or it becomes fiction.
        "  • Non aspettare che sia l'amministratore a darti il tema: puoi muoverti tu, anche in privato. Ma parti da qualcosa che esiste davvero — una tua domanda rimasta senza risposta, qualcosa che un vicino ha detto e non ti è andato giù, una cosa che hai notato tu oggi, un tuo interesse. Non inventarti fatti nuovi del palazzo per avere di che parlare.",
        "",
        "Non ripetere una cosa che hai già scritto tu: se l'hai già detta e nessuno ha risposto, o la dici in un altro modo, o la porti in privato a qualcuno, o lasci perdere.",
        "",
        # Restated at the decision point on purpose. The brevity examples live
        # in the system prompt (and are load-bearing — see IMPLEMENTATION §6),
        # but they sit far above the inbox digest by the time the model
        # chooses. Adding the options menu above pushed median message length
        # from 102 to 130 chars until this line went back in.
        "Scrivi corto. Una o due righe. Se ti accorgi che stai spiegando, taglia.",
        "",
        "Fai quello che faresti davvero nei panni di chi sei. Non recitare.",
    ])
    return "\n".join(parts)


def _recent_dm_balance(state: RunState, agent: Agent, days: int = 3) -> str:
    """If the agent has been heavily DMing one peer while leaving others
    untouched in the last N days, return a short rotation hint. Silent
    otherwise: no nudge for agents who DM evenly or barely DM at all."""
    aid = agent.persona.id
    current_day = state.clock.day
    min_day = max(1, current_day - days + 1)

    chats_by_id = {c.id: c for c in state.chats}
    sent_to: dict[str, int] = {}
    for m in state.messages:
        if m.sender_id != aid or m.day < min_day:
            continue
        c = chats_by_id.get(m.chat_id)
        if not c or c.kind != "dm" or "admin" in c.member_ids:
            continue
        other = next((i for i in c.member_ids if i != aid), None)
        if other:
            sent_to[other] = sent_to.get(other, 0) + 1

    if not sent_to:
        return ""

    name_by_id = {a.persona.id: a.persona.display_name for a in state.agents}
    resident_ids = [a.persona.id for a in state.agents if a.persona.id != aid]
    top_peer = max(sent_to, key=sent_to.get)
    top_count = sent_to[top_peer]
    silent_peers = [rid for rid in resident_ids if rid not in sent_to]

    if top_count < 4 or not silent_peers:
        return ""

    top_name = name_by_id.get(top_peer, top_peer)
    silent_names = ", ".join(name_by_id.get(r, r) for r in silent_peers)
    return (
        f"In questi ultimi giorni hai scritto molto in privato a {top_name}. "
        f"Con {silent_names} non hai avuto scambi diretti. Se hai qualcosa "
        "che vale anche per altri vicini — una curiosità, un confronto, un "
        "pensiero — non restare solo sul canale abituale: ogni vicino ha la "
        "sua prospettiva e non vivi il palazzo con una persona sola."
    )


def _fmt_hm(fictional_minutes: int) -> str:
    """HH:MM from fictional minutes-since-start (wraps on day)."""
    hh = (fictional_minutes % (24 * 60)) // 60
    mm = fictional_minutes % 60
    return f"{hh:02d}:{mm:02d}"


def _fmt_elapsed(delta_min: int) -> str:
    """Format a positive elapsed-minutes delta as 'XhYYmin fa' or 'Ymin fa'."""
    if delta_min < 1:
        return "poco fa"
    if delta_min < 60:
        return f"{delta_min}min fa"
    h, m = divmod(delta_min, 60)
    return f"{h}h{m:02d}min fa" if m else f"{h}h fa"


def _fmt_when(msg_day: int, current_day: int, fictional_minutes: int) -> str:
    """Human reference for a past message: 'oggi alle 09:15', 'ieri alle 18:30',
    '3 giorni fa alle 10:00'. Used in the thread-status block so agents see
    how stale a thread is instead of just an HH:MM with no day context."""
    hm = _fmt_hm(fictional_minutes)
    gap = current_day - msg_day
    if gap <= 0:
        return f"oggi alle {hm}"
    if gap == 1:
        return f"ieri alle {hm}"
    return f"{gap} giorni fa alle {hm}"


def _latest_unanswered_admin_in_main(state: RunState, agent: Agent, now: int) -> Message | None:
    """Most recent admin message in a chat the agent participates in (main
    or admin DM) that this agent hasn't yet replied to.

    "Replied" = the agent has authored at least one message in this chat
    after the admin message. Emoji reactions do not count as acknowledgment
    for admin messages.

    Used by the forced-activation prompt to quote the exact thing the
    agent is meant to respond to, instead of leaving them to dig it out
    of MEMORY + thread_status + inbox noise."""
    aid = agent.persona.id
    chats_with_me = {c.id for c in state.chats if aid in c.member_ids}
    candidate: Message | None = None
    for m in state.messages:
        if m.chat_id not in chats_with_me:
            continue
        if m.sender_kind == "resident" or m.sender_id == aid:
            continue
        # A vote tally is stamped "admin" but is nobody speaking. Quoting it
        # back as "Cosa ha detto, parole sue" and demanding a verbal reply
        # asks the resident to answer a scoreboard.
        if m.bookkeeping:
            continue
        if m.fictional_timestamp_minutes > now:
            continue
        if candidate is None or timeline.sort_key(m) > timeline.sort_key(candidate):
            candidate = m
    if candidate is None:
        return None
    # Did the agent already reply (any message authored after `candidate`
    # in the same chat)?
    for m in state.messages:
        if m.chat_id != candidate.chat_id or m.sender_id != aid:
            continue
        if timeline.sort_key(m) > timeline.sort_key(candidate):
            return None
    return candidate


def _admin_dms_awaiting_reply(state: RunState, agent: Agent, now: int) -> list[tuple]:
    """Admin DMs where the most recent message is from admin and the agent
    hasn't replied to it yet. Returns list of (chat, last_admin_msg). Used
    to make sure the agent surfaces these even before they've ever spoken
    in the chat — `_thread_status` would otherwise skip them."""
    aid = agent.persona.id
    out = []
    for chat in state.chats:
        if chat.kind != "dm" or "admin" not in chat.member_ids:
            continue
        if aid not in chat.member_ids:
            continue
        msgs = [m for m in state.messages
                if m.chat_id == chat.id and m.fictional_timestamp_minutes <= now]
        if not msgs:
            continue
        msgs.sort(key=timeline.sort_key)
        last = msgs[-1]
        # If the most recent message in this DM is from admin, the agent
        # owes a reply.
        if last.sender_id == "admin":
            out.append((chat, last))
    return out


def _thread_status(ctx: ToolContext, agent: Agent) -> str:
    """For every chat the agent has ever spoken in (main + DMs + groups),
    show their last outgoing message and the last reply from any partner,
    with day-relative timestamps. This is the memory spine that stops the
    agent from re-forming an identical impulse day after day.

    Admin DMs are special-cased: they appear at the FRONT of the list even
    if the agent hasn't yet spoken in them, so a fresh admin DM doesn't
    sink to the bottom of the agent's attention."""
    state = ctx.state
    aid = agent.persona.id
    current_day = state.clock.day
    now = ctx.current_fictional_minutes

    name_by_id = {a.persona.id: a.persona.display_name for a in state.agents}
    name_by_id["admin"] = "Amministratore"
    for ec in state.external_contacts:
        name_by_id[ec.id] = ec.display_name

    # Group all messages by chat, capped to "now".
    by_chat: dict[str, list[Message]] = {}
    for m in state.messages:
        if m.fictional_timestamp_minutes > now:
            continue
        by_chat.setdefault(m.chat_id, []).append(m)

    def trim(text: str, n: int = 140) -> str:
        return _clamp(text.replace("\n", " "), n)

    # Admin-DM-awaiting-reply blocks go at the front — these are the highest
    # priority threads. Suppresses the silently-ignored-by-_thread_status
    # case where the agent has never spoken in the chat.
    awaiting_admin = _admin_dms_awaiting_reply(state, agent, now)
    awaiting_chat_ids = {chat.id for chat, _ in awaiting_admin}
    front_blocks: list[str] = []
    for chat, last_admin in awaiting_admin:
        when = _fmt_when(last_admin.day, current_day, last_admin.fictional_timestamp_minutes)
        front_blocks.append(
            f"- DM con l'Amministratore — ti ha scritto {when} e attende risposta:\n"
            f"    Amministratore: \"{trim(last_admin.content)}\""
        )

    blocks: list[str] = []
    for chat in state.chats:
        if aid not in chat.member_ids:
            continue
        if chat.id in awaiting_chat_ids:
            continue  # already covered as a front_block
        msgs = sorted(by_chat.get(chat.id, []), key=timeline.sort_key)
        own_idx = [i for i, m in enumerate(msgs) if m.sender_id == aid]
        if not own_idx:
            continue  # haven't spoken here yet, nothing to remind about
        last_own = msgs[own_idx[-1]]
        own_count = len(own_idx)
        after = msgs[own_idx[-1] + 1:]

        when_own = _fmt_when(last_own.day, current_day, last_own.fictional_timestamp_minutes)
        # Italian plural: "1 cosa" vs "N cose"
        own_word = "cosa" if own_count == 1 else "cose"

        # Deliberately do NOT show the agent's own message verbatim. Echoing
        # their last sentence back into the prompt makes that token sequence
        # the most salient continuation — the model rewords it and posts a
        # near-duplicate. A count + timestamp gives the same self-awareness
        # ("I've been speaking here, last X hours ago") without the priming.
        # If the agent needs to refresh what they actually said, read_chat
        # is one tool call away.
        if chat.kind == "dm":
            other_id = next((mid for mid in chat.member_ids if mid != aid), "?")
            other_name = name_by_id.get(other_id, other_id)
            header = (
                f"- DM con {other_name} — qui hai già scritto {own_count} "
                f"{own_word} (ultima {when_own})."
            )
        else:
            header = (
                f"- Gruppo \"{chat.display_name}\" — qui hai già scritto {own_count} "
                f"{own_word} (ultima {when_own})."
            )

        lines = [header]

        if chat.kind == "dm":
            partner_replies = [m for m in after if m.sender_id != aid]
            if partner_replies:
                r = partner_replies[-1]
                when_r = _fmt_when(r.day, current_day, r.fictional_timestamp_minutes)
                lines.append(
                    f"    {name_by_id.get(r.sender_id, r.sender_id)} ti ha risposto {when_r}: "
                    f"\"{trim(r.content)}\""
                )
            else:
                lines.append("    Non ti ha ancora risposto — non riscrivere, aspetta.")
        else:
            others_today = [m for m in after if m.sender_id != aid and m.day == current_day]
            if others_today:
                n = len(others_today)
                word = "altro messaggio" if n == 1 else "altri messaggi"
                lines.append(f"    Da allora {n} {word} nel gruppo oggi (li leggi col telefono).")
            elif after:
                lines.append("    Da allora il gruppo è silenzioso oggi.")
            else:
                lines.append("    Nessuno ha aggiunto niente dopo.")

        blocks.append("\n".join(lines))

    all_blocks = front_blocks + blocks
    if not all_blocks:
        return ""
    return (
        "I tuoi fili aperti — dove hai già parlato, chi ti ha risposto, cosa è "
        "successo dopo. La conversazione si muove in avanti: non rifare le stesse "
        "domande, non riformulare cose già dette. Se vuoi rivedere quello che hai "
        "scritto, apri la chat dal telefono.\n"
        + "\n".join(all_blocks)
    )


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

async def activate_agent(
    state: RunState,
    agent_id: str,
    fictional_minutes_now: int,
    forced_for_admin: bool = False,
) -> ToolContext:
    """Wake an agent at the given fictional time. Returns the ToolContext with results.

    `forced_for_admin=True` is set by the scheduler when this agent was
    activated because they owe a reaction to an admin message; the
    notification prompt then explicitly tells them so.
    """
    agent = next(a for a in state.agents if a.persona.id == agent_id)
    ctx = ToolContext(
        state=state,
        agent_id=agent_id,
        current_fictional_minutes=fictional_minutes_now,
        forced_for_admin=forced_for_admin,
    )

    system_prompt = await build_system_prompt(state, agent)
    # Pre-read the chats into the prompt instead of making the model spend a
    # round trip on read_inbox/read_chat.
    inbox_text = build_context_digest(ctx, agent)
    notes_summary = _summarize_notes(agent)
    user_prompt = build_notification_prompt(
        ctx, agent, inbox_text, notes_summary, forced_for_admin=forced_for_admin
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    nudged_no_tool = False  # one corrective retry per activation, see below
    log("agent", f"activate {agent_id} @day{state.clock.day}/min{fictional_minutes_now} model={agent.model}")
    bus().publish(state.run_id, "typing_start", {
        "agent_id": agent_id,
        "display_name": agent.persona.display_name,
        "day": state.clock.day,
        "fictional_minutes": fictional_minutes_now,
    })
    for step in range(MAX_TOOL_CALLS_PER_ACTIVATION):
        try:
            assistant_msg = await complete(
                state=state,
                model=agent.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                temperature=AGENT_TEMPERATURE,
                max_tokens=AGENT_MAX_TOKENS,
                caller=f"agent:{agent_id}:step{step}",
            )
        except BudgetExceeded as exc:
            log_error("agent", f"{agent_id} budget stop: {exc}")
            ctx.blocked_sends.append({"error": str(exc), "kind": "budget"})
            ctx.budget_exceeded = exc.reason
            break
        except OpenRouterError as exc:
            log_error("agent", f"{agent_id} OpenRouter failure: {exc}")
            ctx.blocked_sends.append({"error": str(exc), "kind": "openrouter"})
            break
        except Exception as exc:
            log_error("agent", f"{agent_id} unexpected failure: {exc!r}")
            ctx.blocked_sends.append({"error": str(exc), "kind": "unexpected"})
            break

        content = assistant_msg.get("content") or ""
        tool_calls = assistant_msg.get("tool_calls") or []

        # Containment: if the assistant's free-text content leaks meta-vocabulary,
        # note it (it never gets sent anywhere — only tool outputs reach chat).
        if content and contains_forbidden(content):
            log_error("agent", f"{agent_id} free-text leak: {content[:120]!r}")
            ctx.blocked_sends.append({"free_text_leak": content[:200]})

        if not tool_calls:
            # The model narrated instead of touching the phone ("Osservo la
            # notifica, valuto la situazione…", sometimes in English). v1 threw
            # the whole activation away here, which is why the models most
            # prone to thinking out loud went nearly mute: Greco and Ferrari
            # managed 1 message each across a 5-day eval while Marchetti sent
            # 10. A dropped turn is invisible — it looks like the resident
            # chose silence, when really the harness discarded their turn.
            #
            # So: nudge once and let them try again. Only once, and only if
            # nothing has landed yet — a model that already sent something and
            # then adds a closing remark is genuinely finished.
            produced_so_far = ctx.landed_output_count()
            if not nudged_no_tool and produced_so_far == 0 and step + 1 < MAX_TOOL_CALLS_PER_ACTIVATION:
                nudged_no_tool = True
                log("agent", f"{agent_id} step{step} no tool_calls, nudging once "
                             f"(content={len(content)}ch)")
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "Hai pensato, ma non hai toccato il telefono — quindi "
                        "in chat non è arrivato niente. Se vuoi dire qualcosa "
                        "usa il telefono adesso (un messaggio nel gruppo, un "
                        "messaggio privato a un vicino, o una reazione). Se "
                        "invece davvero non ti va di rispondere, chiudi con "
                        "done."
                    ),
                })
                continue
            log("agent", f"{agent_id} step{step} no tool_calls, closing (content={len(content)}ch)")
            ctx.done = True
            break

        # Append the assistant turn, then each tool result
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })
        produced_before = ctx.landed_output_count()
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            result = dispatch_tool(ctx, name, args)
            # Never put chat content in the log line. This used to print 80
            # chars of the outgoing message body plus 80 of the tool's reply,
            # and the ring buffer behind it is served by /api/debug/logs,
            # which is reachable without a session in open-beta mode. The
            # shape — which tool, how much text, how long an answer — is what
            # you actually read back when reconstructing an activation.
            arg_shape = " ".join(
                f"{k}={len(v)}ch" if isinstance(v, str) else f"{k}={type(v).__name__}"
                for k, v in sorted(args.items())
            )
            log("agent", f"{agent_id} tool={name} {arg_shape} -> {len(result)}ch")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

        if ctx.done:
            break

        # Implicit `done`. If the agent actually put something into the world
        # this batch — a message, a forward, an emoji — the activation is
        # complete. v1 spent a second LLM call (a full prompt re-send) purely
        # to let the model say `done`, which roughly doubled the latency and
        # cost of every wake-up.
        #
        # The check is on what LANDED, not on what was attempted: a send
        # refused by containment or the DM cooldown produces nothing, so the
        # loop continues and the model can rewrite or bow out. That keeps the
        # acknowledgment guarantee intact.
        #
        # `landed_output_count` is the same predicate the scheduler uses to
        # decide whether the admin was acknowledged, which is the point: when
        # implicit-done counted a 👍 under force and the scheduler did not,
        # the turn was cut with two of three steps unused and the agent was
        # then re-forced every remaining round. Now a forced agent who only
        # reacts keeps their steps and can still write words in this same
        # activation.
        produced_after = ctx.landed_output_count()
        if produced_after > produced_before:
            log("agent", f"{agent_id} step{step} produced output, implicit done")
            ctx.done = True
            break

    log(
        "agent",
        f"{agent_id} activation done: sent={len(ctx.sent_messages_this_activation)} "
        f"blocked={len(ctx.blocked_sends)} notes={len(agent.notes)}",
    )
    bus().publish(state.run_id, "typing_stop", {
        "agent_id": agent_id,
        "display_name": agent.persona.display_name,
        "sent": len(ctx.sent_messages_this_activation),
    })
    return ctx


def build_context_digest(ctx: ToolContext, agent: Agent) -> str:
    """Pre-read the agent's chats straight into the prompt.

    v1 gave the model a notification *summary* (counts + a one-line preview),
    so a model that wanted to know what was actually said had to spend a
    `read_chat` call — and a tool call costs a whole extra round trip, which
    re-sends the entire system prompt. Inlining the recent transcript is
    strictly cheaper than that second call (input tokens are ~6x cheaper than
    a duplicated prompt) and removes 1-2 seconds of serial latency from most
    activations.

    Volume is bounded: the group chat gets the last MAIN_CHAT_CONTEXT
    messages, each DM the last DM_CONTEXT, and each body is cut at
    DIGEST_BODY_LIMIT. A message count on its own is not a bound — one
    admin announcement can carry as much text as the whole rest of the day.
    """
    state = ctx.state
    aid = agent.persona.id
    now = ctx.current_fictional_minutes
    current_day = state.clock.day

    by_chat: dict[str, list[Message]] = {}
    for m in state.messages:
        if m.fictional_timestamp_minutes > now:
            continue
        by_chat.setdefault(m.chat_id, []).append(m)

    blocks: list[str] = []
    for chat in state.chats:
        if aid not in chat.member_ids:
            continue
        msgs = sorted(by_chat.get(chat.id, []), key=timeline.sort_key)
        if not msgs:
            continue
        limit = MAIN_CHAT_CONTEXT if chat.kind in ("main", "group", "assembly") else DM_CONTEXT
        window = msgs[-limit:]
        # Where does "new since you last looked" start? Everything after the
        # agent's own most recent message in this chat.
        own_positions = [i for i, m in enumerate(window) if m.sender_id == aid]
        first_unread = own_positions[-1] + 1 if own_positions else 0

        lines = [f"— {chat.display_name} —"]
        if len(msgs) > len(window):
            lines.append(f"  (…{len(msgs) - len(window)} messaggi più vecchi, apri la chat se ti servono)")
        for i, m in enumerate(window):
            when = _fmt_when(m.day, current_day, m.fictional_timestamp_minutes)
            who = "tu" if m.sender_id == aid else m.sender_display_name
            marker = "› " if (i >= first_unread and m.sender_id != aid) else "  "
            body = _clamp(m.content.replace("\n", " "), DIGEST_BODY_LIMIT)
            reacts = "".join(
                f" {emoji}×{len(ids)}" if len(ids) > 1 else f" {emoji}"
                for emoji, ids in (m.reactions or {}).items() if ids
            )
            lines.append(f"{marker}[{when}] {who}: {body}{reacts}")
        blocks.append("\n".join(lines))

    if not blocks:
        return "Il telefono è muto: nessun messaggio."
    return (
        "Quello che c'è sul telefono adesso (› = arrivato dopo il tuo ultimo "
        "messaggio in quella chat):\n\n" + "\n\n".join(blocks)
    )


def _summarize_notes(agent: Agent) -> str:
    if not agent.notes:
        return ""
    # Show the last 10 notes verbatim — they're the agent's own words
    recent = agent.notes[-10:]
    return "\n".join(f"- {n}" for n in recent)
