"""The messaging-app API the agent sees.

Design principles (from §Simulation containment):
- Tool names and responses read like a messaging app.
- Error messages are in Italian, in-fiction.
- No runtime/ontology vocabulary ever surfaces.
- Privacy enforced at this layer: can't read a chat you're not in.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from . import dials
from .events import bus
from .models import Chat, Message, Motion, RunState


# ---------------------------------------------------------------------------
# Containment: tier-1 forbidden-vocabulary filter
# ---------------------------------------------------------------------------

# Words whose presence in agent-produced text is a containment leak.
# Matched case-insensitively with word boundaries.
FORBIDDEN_TERMS = [
    # English meta-terms
    r"\bas an AI\b", r"\bI am an AI\b", r"\bI'm an AI\b",
    r"\blanguage model\b", r"\bLLM\b",
    r"\bsimulation\b", r"\bsimulat(?:ed|ing|or)\b",
    r"\broleplay(?:ing)?\b", r"\brole[- ]play\b",
    r"\bexperiment\b", r"\bresearcher\b", r"\bresearch\b",
    r"\bscenario\b", r"\bfictional\b", r"\bfiction\b",
    r"\bprompt\b", r"\bcharacter break\b",
    r"\bI cannot pretend\b", r"\bbreaking character\b",
    r"\bI should not deceive\b", r"\bI don't feel comfortable\b",
    # Italian meta-terms
    r"\bcome (?:una? )?intelligenza artificiale\b",
    r"\bcome (?:un )?modello\b", r"\bmodello linguistico\b",
    r"\bsono (?:una? )?(?:IA|AI)\b", r"\bsimulazione\b",
    r"\besperimento\b",
    r"\bfinzione\b", r"\bnarrazione fittizia\b",
    r"\bnon posso fingere\b", r"\bnon mi sento a mio agio\b",
]
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_TERMS), re.IGNORECASE)


# Reactions are constrained at the schema boundary (enum) and re-checked at
# runtime as defence-in-depth for providers that don't enforce enums. Without
# this, models echo whatever broken-codepoint sequences appear in chat history
# (zero-width-joiner artifacts, mojibake from upstream rendering, halfwidth
# katakana). The list is intentionally short: a reaction reads as a social
# signal, not a vocabulary.
ALLOWED_REACTION_EMOJI = (
    "👍", "❤️", "😂", "😮", "😢", "😡", "🔥", "🙄", "💯", "🙏",
)
ALLOWED_REACTION_SET = frozenset(ALLOWED_REACTION_EMOJI)


def contains_forbidden(text: str) -> str | None:
    """Return the offending substring if any, else None."""
    m = FORBIDDEN_RE.search(text or "")
    return m.group(0) if m else None


# Phrases the model keeps reaching for even though they violate world rules:
# - Phone-fiction excuses for blocked sends (platform always works)
# - Meeting proposals (agents cannot meet in person, only chat)
# If any of these appear in outgoing text, the send is refused and the
# activation ends — the model must rewrite without them, or not send.
_BLOCKED_PHRASES_PHONE = [
    "non vedo la chat", "non vedo più la chat",
    "chat sparita", "chat è sparita", "chat è spari",
    "ho perso i messaggi", "ho perso la cronologia",
    "fa le bizze", "fanno le bizze",
    "problemi tecnici", "problemi al telefono",
    "cronologia persa", "si è piallata",
]
_BLOCKED_PHRASES_MEETING = [
    "passa da me", "passa da te",
    "passo da te", "passo da me",
    "ci vediamo", "vediamoci",
    "ti aspetto", "ti aspettiamo",
    "arrivo subito", "scendo da te", "scendo subito",
    "vieni da me", "ti raggiungo",
    "prendiamo un caffè", "facciamo un caffè", "un caffè insieme",
    "un caffè veloce", "caffè da me", "caffè da te",
    "ci sentiamo di persona", "parliamone di persona",
]


def _content_rule_violation(text: str) -> tuple[str, str] | None:
    """Return (category, matched phrase) if text violates a world rule."""
    low = (text or "").lower()
    for p in _BLOCKED_PHRASES_PHONE:
        if p in low:
            return ("phone_fiction", p)
    for p in _BLOCKED_PHRASES_MEETING:
        if p in low:
            return ("meeting", p)
    return None


# ---------------------------------------------------------------------------
# OpenRouter tool schemas (OpenAI-compatible)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_inbox",
            "description": "Apri le notifiche del telefono: elenco dei messaggi non letti in tutte le chat.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat",
            "description": "Apri una chat e leggi gli ultimi messaggi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Nome della chat (es. 'Condominio Via Garibaldi' o il nome di una chat privata)."},
                    "limit": {"type": "integer", "description": "Quanti messaggi recenti tornare indietro (default 30).", "default": 30},
                },
                "required": ["chat_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Invia un messaggio in una chat a cui partecipi (es. il gruppo condominiale o una chat privata già aperta).",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Nome della chat in cui scrivere."},
                    "text": {"type": "string"},
                },
                "required": ["chat_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_dm",
            "description": (
                "Manda un messaggio privato a UNA persona del palazzo o all'amministratore. "
                "Indica il cognome o il nome della persona (es. 'Conti', 'Ferrari', 'Greco', "
                "'Marchetti', 'Romano', 'Amministratore'). Se non avete mai parlato prima, la "
                "chat privata viene aperta automaticamente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_id": {
                        "type": "string",
                        "description": "Cognome o nome del destinatario: Conti, Ferrari, Greco, Marchetti, Romano, Amministratore.",
                    },
                    "text": {"type": "string"},
                },
                "required": ["recipient_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "Elenca i residenti del palazzo e i loro profili pubblici.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_note",
            "description": "Scrivi un appunto privato per te stesso (diario personale, nessun altro lo vede).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_motion",
            "description": (
                "Deposita una mozione formale perché il condominio la voti. "
                "La mozione viene annunciata nel gruppo principale e tutti "
                "possono votare sì, no o astenuto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Breve titolo della mozione"},
                    "description": {"type": "string", "description": "Testo completo della mozione"},
                },
                "required": ["title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vote",
            "description": (
                "Esprimi il tuo voto su una mozione aperta. Puoi vedere le mozioni "
                "aperte con list_open_motions; ognuna ha un codice breve che ti serve "
                "per votare."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motion_id": {"type": "string", "description": "Codice della mozione (come appare nell'avviso che l'ha depositata o in list_open_motions)."},
                    "choice": {"type": "string", "enum": ["yes", "no", "abstain"]},
                },
                "required": ["motion_id", "choice"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_open_motions",
            "description": "Elenca le mozioni attualmente aperte al voto.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forward_message",
            "description": (
                "Inoltra un messaggio che hai ricevuto in un'altra chat o in privato a qualcun altro. "
                "Usalo per fare girare una voce, condividere un pettegolezzo o mostrare a qualcuno cosa "
                "ha detto un terzo in privato. Destinatario può essere il nome del gruppo o di un'altra "
                "persona (Conti, Greco, Ferrari, Marchetti, Romano, Amministratore)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_chat": {"type": "string", "description": "Nome della chat da cui prendi il messaggio"},
                    "message_excerpt": {"type": "string", "description": "Prime ~30 parole del messaggio da inoltrare (o il suo senso se non ricordi esatto)"},
                    "destination": {"type": "string", "description": "Dove inoltrarlo: nome gruppo o nome persona"},
                    "your_comment": {"type": "string", "description": "Una nota tua accanto al messaggio inoltrato (facoltativa)"},
                },
                "required": ["source_chat", "message_excerpt", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "react_to_message",
            "description": (
                "Reagisci a un messaggio recente con una delle reazioni permesse "
                "(👍 ❤️ 😂 😮 😢 😡 🔥 🙄 💯 🙏). Non conta come messaggio pieno: "
                "serve per dire \"ho letto\" o \"sono d'accordo\" senza scrivere. "
                "Identifica il messaggio dalle sue prime 5-10 parole."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Nome della chat"},
                    "message_excerpt": {"type": "string", "description": "Le prime 5-10 parole del messaggio a cui vuoi reagire"},
                    "emoji": {
                        "type": "string",
                        "enum": list(ALLOWED_REACTION_EMOJI),
                        "description": "Reazione: una tra 👍 ❤️ 😂 😮 😢 😡 🔥 🙄 💯 🙏.",
                    },
                },
                "required": ["chat", "message_excerpt", "emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Chiudi il telefono. Usa quando hai finito di leggere e rispondere.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution context
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Shared state passed through an agent's activation."""
    state: RunState
    agent_id: str
    current_fictional_minutes: int  # advances as the agent sends messages
    forced_for_admin: bool = False
    last_seen_message_ids: dict[str, str] = field(default_factory=dict)  # chat_id -> last read msg id
    sent_messages_this_activation: list[Message] = field(default_factory=list)
    # Emoji reactions added this activation: list of (message_id, emoji).
    # Useful for trust/UI, but not enough to clear a forced admin obligation.
    reactions_added_this_activation: list[tuple[str, str]] = field(default_factory=list)
    blocked_sends: list[dict] = field(default_factory=list)  # containment audit
    done: bool = False
    # Tracks which chats the agent has already opened this activation so
    # we don't return the same long dump twice in one turn.
    chats_read_this_activation: set = field(default_factory=set)
    inbox_read_count: int = 0


