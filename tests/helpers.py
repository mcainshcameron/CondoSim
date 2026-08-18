"""Shared test helpers that aren't fixtures.

`append_message` exists because three of the review's test modules each need
pre-existing transcript content — a neighbour's line to react to, an admin
announcement to be owed a reply to, a vote tally to prove is ignored — without
paying for an activation to produce it. Written independently they were three
copies of the same twenty lines, and a copy is exactly how the four
timestamping sites in v1 drifted apart. One definition, so a test can never
be the thing that breaks the total order it is asserting on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from backend import timeline
from backend.models import Message, RunState


def append_message(
    state: RunState,
    sender_id: str,
    sender_kind: str,
    text: str,
    *,
    chat_id: str = "main",
    minute: int | None = None,
    cascaded: bool = True,
    bookkeeping: bool = False,
) -> Message:
    """Drop a message into the transcript without running an activation.

    Goes through `timeline.allocate_minute` / `timeline.next_seq` like every
    production creation site. `minute` defaults to just after the clock;
    pass it explicitly when the test is about *where* in the day something
    landed. `cascaded=True` by default so a planted message does not
    accidentally seed `pending_admin_reactions` in tests that aren't about
    forcing.
    """
    chat = next(c for c in state.chats if c.id == chat_id)
    target = state.clock.minutes_since_start + 1 if minute is None else minute
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_kind=sender_kind,
        sender_display_name=sender_id,
        content=text,
        fictional_timestamp_minutes=timeline.allocate_minute(state, target),
        seq=timeline.next_seq(state),
        # Runtime metadata only — never shown to an agent, only a tiebreak
        # for `timeline.backfill_seq` on pre-`seq` runs.
        wall_clock_iso=datetime.now(timezone.utc).isoformat(),
        day=state.clock.day,
        audience=[m for m in chat.member_ids if m != sender_id],
        cascaded=cascaded,
        bookkeeping=bookkeeping,
    )
    state.messages.append(msg)
    return msg
