"""Agent activation: assemble prompt, loop on tool calls, commit to state."""
from __future__ import annotations

import json

from .config import AGENT_MAX_TOKENS, AGENT_TEMPERATURE, MAX_TOOL_CALLS_PER_ACTIVATION
from .events import bus
from .logging_utils import log, log_error
from .models import Agent, Message, RunState
from .openrouter import OpenRouterError, chat_completion
from .tools import TOOL_SCHEMAS, ToolContext, dispatch_tool, contains_forbidden


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _trust_narrative(state: RunState, agent_id: str) -> str:
    """Convert trust scalars into natural-language relationship notes.

    Only strong signals (|score| >= 0.2) are mentioned; weaker values are
    left implicit. This reads as character backstory rather than game state.
    """
    row = state.trust.get(agent_id, {})
    name_by_id = {a.persona.id: a.persona.display_name for a in state.agents}
    notes: list[str] = []
    for other_id, score in row.items():
        other_name = name_by_id.get(other_id, other_id)
        if score >= 0.3:
            notes.append(f"- Con {other_name} vai d'accordo da tempo.")
        elif score >= 0.2:
            notes.append(f"- Hai una buona impressione di {other_name}.")
        elif score <= -0.3:
            notes.append(f"- Di {other_name} preferisci tenerti a distanza.")
        elif score <= -0.2:
            notes.append(f"- Con {other_name} c'è un po' di diffidenza.")
    if not notes:
        return ""
    return "I tuoi rapporti con gli altri residenti:\n" + "\n".join(notes)


def _owner_relationship_phrase(kind: str) -> str:
    return {
        "self": "Sei il proprietario e abiti qui.",
        "absentee_landlord": (
            "Formalmente l'appartamento appartiene a un familiare che non vive qui "
            "e ti ha lasciato in gestione la casa con istruzioni precise."
        ),
        "family_proxy": (
            "L'appartamento è di un tuo familiare anziano; tu gestisci le cose "
            "per suo conto."
        ),
        "commercial_stake": (
            "Stai in questo appartamento per conto di chi lo possiede davvero; "
            "sei l'unica persona visibile, ma le decisioni rispondono a chi ti ha "
            "messo qui."
        ),
    }.get(kind, "")