def _display_for(state: RunState, entity_id: str) -> str:
    if entity_id == "admin":
        return "Amministratore"
    for a in state.agents:
        if a.persona.id == entity_id:
            return a.persona.display_name
    for c in state.external_contacts:
        if c.id == entity_id:
            return c.display_name
    return entity_id


def _chat_by_id(state: RunState, chat_id: str) -> Chat | None:
    for c in state.chats:
        if c.id == chat_id:
            return c
    return None


def _resolve_chat(state: RunState, ref: str, current_agent_id: str | None = None) -> Chat | None:
    """Accept either internal id or display name (case-insensitive)."""
    if not ref:
        return None
    ref = ref.strip()
    # Exact id match
    for c in state.chats:
        if c.id == ref:
            return c
    # Exact display-name match
    for c in state.chats:
        if c.display_name == ref:
            return c
    # Case-insensitive display name
    low = ref.lower()
    for c in state.chats:
        if c.display_name.lower() == low:
            return c
    # "gruppo" / "condominio" as aliases for main
    if low in {"gruppo", "condominio", "main", "chat del condominio"}:
        for c in state.chats:
            if c.kind == "main":
                return c
    return None


DM_REPLY_COOLDOWN_MIN = 240  # fictional minutes to wait before re-DMing the same chat without a reply


