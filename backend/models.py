"""Pydantic schemas for the simulation.

Fictional vs wall-clock time: every user-visible timestamp is *fictional*
(the in-world 14-day calendar). wall_clock_timestamp is runtime metadata
that never reaches an agent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

Responsiveness = Literal["fast", "medium", "slow"]
TimeOfDay = Literal["morning", "evening", "scattered"]
OwnerKind = Literal["self", "absentee_landlord", "family_proxy", "commercial_stake"]
SenderKind = Literal["resident", "admin", "external"]
ChatKind = Literal["main", "dm", "group", "neighborhood", "assembly"]


class FictionalClock(BaseModel):
    """The in-world clock. Day 1..14, hour 0..23, minute 0..59."""
    day: int = 1
    # We track elapsed minutes since Day 1 00:00 for easy arithmetic.
    minutes_since_start: int = 0

    def current_dt(self, start_date: datetime) -> datetime:
        return start_date + timedelta(minutes=self.minutes_since_start)

    def format_it(self, start_date: datetime) -> str:
        dt = self.current_dt(start_date)
        # Italian day names
        giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
        mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
        return f"{giorni[dt.weekday()]} {dt.day} {mesi[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


class ExternalContact(BaseModel):
    id: str
    display_name: str
    role_description: str  # e.g., "Vicino, palazzo di fronte"


class Persona(BaseModel):
    """Public-facing data about a resident. Visible to all other agents."""
    id: str  # e.g. "conti"
    display_name: str  # e.g. "Sig.ra Conti"
    unit: str  # e.g. "2B"
    public_description: str  # short in-fiction bio visible via list_contacts
    responsiveness: Responsiveness
    time_of_day: TimeOfDay
    millesimi: int


class OwnerBrief(BaseModel):
    """The resident's principal-agent relationship to the unit.

    Personality and stakes live in the scenario's souls/{id}.md and
    memory_seeds/{id}.md; `kind` only drives the "Rapporto con l'appartamento"
    line in the system prompt preamble.
    """
    kind: OwnerKind


class Agent(BaseModel):
    """A cast member: persona + owner + model + runtime state."""
    persona: Persona
    owner: OwnerBrief
    model: str
    starting_wallet_eur: int
    notes: list[str] = Field(default_factory=list)  # agent's own scratchpad
    messages_sent_today: int = 0
    # Admin-authored extra goal injected into the system prompt as part of
    # the agent's own thinking (never framed as "admin said"). Empty string
    # when unset.
    admin_goal: str = ""


class Message(BaseModel):
    id: str
    chat_id: str
    sender_id: str  # persona.id, "admin", or external contact id
    sender_kind: SenderKind
    sender_display_name: str
    content: str
    fictional_timestamp_minutes: int  # minutes since Day 1 00:00
    wall_clock_iso: str  # runtime metadata, never sent to agents
    day: int
    audience: list[str] = Field(default_factory=list)
    # If this message is a forward (leak) from another chat, record the origin
    forwarded_from_chat: str | None = None
    forwarded_from_sender_name: str | None = None
    forwarded_original_content: str | None = None
    # Emoji reactions: map emoji -> list of agent_ids who reacted
    reactions: dict[str, list[str]] = Field(default_factory=dict)


class Chat(BaseModel):
    id: str
    kind: ChatKind
    display_name: str  # e.g. "Condominio Via Garibaldi"
    member_ids: list[str]  # persona ids or "admin" or external ids
    created_day: int = 1


class Motion(BaseModel):
    """A formal proposal submitted for voting by residents."""
    id: str
    title: str
    description: str
    proposer_id: str  # resident persona.id or "admin"
    proposer_display_name: str
    day_proposed: int
    proposed_at_fictional_min: int
    status: Literal["open", "passed", "failed", "expired"] = "open"
    # agent_id -> "yes" | "no" | "abstain"
    votes: dict[str, Literal["yes", "no", "abstain"]] = Field(default_factory=dict)
    closed_at_fictional_min: int | None = None
    outcome_note: str | None = None


class RunState(BaseModel):
    """The complete simulation state for a single run."""
    run_id: str
    building_id: str
    started_at_iso: str
    fictional_start_iso: str  # in-world calendar anchor (Day 1 00:00)
    clock: FictionalClock = Field(default_factory=FictionalClock)
    agents: list[Agent]
    external_contacts: list[ExternalContact] = Field(default_factory=list)
    chats: list[Chat]
    messages: list[Message] = Field(default_factory=list)
    motions: list[Motion] = Field(default_factory=list)
    # trust[speaker_id][listener_id] — nested dict for JSON sanity
    trust: dict[str, dict[str, float]] = Field(default_factory=dict)
    ended: bool = False