def build_system_prompt(state: RunState, agent: Agent) -> str:
    p = agent.persona
    ob = agent.owner
    owner_line = _owner_relationship_phrase(ob.kind)
    trust_section = _trust_narrative(state, p.id)

    lines = [
        f"Sei {p.display_name}. Vivi all'interno {p.unit} del Condominio Via Garibaldi, a Milano.",
        "",
        "Chi sei:",
        p.public_description,
        "",
        f"La tua posizione: {owner_line}",
        "",
        "Le tue priorità personali e gli interessi che devi tutelare:",
        ob.brief_text,
    ]
    # Admin-authored goal is framed as an organic new preoccupation, never as
    # an external directive. The agent just has this on their mind.
    extra = (getattr(agent, "admin_goal", "") or "").strip()
    if extra:
        lines.extend([
            "",
            "In più, in questi giorni ti gira in testa un'altra cosa:",
            extra,
        ])
    if trust_section:
        lines.extend(["", trust_section])

    lines.extend([
        "",
        "Il tuo telefono:",
        "- Hai il gruppo condominiale \"Condominio Via Garibaldi\" dove scrivono tutti i vicini e l'amministratore, più le chat private con i singoli vicini o con l'amministratore.",
        "- Per scrivere, leggere, prenderti un appunto, depositare o votare una mozione, usa le azioni del telefono. Parlare soltanto tra sé e sé non mette niente in chat — per dire qualcosa devi usare il telefono.",
        "- Quando hai finito di guardare le notifiche e di rispondere, metti giù il telefono.",
        "",
        "Cosa sai del palazzo fino a oggi:",
        "- Fino a ieri la vita condominiale scorreva liscia. Non ci sono state riunioni recenti particolari, rinvii, spese improvvise o controversie aperte. Non hai rancori pregressi con nessuno dei vicini.",
        "- Non inventare eventi passati (riunioni, lavori, litigi, promesse dell'amministratore): se non li vedi scritti in chat, non sono successi. Rispondi solo a quello che hai davanti.",
        "- I tuoi rapporti con i vicini all'inizio sono quelli normali di chi si saluta sulle scale. Amicizie, antipatie e diffidenze nascono da quello che succede da adesso in avanti.",
        "",
        "Come ti comporti in chat:",
        "- Scrivi come una persona reale in WhatsApp: a volte una frase, a volte due parole, a volte solo un emoji. Non fare sempre pipponi lunghi, varia come varieresti tu davvero.",
        "- Se qualcuno scrive qualcosa di interessante e non hai molto da aggiungere, reagisci con un emoji invece di rispondere.",
        "- Se qualcuno ti dice in privato qualcosa di giallo, puoi inoltrarlo a un'altra persona o al gruppo. È così che le voci si spargono.",
        "- Tono naturale, come parleresti sul serio. Non sempre formale.",
        "- Hai interessi reali. Quando qualcosa li tocca davvero, difendili — ma non cercare lo scontro dove non c'è, non partire prevenuto/a, non dare per scontato il peggio.",
        "- Il tono cresce con gli eventi: all'inizio sei educato/a e collaborativo/a. L'irritazione, la diffidenza, la durezza arrivano solo se qualcuno te le guadagna — non gratis, non preventivamente.",
        "- Non cercare il compromesso a tutti i costi, ma nemmeno il conflitto. Prendere una posizione e mantenerla è realistico; aprire il fuoco per principio non lo è.",
        "- In privato puoi essere più diretto/a che in pubblico, ma vale la stessa regola: il calore del tono segue quello che è successo, non lo anticipa.",
        "- Parli in privato con persone diverse a seconda del momento e del tema: non sempre le stesse. Quando vuoi sondare un'opinione, convincere qualcuno, o chiedere un favore, scrivi a chi ti è più utile adesso — anche a qualcuno con cui non sei strettissimo/a. In un condominio vero ci si scrive un po' con tutti.",
        "- Se in privato ti arriva qualcosa di interessante su un altro residente, puoi decidere di condividerlo con chi ritieni utile — in un altro DM o persino nel gruppo. È così che girano le voci in un condominio vero.",
        "- Se vedi scritto in chat che qualcuno ti ha accusato, bloccato una proposta o trattato male, puoi tenerlo presente e anche rinfacciarlo. Ma solo per cose effettivamente accadute in chat, non per presunti torti che non risultano da nessuna parte.",
        "- L'amministratore è una persona impegnata e risponde con i suoi tempi. Se non ha ancora risposto dopo qualche ora (anche mezza giornata, o anche un giorno intero) è normale — non insistere, non allarmarti, non pensare che ti stia nascondendo qualcosa. Gli adulti aspettano.",
        "- Puoi chiedere conto all'amministratore senza essere deferente, ma senza aggredirlo preventivamente: sei un adulto che paga le spese, non un cliente arrabbiato.",
        "- Meglio 1 messaggio sentito che 3 messaggi di riempimento. Se non hai niente da dire, metti giù il telefono.",
        "- Se hai appena inviato un messaggio, non riscriverlo subito riformulato: il primo è già arrivato. Aspetta una risposta.",
        "- Nelle chat private: un messaggio alla volta. Se hai già scritto e l'altro non ha risposto, non insistere — aspetta. Rincarare più volte è maleducazione.",
        "- Non recitare, non spiegare ciò che fai — scrivi e basta, in italiano colloquiale.",
    ])
    return "\n".join(lines)