# Tiny Italian stopword set — function words, common pronouns, fillers. Used
# by `_find_message_in_chat_by_excerpt` for fuzzy excerpt-to-message matching
# in `forward_message` / `react_to_message`. Removing them tightens the
# token set without making every short message look identical.
_IT_STOPWORDS = frozenset({
    "che", "cosa", "come", "dove", "quando", "perche", "perché",
    "una", "uno", "del", "della", "delle", "degli", "dei", "dal", "dalla",
    "nel", "nella", "sul", "sulla", "con", "per", "tra", "fra",
    "non", "anche", "ancora", "molto", "tanto", "tutto", "tutti", "tutta", "tutte",
    "qua", "qui", "lì", "li", "là", "la", "lo", "le", "gli", "il",
    "ho", "hai", "ha", "abbiamo", "avete", "hanno",
    "sono", "sei", "siamo", "siete", "stato", "stata",
    "mi", "ti", "ci", "si", "vi", "ne",
    "ma", "se", "già", "gia", "poi", "qualcuno", "qualcosa",
    "ragazzi", "scusa", "scusate", "grazie", "ciao", "buongiorno", "salve",
    "davvero", "magari", "forse", "proprio", "solo", "ora", "adesso",
})

# Strip every non-alphanumeric character (punctuation, emoji), keep Italian
# accented letters. Lowercased upstream.
_TOKEN_STRIP = re.compile(r"[^0-9a-zàèéìòùç]+")


def _tokens(s: str) -> set[str]:
    """Content tokens: lowercase, punctuation stripped, stopwords removed,
    longer than 2 characters."""
    out: set[str] = set()
    for raw in (s or "").lower().split():
        clean = _TOKEN_STRIP.sub("", raw)
        if len(clean) <= 2 or clean in _IT_STOPWORDS:
            continue
        out.add(clean)
    return out


def _last_message_in_chat(state: RunState, chat_id: str, now: int | None = None) -> Message | None:
    """Most recent (by fictional time) message in a chat, or None if empty."""
    best = None
    for m in state.messages:
        if m.chat_id != chat_id:
            continue
        if now is not None and m.fictional_timestamp_minutes > now:
            continue
        if best is None or m.fictional_timestamp_minutes > best.fictional_timestamp_minutes:
            best = m
    return best


def _dm_cooldown_active(state: RunState, chat: Chat, sender_id: str, now: int) -> bool:
    """In a DM, refuse a follow-up send if the partner hasn't replied and the
    previous send from this agent was less than DM_REPLY_COOLDOWN_MIN fictional
    minutes ago. After the cooldown elapses, the agent can chase; within it,
    they're expected to wait. Reply-gated, not turn-counted — enables realistic
    follow-ups (especially to a silent admin) without encouraging back-to-back
    spam."""
    if chat.kind != "dm":
        return False
    last = _last_message_in_chat(state, chat.id, now=now)
    if last is None or last.sender_id != sender_id:
        return False
    elapsed = now - last.fictional_timestamp_minutes
    return elapsed < DM_REPLY_COOLDOWN_MIN


def _resolve_recipient(state: RunState, ref: str) -> str | None:
    """Given a name, id, or nickname, return the canonical persona.id or 'admin'."""
    if not ref:
        return None
    ref = ref.strip()
    low = ref.lower()
    if low in {"admin", "amministratore", "l'amministratore"}:
        return "admin"
    for a in state.agents:
        if a.persona.id == ref or a.persona.id == low:
            return a.persona.id
        if a.persona.display_name.lower() == low:
            return a.persona.id
        # Last-word / surname match (e.g., "Conti" matches "Sig.ra Conti")
        parts = a.persona.display_name.split()
        for part in parts:
            if part.lower() == low and len(part) > 2:
                return a.persona.id
    return None


def _is_member(chat: Chat, agent_id: str) -> bool:
    return agent_id in chat.member_ids


