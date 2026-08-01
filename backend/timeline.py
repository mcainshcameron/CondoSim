"""The run's single source of truth for message ordering.

Why this module exists
----------------------
v1 stamped fictional timestamps at four independent call sites, each
deriving "now" from a different value:

  * `tools._create_and_append_message` advanced `ctx.current_fictional_minutes`
  * `tools.tool_forward_message` advanced it differently
  * `main._append_admin_message` used `state.clock.minutes_since_start`
  * the scheduler clamped agent targets against `state.clock` too

`state.clock` only caught up to the newest message *after* an activation
finished. So an admin message posted while an agent was mid-activation got
stamped from a stale clock and landed *before* messages that had already
been streamed to the browser. The UI re-sorts on every merge, so the
admin's own message visibly jumped backwards up the transcript — and an
agent could appear to answer something posted after them.

The fix is to make ordering a property of one allocator:

  * `allocate_minute` never returns a minute earlier than the newest message
    already in the run. Causality can't invert.
  * `next_seq` hands out a per-run monotonic counter. Messages that share a
    fictional minute (two neighbours replying "at 09:12") keep the order they
    were actually generated in.

Sort key everywhere — backend, frontend, analysis — is
`(fictional_timestamp_minutes, seq)`. That is a *total* order, so a re-sort
after a refetch can never reshuffle the transcript.
"""
from __future__ import annotations

from .models import Message, RunState


def latest_minute(state: RunState) -> int:
    """Fictional minute of the newest message in the run, or -1 if empty."""
    latest = -1
    for m in state.messages:
        if m.fictional_timestamp_minutes > latest:
            latest = m.fictional_timestamp_minutes
    return latest


def allocate_minute(state: RunState, desired: int) -> int:
    """Clamp `desired` so it is never earlier than the run's newest message.

    Ties are allowed and expected — two residents can plausibly post in the
    same minute. `next_seq` breaks them in generation order.
    """
    return max(int(desired), latest_minute(state))


def next_seq(state: RunState) -> int:
    """Per-run monotonic counter used as the tiebreaker in the sort key."""
    state.next_seq += 1
    return state.next_seq


def sort_key(msg: Message) -> tuple[int, int]:
    return (msg.fictional_timestamp_minutes, msg.seq)


def in_order(messages: list[Message]) -> list[Message]:
    """Messages in canonical transcript order."""
    return sorted(messages, key=sort_key)


def backfill_seq(state: RunState) -> None:
    """Assign `seq` to any message that predates the field.

    Runs on load so a run started before this change keeps a stable order
    instead of collapsing to an all-zero tiebreaker. Existing rows are
    ordered by (minute, wall-clock, id) — the best reconstruction available —
    and `next_seq` is advanced past the highest value handed out.
    """
    missing = [m for m in state.messages if not m.seq]
    if not missing:
        state.next_seq = max(state.next_seq, max((m.seq for m in state.messages), default=0))
        return
    ordered = sorted(
        state.messages,
        key=lambda m: (m.fictional_timestamp_minutes, m.wall_clock_iso or "", m.id),
    )
    counter = 0
    for m in ordered:
        counter += 1
        if not m.seq:
            m.seq = counter
    state.next_seq = max(state.next_seq, counter)
