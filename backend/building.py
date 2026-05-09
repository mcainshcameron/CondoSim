"""Generic building loader.

A building is pure data under `data/buildings/{id}/`:
  building.json     — id, name, fictional_start_iso
  residents.json    — cast: list of personas + owner_kind + wallet
  souls/*.md        — first-person SOUL per resident
  memory_seeds/*.md — day-1 MEMORY seed per resident

This module knows nothing about specific buildings. Any building id maps
to a directory under DATA_DIR/buildings/; the code is scenario-free.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from .config import DATA_DIR, DEFAULT_AGENT_MODEL
from .models import (
    Agent,
    Chat,
    ExternalContact,
    FictionalClock,
    Message,
    OwnerBrief,
    OwnerKind,
    Persona,
    RunState,
)

BUILDINGS_DIR = DATA_DIR / "buildings"
_BUILDING_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Config schemas (validate the JSON on disk)
# ---------------------------------------------------------------------------

class BuildingConfig(BaseModel):
    id: str
    name: str
    fictional_start_iso: str


class ResidentTemplate(BaseModel):
    persona: Persona
    owner_kind: OwnerKind
    starting_wallet_eur: int
    model: str | None = None  # defaults to DEFAULT_AGENT_MODEL


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def building_dir(building_id: str) -> Path:
    building_id = (building_id or "").strip()
    if not _BUILDING_ID_RE.fullmatch(building_id):
        raise ValueError("building_id non valido")
    return BUILDINGS_DIR / building_id


def souls_dir(building_id: str) -> Path:
    return building_dir(building_id) / "souls"


def memory_seeds_dir(building_id: str) -> Path:
    return building_dir(building_id) / "memory_seeds"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_building(building_id: str) -> BuildingConfig:
    path = building_dir(building_id) / "building.json"
    if not path.exists():
        raise ValueError(f"Building {building_id} not found")
    return BuildingConfig.model_validate_json(path.read_text(encoding="utf-8"))


def load_residents(building_id: str) -> list[Agent]:
    path = building_dir(building_id) / "residents.json"
    if not path.exists():
        raise ValueError(f"Residents for building {building_id} not found")
    raw = json.loads(path.read_text(encoding="utf-8"))
    templates = [ResidentTemplate.model_validate(r) for r in raw]
    return [
        Agent(
            persona=t.persona,
            owner=OwnerBrief(kind=t.owner_kind),
            model=t.model or DEFAULT_AGENT_MODEL,
            starting_wallet_eur=t.starting_wallet_eur,
        )
        for t in templates
    ]


def list_buildings() -> list[str]:
    if not BUILDINGS_DIR.exists():
        return []
    return sorted(p.name for p in BUILDINGS_DIR.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Run construction
# ---------------------------------------------------------------------------

def build_run_state(building_id: str, opening_text: str) -> RunState:
    """Start a new run in the given building. The admin's opening_text is
    the inciting event — whatever they type is what the building wakes up
    to. No canned scenario."""
    text = (opening_text or "").strip()
    if not text:
        raise ValueError("opening_text is required — the admin's first message is the scenario")

    cfg = load_building(building_id)
    cast = load_residents(building_id)
    resident_ids = [a.persona.id for a in cast]

    run_id = f"run_{uuid4().hex[:8]}"
    now_iso = datetime.utcnow().isoformat() + "Z"

    main_chat = Chat(
        id="main",
        kind="main",
        display_name=cfg.name,
        member_ids=[*resident_ids, "admin"],
    )

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
        audience=resident_ids,
        cascaded=False,
    )

    # Neutral starting trust matrix — relationships accrue in MEMORY, not here.
    trust = {aid: {other: 0.0 for other in resident_ids if other != aid} for aid in resident_ids}

    return RunState(
        run_id=run_id,
        building_id=building_id,
        started_at_iso=now_iso,
        fictional_start_iso=cfg.fictional_start_iso,
        clock=FictionalClock(day=1, minutes_since_start=0),
        agents=[a.model_copy(deep=True) for a in cast],
        external_contacts=[],
        chats=[main_chat],
        messages=[opening_msg],
        trust=trust,
    )