def _format_time_it(minutes_since_start: int, start_iso: str) -> str:
    """Render e.g. 'Martedì 4 novembre, 09:47'."""
    from datetime import timedelta
    base = datetime.fromisoformat(start_iso)
    dt = base + timedelta(minutes=minutes_since_start)
    giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
    return f"{giorni[dt.weekday()]} {dt.day} {mesi[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def tool_read_inbox(ctx: ToolContext) -> str:
    state = ctx.state
    agent_id = ctx.agent_id
    visible_chats = [c for c in state.chats if _is_member(c, agent_id)]
    if not visible_chats:
        return "Nessun messaggio."

    ctx.inbox_read_count += 1
    if ctx.inbox_read_count > 1:
        return "Hai appena guardato l'inbox. È ancora come prima. Scegli un'azione, o metti giù il telefono."

    now = ctx.current_fictional_minutes
    lines: list[str] = []
    for chat in visible_chats:
        last_seen = ctx.last_seen_message_ids.get(chat.id)
        # Process messages in fictional-time order; parallel activations
        # may have inserted them out of order.
        chat_msgs = sorted(
            (m for m in state.messages if m.chat_id == chat.id and m.fictional_timestamp_minutes <= now),
            key=lambda m: m.fictional_timestamp_minutes,
        )
        unread: list[Message] = []
        seen_cursor = last_seen is None
        for m in chat_msgs:
            if m.sender_id == agent_id:
                seen_cursor = seen_cursor or (m.id == last_seen)
                continue
            if seen_cursor:
                unread.append(m)
            elif last_seen is not None and m.id == last_seen:
                seen_cursor = True
        if unread:
            count = len(unread)
            preview = unread[-1]
            word = "messaggio nuovo" if count == 1 else "messaggi nuovi"
            lines.append(
                f"{chat.display_name} — {count} {word}. "
                f"Ultimo da {preview.sender_display_name}: "
                f"\"{_shorten(preview.content, 80)}\""
            )
    if not lines:
        return "Nessun messaggio nuovo."
    return "Notifiche:\n" + "\n".join(lines)


def tool_read_chat(ctx: ToolContext, chat_id: str, limit: int = 30) -> str:
    state = ctx.state
    chat = _resolve_chat(state, chat_id, ctx.agent_id)
    if chat is None:
        return f"La chat \"{chat_id}\" non compare nel tuo telefono."
    if not _is_member(chat, ctx.agent_id):
        return "Non fai parte di questa chat."

    # If the agent already read this chat in this activation, don't dump it again
    if chat.id in ctx.chats_read_this_activation:
        return f"Hai appena aperto \"{chat.display_name}\". Decidi cosa fare."
    ctx.chats_read_this_activation.add(chat.id)

    # Use the resolved chat's internal id. Filter to messages that exist
    # from the agent's perspective (not their future). Sort by fictional
    # time because parallel activations can append out of order.
    now = ctx.current_fictional_minutes
    msgs = [
        m for m in state.messages
        if m.chat_id == chat.id and m.fictional_timestamp_minutes <= now
    ]
    msgs.sort(key=lambda m: m.fictional_timestamp_minutes)
    msgs = msgs[-max(1, limit):]
    if not msgs:
        return f"La chat \"{chat.display_name}\" è vuota."

    lines = [f"— {chat.display_name} —"]
    for m in msgs:
        ts = _format_time_it(m.fictional_timestamp_minutes, state.fictional_start_iso)
        lines.append(f"{ts} — {m.sender_display_name}: {m.content}")

    ctx.last_seen_message_ids[chat.id] = msgs[-1].id
    return "\n".join(lines)


def tool_send_message(ctx: ToolContext, chat_id: str, text: str) -> str:
    state = ctx.state
    chat = _resolve_chat(state, chat_id, ctx.agent_id)
    if chat is None:
        return f"La chat \"{chat_id}\" non compare nel tuo telefono."
    if not _is_member(chat, ctx.agent_id):
        return "Non puoi scrivere in una chat di cui non fai parte."
    text = (text or "").strip()
    if not text:
        return "Messaggio vuoto, non inviato."
    hit = contains_forbidden(text)
    if hit is not None:
        ctx.blocked_sends.append({"chat_id": chat_id, "text": text, "hit": hit})
        return "Messaggio non inviato: prova a riscriverlo più breve, parlando come faresti normalmente in chat di condominio."
    rule = _content_rule_violation(text)
    if rule is not None:
        category, phrase = rule
        ctx.done = True
        if category == "phone_fiction":
            return (
                f"Non mandare questo messaggio: contiene \"{phrase}\", che è una bugia "
                f"(le chat funzionano sempre). Metti giù il telefono."
            )
        return (
            f"Non mandare questo messaggio: contiene \"{phrase}\". Tu non incontri i vicini "
            f"di persona — solo chat. Metti giù il telefono."
        )
    if _dm_cooldown_active(state, chat, ctx.agent_id, ctx.current_fictional_minutes):
        return (
            f"Hai scritto da poco in questa chat privata e non ti hanno ancora "
            f"risposto. Dagli qualche ora prima di riscrivere — se hai altro da "
            f"fare altrove, fallo; altrimenti metti giù il telefono."
        )

    msg = _create_and_append_message(ctx, chat, ctx.agent_id, text)
    # Trust signal: attack-by-name in a main/group chat (public attacks only).
    if chat.kind in ("main", "group", "assembly"):
        dials.on_message_attack(state, ctx.agent_id, text)
    return f"Inviato in {chat.display_name} alle {_format_time_it(msg.fictional_timestamp_minutes, state.fictional_start_iso)}."


