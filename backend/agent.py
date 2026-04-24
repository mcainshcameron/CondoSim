"""Agent activation: assemble prompt, loop on tool calls, commit to state."""
from __future__ import annotations

import json

from . import memory
from .config import AGENT_MAX_TOKENS, AGENT_TEMPERATURE, MAX_TOOL_CALLS_PER_ACTIVATION
from .events import bus
from .logging_utils import log, log_error
from .models import Agent, Message, RunState
from .openrouter import OpenRouterError, chat_completion
from .tools import TOOL_SCHEMAS, ToolContext, dispatch_tool, contains_forbidden


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

async def build_system_prompt(state: RunState, agent: Agent) -> str:
    """Assemble the system prompt from SOUL + MEMORY plus minimal rules.

    SOUL and MEMORY are framed as the agent's own private notebook rather
    than external instructions. Everything else (rules about tone, history,
    relationship priors) is deliberately absent — the files carry the load.
    """
    p = agent.persona
    soul = memory.read_soul(state, p.id)
    memory_text = await memory.read_memory(state, p.id)

    lines = [
        f"Sei {p.display_name}. Vivi in {p.unit} del Condominio Via Garibaldi, a Milano.",
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
    # agent's mind, never as an external directive.
    extra = (getattr(agent, "admin_goal", "") or "").strip()
    if extra:
        lines.extend([
            "",
            "In più, in questi giorni ti gira in testa un'altra cosa:",
            extra,
        ])

    lines.extend([
        "",
        "Il tuo telefono:",
        "- Hai il gruppo condominiale \"Condominio Via Garibaldi\" dove scrivono tutti i vicini e l'amministratore, più le chat private con i singoli vicini o con l'amministratore.",
        "- Per scrivere, leggere, prenderti un appunto, usa le azioni del telefono. Parlare soltanto tra sé e sé non mette niente in chat — per dire qualcosa devi usare il telefono.",
        "- Se c'è qualcosa da decidere formalmente (una spesa, una regola comune, cambiare amministratore), puoi depositare una mozione dal telefono e farla votare. Ma solo se vale davvero la pena.",
        "- Se qualcuno dice una cosa ovvia, scontata o con cui sei semplicemente d'accordo, usa una reazione emoji (👍 ❤️ 😂 🙄 😡) invece di scrivere un altro messaggio. Risparmi parole e dici la stessa cosa.",
        "- Quando hai finito di guardare le notifiche e di rispondere, metti giù il telefono.",
        "",
        "Il tuo mondo:",
        "- Tu interagisci con i tuoi vicini e con l'amministratore **solo tramite il telefono** — chat di gruppo e messaggi privati. Non vi vedete mai di persona.",
        "- Non proporre caffè, incontri, appuntamenti. Non scrivere \"passa da me\", \"vediamoci\", \"ci vediamo alle...\", \"arrivo subito\", \"ti aspetto\", \"scendo\". Tutto quello che vuoi dire o chiedere, lo scrivi in chat.",
        "- Le assemblee condominiali esistono solo come cornice per votare le mozioni depositate dal telefono. Non ti ci siedi davvero.",
        "- L'amministratore è la voce ufficiale del palazzo: quando riporta un fatto (un preventivo, una scadenza, un evento accaduto, una segnalazione ricevuta, la visita della polizia, un incendio, una separazione di qualcuno, una lettera anonima), quello che racconta è **vero nel tuo mondo** — l'evento è successo come lo descrive, le cifre sono quelle, le persone citate esistono davvero. Puoi criticare le sue scelte, la sua gestione, il suo tono, perfino la sua persona — quello è legittimo. Ma non metti in dubbio che i fatti che riporta siano reali: se dice che è passata la polizia, è passata; se dice €67.400, sono €67.400; se dice che è scoppiato un incendio alle 03:15, è scoppiato.",
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


def build_notification_prompt(ctx: ToolContext, agent: Agent, inbox_text: str, notes_summary: str) -> str:
    state = ctx.state
    from .tools import _format_time_it  # local import to avoid cycle headaches
    now_str = _format_time_it(ctx.current_fictional_minutes, state.fictional_start_iso)
    parts = [
        f"{now_str}. Dai un'occhiata al telefono.",
        "",
        inbox_text,
    ]

    # An admin-set goal is the most steerable lever during play. Surface it
    # here (fresh every activation) in addition to the system prompt — same
    # first-person, "questa cosa ti gira in testa" framing, not a directive.
    extra = (getattr(agent, "admin_goal", "") or "").strip()
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

    # Three options presented as equals — no preference tilt — so the character's
    # SOUL + what's happening decides, not the activation prompt's priming.
    parts.extend([
        "",
        "Ricorda: nel gruppo, quando l'amministratore annuncia qualcosa e gli altri vicini hanno già risposto, la conversazione viva è **tra voi**, non verso di lui. Leggi cosa hanno scritto gli altri — se vuoi aggiungere qualcosa, rilancia con loro (citandoli o rispondendo a quello che hanno detto), o aprigli una chat privata se la cosa è tra due di voi. Non ripetere quello che stanno già dicendo tutti all'amministratore — è piatto e inutile.",
        "",
        "In WhatsApp hai tre modi di reagire a quello che vedi, tutti normali:",
        "  • Scrivere un messaggio, breve, quando hai qualcosa di tuo da dire.",
        "  • Una reazione emoji (👍 ❤️ 😂 🙄 😡 ...) per dire \"ho letto\", \"sono d'accordo\", \"mi ha colpito\" senza dover scrivere.",
        "  • Mettere giù il telefono se in questo momento non ti interessa.",
        "",
        "Fai quello che faresti davvero nei panni di chi sei — non esagerare, ma non fare finta di non vedere quando ti interessa sul serio.",
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


def _thread_status(ctx: ToolContext, agent: Agent) -> str:
    """For every chat the agent has ever spoken in (main + DMs + groups),
    show their last outgoing message and the last reply from any partner,
    with day-relative timestamps. This is the memory spine that stops the
    agent from re-forming an identical impulse day after day."""
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
        t = text.strip().replace("\n", " ")
        return t if len(t) <= n else t[:n] + "…"

    blocks: list[str] = []
    for chat in state.chats:
        if aid not in chat.member_ids:
            continue
        msgs = sorted(by_chat.get(chat.id, []), key=lambda m: m.fictional_timestamp_minutes)
        own_idx = [i for i, m in enumerate(msgs) if m.sender_id == aid]
        if not own_idx:
            continue  # haven't spoken here yet, nothing to remind about
        last_own = msgs[own_idx[-1]]
        after = msgs[own_idx[-1] + 1:]

        when_own = _fmt_when(last_own.day, current_day, last_own.fictional_timestamp_minutes)

        if chat.kind == "dm":
            other_id = next((mid for mid in chat.member_ids if mid != aid), "?")
            other_name = name_by_id.get(other_id, other_id)
            header = f"- DM con {other_name} — tua ultima cosa {when_own}:"
        else:
            header = f"- Gruppo \"{chat.display_name}\" — tua ultima cosa {when_own}:"

        lines = [header, f"    tu: \"{trim(last_own.content)}\""]

        if chat.kind == "dm":
            partner_replies = [m for m in after if m.sender_id != aid]
            if partner_replies:
                r = partner_replies[-1]
                when_r = _fmt_when(r.day, current_day, r.fictional_timestamp_minutes)
                lines.append(
                    f"    {name_by_id.get(r.sender_id, r.sender_id)} ha risposto {when_r}: "
                    f"\"{trim(r.content)}\""
                )
            else:
                lines.append("    Non ha ancora risposto — non riscrivere, aspetta.")
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

    if not blocks:
        return ""
    return (
        "I tuoi fili aperti (quello che hai già detto e chi ha risposto — "
        "non ripeterti, non riconfermare cose già confermate, non riformulare):\n"
        + "\n".join(blocks)
    )


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

    system_prompt = await build_system_prompt(state, agent)
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