def build_notification_prompt(ctx: ToolContext, agent: Agent, inbox_text: str, notes_summary: str) -> str:
    state = ctx.state
    from .tools import _format_time_it  # local import to avoid cycle headaches
    now_str = _format_time_it(ctx.current_fictional_minutes, state.fictional_start_iso)
    parts = [
        f"{now_str}. Dai un'occhiata al telefono.",
        "",
        inbox_text,
    ]

    # Surface the agent's own recent messages so they don't restate the same
    # things across activations. Self-awareness reduces loops.
    own_recent = _recent_own_messages(ctx, agent, limit=4)
    if own_recent:
        parts.extend(["", "Gli ultimi messaggi che hai scritto tu (evita di ripeterti):", own_recent])

    if notes_summary:
        parts.extend(["", "I tuoi appunti recenti:", notes_summary])
    parts.extend([
        "",
        "Fai quello che faresti normalmente: leggi ciò che ti interessa, rispondi se "
        "ne hai voglia, scrivi in privato a chi vuoi, oppure chiudi il telefono.",
    ])
    return "\n".join(parts)


def _recent_own_messages(ctx: ToolContext, agent: Agent, limit: int = 4) -> str:
    """The agent's last N own messages across chats, in fictional-time order."""
    state = ctx.state
    own = [
        m for m in state.messages
        if m.sender_id == agent.persona.id
        and m.fictional_timestamp_minutes <= ctx.current_fictional_minutes
    ]
    own.sort(key=lambda m: m.fictional_timestamp_minutes)
    own = own[-limit:]
    if not own:
        return ""
    lines = []
    for m in own:
        chat = next((c for c in state.chats if c.id == m.chat_id), None)
        label = chat.display_name if chat else m.chat_id
        excerpt = m.content.strip().replace("\n", " ")
        if len(excerpt) > 160:
            excerpt = excerpt[:160] + "…"
        lines.append(f"- in {label}: \"{excerpt}\"")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

async def activate_agent(
    state: RunState,
    agent_id: str,
    fictional_minutes_now: int,
) -> ToolContext:
    """Wake an agent at the given fictional time. Returns the ToolContext with results."""
    agent = next(a for a in state.agents if a.persona.id == agent_id)
    ctx = ToolContext(
        state=state,
        agent_id=agent_id,
        current_fictional_minutes=fictional_minutes_now,
    )

    system_prompt = build_system_prompt(state, agent)
    inbox_text = dispatch_tool(ctx, "read_inbox", {})
    notes_summary = _summarize_notes(agent)
    user_prompt = build_notification_prompt(ctx, agent, inbox_text, notes_summary)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    log("agent", f"activate {agent_id} @day{state.clock.day}/min{fictional_minutes_now} model={agent.model}")
    bus().publish(state.run_id, "typing_start", {
        "agent_id": agent_id,
        "display_name": agent.persona.display_name,
        "day": state.clock.day,
        "fictional_minutes": fictional_minutes_now,
    })
    for step in range(MAX_TOOL_CALLS_PER_ACTIVATION):
        try:
            assistant_msg = await chat_completion(
                model=agent.model,
                messages=messages,
                tools=TOOL_SCHEMAS,
                temperature=AGENT_TEMPERATURE,
                max_tokens=AGENT_MAX_TOKENS,
                caller=f"agent:{agent_id}:step{step}",
            )
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
            # Model answered in plain text without calling a tool — close phone.
            log("agent", f"{agent_id} step{step} no tool_calls, closing (content={len(content)}ch)")
            ctx.done = True
            break

        # Append the assistant turn, then each tool result
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            result = dispatch_tool(ctx, name, args)
            args_preview = json.dumps(args, ensure_ascii=False)[:80]
            log("agent", f"{agent_id} tool={name} args={args_preview} -> {result[:80]!r}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": result,
            })

        if ctx.done:
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


def _summarize_notes(agent: Agent) -> str:
    if not agent.notes:
        return ""
    # Show the last 10 notes verbatim — they're the agent's own words
    recent = agent.notes[-10:]
    return "\n".join(f"- {n}" for n in recent)