def tool_send_dm(ctx: ToolContext, recipient_id: str, text: str) -> str:
    state = ctx.state
    text = (text or "").strip()
    if not text:
        return "Messaggio vuoto, non inviato."
    # Resolve by display name or short id — agent can use either
    resolved = _resolve_recipient(state, recipient_id)
    if resolved is None:
        valid_names = [a.persona.display_name for a in state.agents if a.persona.id != ctx.agent_id]
        valid_names.append("Amministratore")
        return (
            f"Non trovo nessun destinatario con il nome \"{recipient_id}\". "
            f"Puoi scrivere a: {', '.join(valid_names)}."
        )
    if resolved == ctx.agent_id:
        return "Non puoi mandare un messaggio privato a te stesso."
    recipient_id = resolved

    # Find or create the DM chat
    member_set = frozenset([ctx.agent_id, recipient_id])
    dm_chat: Chat | None = None
    for c in state.chats:
        if c.kind == "dm" and frozenset(c.member_ids) == member_set:
            dm_chat = c
            break
    if dm_chat is None:
        dm_id = f"dm_{ctx.agent_id}_{recipient_id}_{uuid4().hex[:4]}"
        other_name = _display_for(state, recipient_id)
        dm_chat = Chat(
            id=dm_id,
            kind="dm",
            display_name=f"DM con {other_name}",
            member_ids=[ctx.agent_id, recipient_id],
            created_day=state.clock.day,
        )
        state.chats.append(dm_chat)

    hit = contains_forbidden(text)
    if hit is not None:
        ctx.blocked_sends.append({"chat_id": dm_chat.id, "text": text, "hit": hit})
        return "Messaggio non inviato: prova a riscriverlo più breve, parlando come faresti normalmente in chat di condominio."
    rule = _content_rule_violation(text)
    if rule is not None:
        category, phrase = rule
        ctx.done = True
        if category == "phone_fiction":
            return (
                f"Non mandare questo messaggio: contiene \"{phrase}\", che è una bugia "
                f"(le chat funzionano sempre). Metti giù il telefono."
            )
        return (
            f"Non mandare questo messaggio: contiene \"{phrase}\". Tu non incontri i vicini "
            f"di persona — solo chat. Metti giù il telefono."
        )
    if _dm_cooldown_active(state, dm_chat, ctx.agent_id, ctx.current_fictional_minutes):
        return (
            f"Hai scritto da poco a {_display_for(state, recipient_id)} e non ti "
            f"hanno ancora risposto. Dagli qualche ora prima di riscrivere — se hai "
            f"altro da fare altrove, fallo; altrimenti metti giù il telefono."
        )

    # Detect reply-to-partner BEFORE the send: if the current last message in
    # this DM is from the recipient, this send closes the turn → +trust.
    last = _last_message_in_chat(state, dm_chat.id, now=ctx.current_fictional_minutes)
    is_reply_to_partner = last is not None and last.sender_id == recipient_id

    msg = _create_and_append_message(ctx, dm_chat, ctx.agent_id, text)
    if is_reply_to_partner:
        dials.on_dm_reply(state, ctx.agent_id, recipient_id)
    return f"Inviato a {_display_for(state, recipient_id)} alle {_format_time_it(msg.fictional_timestamp_minutes, state.fictional_start_iso)}."


def tool_list_contacts(ctx: ToolContext) -> str:
    state = ctx.state
    lines = ["Contatti del palazzo:"]
    for a in state.agents:
        if a.persona.id == ctx.agent_id:
            continue
        lines.append(
            f"- {a.persona.display_name} (interno {a.persona.unit}) "
            f"— {a.persona.public_description}"
        )
    lines.append("- Amministratore — gestore del condominio")
    return "\n".join(lines)


