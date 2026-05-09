"""Ambient in-world events that make the building feel less hermetic."""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime
from uuid import uuid4

from .config import DATA_DIR, WORLD_EVENT_PROBABILITY
from .events import bus
from .models import ExternalContact, Message, RunState


_BACHECA_ID = "bacheca"


def _stable_seed(run_id: str, day: int) -> int:
    raw = f"{run_id}:world:{day}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def _load_events() -> list[dict]:
    path = DATA_DIR / "world_events.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def maybe_inject_world_event(state: RunState) -> Message | None:
    """Occasionally post a small bacheca notice to the main chat.

    The event is deterministic per run/day so a restarted background task
    does not choose a different notice for the same fictional morning.
    """
    day = state.clock.day
    if any(m.day == day and m.sender_id == _BACHECA_ID for m in state.messages):
        return None

    events = _load_events()
    if not events:
        return None

    rng = random.Random(_stable_seed(state.run_id, day))
    if rng.random() >= WORLD_EVENT_PROBABILITY:
        return None

    main_chat = next((c for c in state.chats if c.kind == "main"), None)
    if main_chat is None:
        return None

    if not any(c.id == _BACHECA_ID for c in state.external_contacts):
        state.external_contacts.append(
            ExternalContact(
                id=_BACHECA_ID,
                display_name="Bacheca del palazzo",
                role_description="Avvisi pratici del condominio",
            )
        )

    event = rng.choice(events)
    resident_ids = [a.persona.id for a in state.agents]
    fictional_min = max(state.clock.minutes_since_start, (day - 1) * 24 * 60 + 8 * 60)
    fictional_min += rng.randint(4, 35)
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=main_chat.id,
        sender_id=_BACHECA_ID,
        sender_kind="external",
        sender_display_name="Bacheca del palazzo",
        content=str(event.get("text") or "").strip(),
        fictional_timestamp_minutes=fictional_min,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=day,
        audience=resident_ids,
        # Ambient notices are context, not admin messages owed a guaranteed
        # reaction. Agents will see them organically when their turn reaches
        # the notice timestamp.
        cascaded=True,
    )
    state.messages.append(msg)
    bus().publish(state.run_id, "message_sent", {
        "message": msg.model_dump(),
        "chat": main_chat.model_dump(),
    })
    return msg
