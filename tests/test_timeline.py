"""Ordering invariants — the class of bug that made the transcript reshuffle.

The user-visible symptom was messages jumping around: the admin's own post
sliding above residents' replies after a refetch, and residents appearing to
answer things posted after them. Both reduce to "the transcript had no total
order and timestamps could go backwards".
"""
from __future__ import annotations

import pytest

from backend import timeline
from backend.models import Message


def _msg(mid: str, minute: int, seq: int = 0, sender: str = "conti") -> Message:
    return Message(
        id=mid,
        chat_id="main",
        sender_id=sender,
        sender_kind="resident",
        sender_display_name=sender.title(),
        content=f"contenuto {mid}",
        fictional_timestamp_minutes=minute,
        seq=seq,
        wall_clock_iso="2026-01-01T00:00:00Z",
        day=1,
    )


def test_allocate_never_goes_backwards(run_state_factory):
    state = run_state_factory()
    state.messages.append(_msg("a", 600, seq=1))
    # A caller deriving "now" from a stale clock asks for an earlier minute.
    assert timeline.allocate_minute(state, 500) == 600


def test_allocate_respects_a_later_desired_minute(run_state_factory):
    state = run_state_factory()
    state.messages.append(_msg("a", 600, seq=1))
    assert timeline.allocate_minute(state, 640) == 640


def test_allocate_allows_ties(run_state_factory):
    """Two neighbours posting in the same minute is realistic; `seq` keeps
    them ordered rather than forcing artificial minute gaps."""
    state = run_state_factory()
    state.messages.append(_msg("a", 600, seq=1))
    assert timeline.allocate_minute(state, 600) == 600


def test_seq_is_monotonic(run_state_factory):
    state = run_state_factory()
    handed_out = [timeline.next_seq(state) for _ in range(5)]
    assert handed_out == sorted(handed_out)
    assert len(set(handed_out)) == 5


def test_sort_is_a_total_order_under_ties(run_state_factory):
    """Same minute, different seq — order must be stable and generation-order."""
    state = run_state_factory()
    state.messages = [
        _msg("third", 600, seq=3),
        _msg("first", 600, seq=1),
        _msg("second", 600, seq=2),
    ]
    assert [m.id for m in timeline.in_order(state.messages)] == ["first", "second", "third"]
    # Re-sorting an already-sorted list must not change it (idempotent) —
    # this is what stops the UI reshuffling on every refetch/merge.
    once = timeline.in_order(state.messages)
    assert timeline.in_order(once) == once


def test_backfill_seq_repairs_legacy_runs(run_state_factory):
    """Runs saved before Message.seq existed load with seq=0 everywhere."""
    state = run_state_factory()
    state.messages = [
        _msg("b", 610, seq=0),
        _msg("a", 600, seq=0),
        _msg("c", 620, seq=0),
    ]
    state.next_seq = 0
    timeline.backfill_seq(state)

    seqs = [m.seq for m in state.messages]
    assert all(s > 0 for s in seqs), "every legacy message gets a seq"
    assert len(set(seqs)) == 3, "seqs are unique"
    assert [m.id for m in timeline.in_order(state.messages)] == ["a", "b", "c"]
    assert state.next_seq >= max(seqs), "allocator won't reissue a used seq"


def test_backfill_is_idempotent(run_state_factory):
    state = run_state_factory()
    state.messages = [_msg("a", 600), _msg("b", 610)]
    timeline.backfill_seq(state)
    first = [(m.id, m.seq) for m in state.messages]
    timeline.backfill_seq(state)
    assert [(m.id, m.seq) for m in state.messages] == first


@pytest.fixture
def run_state_factory():
    from backend import building

    def _make():
        return building.build_run_state(building_id="001", opening_text="apertura")

    return _make