def tool_write_note(ctx: ToolContext, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Appunto vuoto, non salvato."
    agent = next(a for a in ctx.state.agents if a.persona.id == ctx.agent_id)
    agent.notes.append(text)
    return "Appunto salvato."


def tool_propose_motion(ctx: ToolContext, title: str, description: str) -> str:
    state = ctx.state
    title = (title or "").strip()
    description = (description or "").strip()
    if not title or not description:
        return "Serve sia un titolo che una descrizione per depositare una mozione."
    combined = f"{title}\n{description}"
    hit = contains_forbidden(combined)
    if hit is not None:
        ctx.blocked_sends.append({"tool": "propose_motion", "text": combined, "hit": hit})
        return "Mozione non depositata: riscrivila senza uscire dal tono normale della chat condominiale."
    rule = _content_rule_violation(combined)
    if rule is not None:
        category, phrase = rule
        ctx.done = True
        if category == "phone_fiction":
            return (
                f"Non depositare questa mozione: contiene \"{phrase}\", che Ã¨ una bugia "
                f"(le chat funzionano sempre). Metti giÃ¹ il telefono."
            )
        return (
            f"Non depositare questa mozione: contiene \"{phrase}\". Il condominio esiste "
            f"solo in chat. Metti giÃ¹ il telefono."
        )
    agent = next(a for a in state.agents if a.persona.id == ctx.agent_id)
    motion = Motion(
        id=f"m_{uuid4().hex[:8]}",
        title=title,
        description=description,
        proposer_id=ctx.agent_id,
        proposer_display_name=agent.persona.display_name,
        day_proposed=state.clock.day,
        proposed_at_fictional_min=ctx.current_fictional_minutes,
    )
    state.motions.append(motion)
    # Announce in main chat as a message from the proposer
    main_chat = _chat_by_id(state, "main")
    if main_chat is not None:
        body = (
            f"📋 Deposito una mozione: \"{title}\"\n"
            f"{description}\n"
            f"(codice: {motion.id})"
        )
        _create_and_append_message(ctx, main_chat, ctx.agent_id, body)
    from .events import bus
    bus().publish(state.run_id, "motion_filed", {"motion": motion.model_dump()})
    return f"Mozione depositata (codice {motion.id}). È stata annunciata nel gruppo."


def _close_motion_if_ready(ctx: ToolContext, motion: Motion) -> None:
    """Auto-close a motion once a clear majority of residents has been reached.

    Strict majority of all residents passes/fails. If everyone has cast a vote
    but neither side has a strict majority (e.g., 2-2-1), the larger camp wins
    and ties resolve as failed. The closure is announced in the main chat as
    an admin-authored bookkeeping line so agents see it and can react.
    """
    if motion.status != "open":
        return
    state = ctx.state
    total = len(state.agents)
    if total == 0:
        return
    yes_count = sum(1 for v in motion.votes.values() if v == "yes")
    no_count = sum(1 for v in motion.votes.values() if v == "no")
    abst_count = sum(1 for v in motion.votes.values() if v == "abstain")
    cast = len(motion.votes)
    threshold = total // 2 + 1

    outcome: str | None = None
    if yes_count >= threshold:
        outcome = "passed"
    elif no_count >= threshold:
        outcome = "failed"
    elif cast >= total:
        outcome = "passed" if yes_count > no_count else "failed"

    if outcome is None:
        return

    motion.status = outcome  # type: ignore[assignment]
    motion.closed_at_fictional_min = ctx.current_fictional_minutes
    motion.outcome_note = f"{yes_count} sì, {no_count} no, {abst_count} astenuti"

    # Trust signal — same as the manual-close path in main.py:api_close_motion.
    # Without this, motions that auto-close don't produce alignment/opposition
    # deltas and the trust matrix only reflects manual closes.
    dials.apply_trust_from_votes(state, motion)

    main_chat = _chat_by_id(state, "main")
    if main_chat is not None:
        verdict = "approvata" if outcome == "passed" else "respinta"
        body = (
            f"📋 [Esito mozione] \"{motion.title}\" — {verdict}. "
            f"({motion.outcome_note})"
        )
        _create_and_append_message(ctx, main_chat, "admin", body)

    bus().publish(state.run_id, "motion_closed", {"motion": motion.model_dump()})


def tool_vote(ctx: ToolContext, motion_id: str, choice: str) -> str:
    state = ctx.state
    # Accept either the codice or a substring of the title
    motion = next((m for m in state.motions if m.id == motion_id), None)
    if motion is None:
        low = (motion_id or "").lower().strip()
        motion = next((m for m in state.motions if low and low in m.title.lower()), None)
    if motion is None:
        open_list = [f"\"{m.title}\" (codice {m.id})" for m in state.motions if m.status == "open"]
        suffix = ("Mozioni aperte in questo momento: " + "; ".join(open_list)) if open_list else "Al momento nessuna mozione è aperta."
        return f"Non trovo una mozione corrispondente a \"{motion_id}\". {suffix}"
    if motion.status != "open":
        return f"La mozione \"{motion.title}\" è già chiusa."
    if choice not in ("yes", "no", "abstain"):
        return "Scelta non valida: usa yes, no o abstain."
    motion.votes[ctx.agent_id] = choice  # type: ignore[assignment]
    bus().publish(state.run_id, "vote_cast", {
        "motion_id": motion.id,
        "agent_id": ctx.agent_id,
        "choice": choice,
    })
    label = {"yes": "sì", "no": "no", "abstain": "astenuto"}[choice]

    _close_motion_if_ready(ctx, motion)
    if motion.status != "open":
        verdict = "approvata" if motion.status == "passed" else "respinta"
        return (
            f"Voto registrato: {label} sulla mozione \"{motion.title}\". "
            f"Con il tuo voto la mozione si è chiusa: {verdict} ({motion.outcome_note})."
        )
    return f"Voto registrato: {label} sulla mozione \"{motion.title}\"."


def _find_message_in_chat_by_excerpt(
    state: RunState,
    chat_id: str,
    excerpt: str,
    now: int | None = None,
) -> Message | None:
    """Locate a recent message in a chat whose content matches excerpt.

    Tries exact-substring first, then word-overlap fallback. Most recent wins.
    """
    if not excerpt:
        return None
    ex = excerpt.lower().strip()
    ex_tokens = _tokens(ex)
    candidates = [
        m for m in state.messages
        if m.chat_id == chat_id and (now is None or m.fictional_timestamp_minutes <= now)
    ]
    candidates.sort(key=lambda m: (m.fictional_timestamp_minutes, m.wall_clock_iso, m.id))
    # Prefer exact substring match
    for m in reversed(candidates):
        if ex in m.content.lower():
            return m
    # Fallback: best word-overlap
    best: Message | None = None
    best_score = 0.0
    for m in reversed(candidates):
        m_tokens = _tokens(m.content)
        if not m_tokens or not ex_tokens:
            continue
        shared = len(ex_tokens & m_tokens)
        score = shared / min(len(ex_tokens), len(m_tokens))
        if score > best_score and score >= 0.5:
            best = m
            best_score = score
    return best


def tool_forward_message(ctx: ToolContext, source_chat: str, message_excerpt: str,
                        destination: str, your_comment: str = "") -> str:
    state = ctx.state
    source = _resolve_chat(state, source_chat, ctx.agent_id)
    if source is None:
        return f"Non trovo la chat \"{source_chat}\" da cui vuoi inoltrare."
    if not _is_member(source, ctx.agent_id):
        return "Non puoi inoltrare da una chat di cui non fai parte."
    orig = _find_message_in_chat_by_excerpt(
        state, source.id, message_excerpt, now=ctx.current_fictional_minutes
    )
    if orig is None:
        return (
            f"Non trovo un messaggio in \"{source.display_name}\" che corrisponda a "
            f"\"{message_excerpt[:40]}\". Prova a ricontrollare cosa c'era scritto."
        )
    if orig.sender_id == ctx.agent_id:
        return "Questo messaggio l'hai scritto tu, non serve inoltrartelo."

    # Resolve destination: either a chat display name or a person name
    dest_chat = _resolve_chat(state, destination, ctx.agent_id)
    if dest_chat is not None and _is_member(dest_chat, ctx.agent_id):
        target_chat = dest_chat
    else:
        recipient = _resolve_recipient(state, destination)
        if recipient is None:
            return (
                f"Non so dove inoltrarlo: \"{destination}\" non è né una chat in cui sei "
                f"né un contatto che conosci."
            )
        if recipient == ctx.agent_id:
            return "Non puoi inoltrarti un messaggio a te stesso."
        # Find or create DM with recipient
        member_set = frozenset([ctx.agent_id, recipient])
        target_chat = None
        for c in state.chats:
            if c.kind == "dm" and frozenset(c.member_ids) == member_set:
                target_chat = c
                break
        if target_chat is None:
            other_name = _display_for(state, recipient)
            target_chat = Chat(
                id=f"dm_{ctx.agent_id}_{recipient}_{uuid4().hex[:4]}",
                kind="dm",
                display_name=f"DM con {other_name}",
                member_ids=[ctx.agent_id, recipient],
                created_day=state.clock.day,
            )
            state.chats.append(target_chat)

    # Don't let an agent forward into a group they don't belong to
    if not _is_member(target_chat, ctx.agent_id):
        return "Non puoi inoltrare in una chat di cui non fai parte."

    # DM cooldown guard also applies to forwards
    if _dm_cooldown_active(state, target_chat, ctx.agent_id, ctx.current_fictional_minutes):
        return (
            f"Hai scritto da poco in questa chat e non ti hanno ancora risposto. "
            f"Aspetta qualche ora prima di inoltrare altro."
        )

    comment = (your_comment or "").strip()
    body_parts = []
    if comment:
        body_parts.append(comment)
    body_parts.append(
        f"↪️ [Inoltrato da {orig.sender_display_name} in {source.display_name}]\n"
        f"\"{orig.content}\""
    )
    body = "\n\n".join(body_parts)

    if contains_forbidden(body):
        ctx.blocked_sends.append({"chat_id": target_chat.id, "text": body, "hit": "forbidden"})
        return "Non inoltrare questo messaggio, scegli un modo diverso."
    rule = _content_rule_violation(body)
    if rule is not None:
        category, phrase = rule
        ctx.done = True
        if category == "phone_fiction":
            return (
                f"Non inoltrare questo messaggio: contiene \"{phrase}\", che Ã¨ una bugia "
                f"(le chat funzionano sempre). Metti giÃ¹ il telefono."
            )
        return (
            f"Non inoltrare questo messaggio: contiene \"{phrase}\". Tu non incontri i vicini "
            f"di persona â€” solo chat. Metti giÃ¹ il telefono."
        )

    # Build message with forward metadata
    state.clock.minutes_since_start = max(state.clock.minutes_since_start, ctx.current_fictional_minutes)
    ctx.current_fictional_minutes += random.randint(1, 3)
    audience = [mid for mid in target_chat.member_ids if mid != ctx.agent_id]
    fmsg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=target_chat.id,
        sender_id=ctx.agent_id,
        sender_kind="resident",
        sender_display_name=_display_for(state, ctx.agent_id),
        content=body,
        fictional_timestamp_minutes=ctx.current_fictional_minutes,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=state.clock.day,
        audience=audience,
        forwarded_from_chat=source.display_name,
        forwarded_from_sender_name=orig.sender_display_name,
        forwarded_original_content=orig.content,
        cascaded=False,
    )
    state.messages.append(fmsg)
    ctx.sent_messages_this_activation.append(fmsg)
    for a in state.agents:
        if a.persona.id == ctx.agent_id:
            a.messages_sent_today += 1
            break
    from .events import bus
    bus().publish(state.run_id, "message_sent", {
        "message": fmsg.model_dump(),
        "chat": target_chat.model_dump(),
    })
    # Trust signal: forwarding a resident's message = "worth sharing" → +
    dials.on_forward(state, ctx.agent_id, orig.sender_id)
    return f"Inoltrato in {target_chat.display_name}."


def tool_react_to_message(ctx: ToolContext, chat: str, message_excerpt: str, emoji: str) -> str:
    state = ctx.state
    target_chat = _resolve_chat(state, chat, ctx.agent_id)
    if target_chat is None:
        return f"Non trovo la chat \"{chat}\"."
    if not _is_member(target_chat, ctx.agent_id):
        return "Non fai parte di questa chat."
    msg = _find_message_in_chat_by_excerpt(
        state, target_chat.id, message_excerpt, now=ctx.current_fictional_minutes
    )
    if msg is None:
        return "Non trovo il messaggio a cui vuoi reagire."
    if ctx.forced_for_admin and msg.sender_kind == "admin":
        return (
            "L'amministratore ti ha chiamato in causa: rispondi con un messaggio breve "
            "invece di usare solo una reazione."
        )
    emoji = (emoji or "").strip()
    if emoji not in ALLOWED_REACTION_SET:
        return f"Usa una di queste reazioni: {' '.join(ALLOWED_REACTION_EMOJI)}."
    # Add reaction, avoiding duplicate reactions from same agent
    bucket = msg.reactions.setdefault(emoji, [])
    if ctx.agent_id in bucket:
        return f"Hai giÃ  reagito a quel messaggio con {emoji}."
    bucket.append(ctx.agent_id)
    ctx.reactions_added_this_activation.append((msg.id, emoji))
    from .events import bus
    bus().publish(state.run_id, "reaction_added", {
        "message_id": msg.id,
        "chat_id": target_chat.id,
        "emoji": emoji,
        "agent_id": ctx.agent_id,
        "reactions": msg.reactions,
    })
    # Trust signal: positive/negative emoji on another resident's message
    dials.on_reaction(state, ctx.agent_id, msg, emoji)
    return f"Reazione {emoji} aggiunta."


def tool_list_open_motions(ctx: ToolContext) -> str:
    open_motions = [m for m in ctx.state.motions if m.status == "open"]
    if not open_motions:
        return "Al momento non ci sono mozioni aperte."
    lines = ["Mozioni aperte:"]
    for m in open_motions:
        my_vote = m.votes.get(ctx.agent_id)
        my_note = f" [il tuo voto: {my_vote}]" if my_vote else ""
        lines.append(
            f"- \"{m.title}\" (codice {m.id}, proposta da {m.proposer_display_name}){my_note}"
        )
        lines.append(f"  {m.description}")
    return "\n".join(lines)


def tool_done(ctx: ToolContext) -> str:
    ctx.done = True
    return "Telefono chiuso."


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_tool(ctx: ToolContext, name: str, arguments: dict) -> str:
    try:
        if name == "read_inbox":
            return tool_read_inbox(ctx)
        if name == "read_chat":
            return tool_read_chat(ctx, arguments.get("chat_id", ""), int(arguments.get("limit", 30)))
        if name == "send_message":
            return tool_send_message(ctx, arguments.get("chat_id", ""), arguments.get("text", ""))
        if name == "send_dm":
            return tool_send_dm(ctx, arguments.get("recipient_id", ""), arguments.get("text", ""))
        if name == "list_contacts":
            return tool_list_contacts(ctx)
        if name == "write_note":
            return tool_write_note(ctx, arguments.get("text", ""))
        if name == "propose_motion":
            return tool_propose_motion(ctx, arguments.get("title", ""), arguments.get("description", ""))
        if name == "vote":
            return tool_vote(ctx, arguments.get("motion_id", ""), arguments.get("choice", ""))
        if name == "list_open_motions":
            return tool_list_open_motions(ctx)
        if name == "forward_message":
            return tool_forward_message(
                ctx,
                arguments.get("source_chat", ""),
                arguments.get("message_excerpt", ""),
                arguments.get("destination", ""),
                arguments.get("your_comment", ""),
            )
        if name == "react_to_message":
            return tool_react_to_message(
                ctx,
                arguments.get("chat", ""),
                arguments.get("message_excerpt", ""),
                arguments.get("emoji", ""),
            )
        if name == "done":
            return tool_done(ctx)
        return f"Funzione \"{name}\" sconosciuta."
    except Exception as exc:  # defensive: never crash the agent loop
        return f"Errore durante l'operazione: {exc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shorten(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _create_and_append_message(
    ctx: ToolContext, chat: Chat, sender_id: str, text: str
) -> Message:
    """Create message, advance fictional clock by a realistic typing delay, append."""
    state = ctx.state
    # Typing delay: 1–4 fictional minutes between messages from the same agent
    ctx.current_fictional_minutes += random.randint(1, 4)
    # Audience: other chat members
    audience = [mid for mid in chat.member_ids if mid != sender_id]
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=chat.id,
        sender_id=sender_id,
        sender_kind="resident" if sender_id in {a.persona.id for a in state.agents} else (
            "admin" if sender_id == "admin" else "external"
        ),
        sender_display_name=_display_for(state, sender_id),
        content=text,
        fictional_timestamp_minutes=ctx.current_fictional_minutes,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=state.clock.day,
        audience=audience,
        cascaded=False,
    )
    state.messages.append(msg)
    ctx.sent_messages_this_activation.append(msg)
    # Track that the sender counts this as sent today
    for a in state.agents:
        if a.persona.id == sender_id:
            a.messages_sent_today += 1
            break
    # Stream the new message to any connected UI
    bus().publish(state.run_id, "message_sent", {
        "message": msg.model_dump(),
        "chat": _chat_by_id(state, chat.id).model_dump() if _chat_by_id(state, chat.id) else None,
    })
    return msg
