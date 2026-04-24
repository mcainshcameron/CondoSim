"""Reaction-cascade scheduler.

Implements the algorithm from GDD §Pacing / Scheduler:
- Messages trigger audience-wide engagement rolls.
- Engaged agents enter a fictional-time priority queue with a delay drawn
  from their responsiveness profile.
- Agents activate in fictional-time order. Their new messages re-cascade,
  bounded by depth.

Everything here is wall-clock-free: all timing is fictional minutes since
Day 1 00:00.
"""
from __future__ import annotations

import asyncio
import heapq
import random
from dataclasses import dataclass

import asyncio

from . import memory
from .agent import activate_agent
from .config import (
    CASCADE_MAX_DEPTH,
    DAY_END_HOUR,
    DAY_START_HOUR,
    PER_AGENT_DAILY_SOFT_BUDGET,
)
from .events import bus
from .logging_utils import log, log_error
from .models import Message, RunState
from .storage import save_run


BATCH_FICTIONAL_WINDOW_MIN = 60  # pull queue items within this many fictional minutes of the head


RESPONSIVENESS_DELAY_MIN: dict[str, tuple[int, int]] = {
    "fast": (5, 30),
    "medium": (20, 120),
    "slow": (60, 360),
}

RESPONSIVENESS_BASE_PROB: dict[str, float] = {
    "fast": 0.85,
    "medium": 0.6,
    "slow": 0.35,
}

# When within the day an agent prefers to reply (hours-of-day range)
TIME_OF_DAY_WINDOWS: dict[str, tuple[int, int]] = {
    "morning": (8, 13),
    "evening": (18, 23),
    "scattered": (8, 23),
}


def day_start_minutes(day: int) -> int:
    return (day - 1) * 24 * 60 + DAY_START_HOUR * 60


def day_end_minutes(day: int) -> int:
    return (day - 1) * 24 * 60 + DAY_END_HOUR * 60


def hour_of(minutes: int) -> int:
    return (minutes % (24 * 60)) // 60


def _clamp_to_window(target: int, window: tuple[int, int], day: int) -> int:
    """Push target into this day's preferred window for the agent."""
    start = (day - 1) * 24 * 60 + window[0] * 60
    end = (day - 1) * 24 * 60 + window[1] * 60
    if target < start:
        return start + random.randint(0, 20)
    if target > end:
        return end  # missed window; scheduler will drop if past day_end
    return target


def _sample_activation_time(agent, from_minutes: int, day: int) -> int:
    lo, hi = RESPONSIVENESS_DELAY_MIN[agent.persona.responsiveness]
    delay = random.randint(lo, hi)
    target = from_minutes + delay
    window = TIME_OF_DAY_WINDOWS[agent.persona.time_of_day]
    return _clamp_to_window(target, window, day)


def _engagement_probability(agent, trigger: Message, state: RunState) -> float:
    base = RESPONSIVENESS_BASE_PROB[agent.persona.responsiveness]
    # @mention boost
    if agent.persona.display_name.split()[0].lower() in trigger.content.lower():
        base = max(base, 0.95)
    # Admin announcements engage more broadly
    if trigger.sender_kind == "admin":
        base = min(1.0, base + 0.15)
    # Soft budget pressure
    if agent.messages_sent_today >= PER_AGENT_DAILY_SOFT_BUDGET:
        base *= 0.3
    # Saturation: if the agent has already sent 2+ messages in this chat today,
    # sharp drop — avoids DM echo loops of "grazie / non ti preoccupare / grazie".
    sent_here = sum(
        1 for m in state.messages
        if m.sender_id == agent.persona.id
        and m.chat_id == trigger.chat_id
        and m.day == state.clock.day
    )
    if sent_here >= 3:
        base *= 0.10
    elif sent_here >= 2:
        base *= 0.30
    # Pending-admin-reply damper: if the last message in this chat is a resident
    # question/message and admin hasn't spoken since, agents who already posted
    # today should wait rather than pile on. Prevents the hourly escalation
    # spiral when admin is silent.
    if trigger.sender_kind == "resident" and trigger.chat_id == "main":
        last_admin_min = _last_admin_message_min_in_chat(state, trigger.chat_id)
        # Messages addressed to/implying admin: if the trigger itself mentions
        # admin or is a question, treat this as a pending ping.
        trigger_pings_admin = _looks_like_admin_ping(trigger)
        if trigger_pings_admin and sent_here >= 1:
            hours_since_admin = (trigger.fictional_timestamp_minutes - last_admin_min) / 60.0
            if hours_since_admin < 6.0:
                base *= 0.20
    return base


def _last_admin_message_min_in_chat(state: RunState, chat_id: str) -> int:
    """Fictional-minute timestamp of admin's most recent message in chat, or -1."""
    latest = -1
    for m in state.messages:
        if m.chat_id == chat_id and m.sender_kind == "admin":
            if m.fictional_timestamp_minutes > latest:
                latest = m.fictional_timestamp_minutes
    return latest


_ADMIN_PING_WORDS = (
    "amministratore", "amministrator", "?", "quando", "cosa dice", "ci dica",
    "può confermare", "può dire", "ci risponda", "attendiamo", "in attesa",
    "chiarimenti", "chiarire", "spiegaci", "ci spieghi",
)


def _looks_like_admin_ping(msg: Message) -> bool:
    """Heuristic: does this resident message expect an admin reply?"""
    low = (msg.content or "").lower()
    return any(tok in low for tok in _ADMIN_PING_WORDS)


def _unread_count_for_agent(state: RunState, agent_id: str) -> int:
    """Messages in the agent's chats not authored by them, on today or yesterday.

    Rough proxy — doesn't track read-cursors across activations, which is fine
    for the morning check-in gate. We just want to know whether the agent has
    anything worth reopening the phone for.
    """
    today = state.clock.day
    chats_with_me = {c.id for c in state.chats if agent_id in c.member_ids}
    count = 0
    for m in state.messages:
        if m.chat_id not in chats_with_me:
            continue
        if m.sender_id == agent_id:
            continue
        if m.day < today - 1:
            continue
        count += 1
    return count


def _main_chat_had_activity_on_day(state: RunState, chat_id: str, day: int) -> bool:
    if day < 1:
        return False
    for m in state.messages:
        if m.chat_id == chat_id and m.day == day:
            return True
    return False


@dataclass(order=True)
class QueueItem:
    at_minutes: int
    seq: int
    agent_id: str
    trigger_msg_id: str
    depth: int


class DayLoop:
    def __init__(self, state: RunState):
        self.state = state
        self.queue: list[QueueItem] = []
        self.seq = 0
        self.activated: set[tuple[str, str]] = set()  # (agent_id, trigger_msg_id)
        # If an agent is already queued to react in a chat, we don't queue
        # them again when more messages arrive in that chat — they'll see all
        # accumulated messages on their single activation. Cleared when the
        # agent actually activates.
        self.chat_queued: set[tuple[str, str]] = set()  # (agent_id, chat_id)

    def _push(self, item: QueueItem) -> None:
        heapq.heappush(self.queue, item)

    def _resident_ids(self) -> set[str]:
        return {a.persona.id for a in self.state.agents}

    def schedule_reactions(self, trigger: Message, depth: int, force: bool = False) -> None:
        """Schedule reaction activations for a new message's audience.

        force=True: skip engagement probability roll and clamp past-day-end
        activations to just before day close. Used for admin-originated
        messages where a response is expected and shouldn't be random.
        """
        if depth > CASCADE_MAX_DEPTH:
            log("sched", f"cascade depth limit reached ({depth}), skipping")
            return
        residents = self._resident_ids()
        day_end = day_end_minutes(self.state.clock.day)
        scheduled = 0
        for aid in trigger.audience:
            if aid == trigger.sender_id:
                continue
            if aid not in residents:
                continue
            key = (aid, trigger.id)
            if key in self.activated:
                continue
            # Chat-level dedup: if agent is already queued to react in this
            # chat, let them see all accumulated messages on their single
            # pending activation rather than queueing them N times.
            chat_key = (aid, trigger.chat_id)
            if chat_key in self.chat_queued:
                log("sched", f"  {aid} already queued for {trigger.chat_id}, skip extra trigger")
                self.activated.add(key)
                continue
            agent = next(a for a in self.state.agents if a.persona.id == aid)
            if not force:
                prob = _engagement_probability(agent, trigger, self.state)
                roll = random.random()
                if roll > prob:
                    log("sched", f"  {aid} skip (prob={prob:.2f} roll={roll:.2f}) for msg from {trigger.sender_display_name}")
                    continue
            at = _sample_activation_time(
                agent, trigger.fictional_timestamp_minutes, self.state.clock.day
            )
            if at > day_end:
                if force:
                    at = day_end - 1
                    log("sched", f"  {aid} FORCED clamp to day_end-1 (was {at})")
                else:
                    log("sched", f"  {aid} past day_end (at={at} > {day_end}), dropping")
                    continue
            self.activated.add(key)
            self.chat_queued.add(chat_key)
            self.seq += 1
            self._push(QueueItem(at, self.seq, aid, trigger.id, depth))
            scheduled += 1
            log("sched", f"  {aid} SCHEDULED @min{at} depth={depth} force={force}")
        log("sched", f"schedule_reactions for msg '{trigger.content[:40]!r}' -> {scheduled} activations")

    async def run(self) -> None:
        # Reset daily counters
        for a in self.state.agents:
            a.messages_sent_today = 0

        day = self.state.clock.day
        log("sched", f"=== DAY {day} START ===")
        bus().publish(self.state.run_id, "day_start", {"day": day})

        # Seed from all non-resident messages sent today (admin announcements,
        # external contacts, etc.) that haven't cascaded yet.
        seed_msgs = [m for m in self.state.messages if m.day == day and m.sender_kind != "resident"]
        log("sched", f"seed: {len(seed_msgs)} non-resident messages on day {day}")
        for m in seed_msgs:
            self.schedule_reactions(m, depth=0)

        # Morning check-in: every agent opens their phone in their preferred
        # time-of-day window — but only if there's something to respond to.
        # Without gating, on quiet days every agent re-reads yesterday's
        # unanswered messages and restarts the anxiety spiral.
        day_end = day_end_minutes(day)
        main_chat_id = next((c.id for c in self.state.chats if c.kind == "main"), "main")
        # Is there any new main-chat activity today that hasn't been cascaded
        # into the queue yet? If today is day 1, seed_msgs already covers it.
        # On later days, a check-in is useful only if yesterday had activity
        # that might still need a follow-up, OR if the agent has unread messages.
        for agent in self.state.agents:
            aid = agent.persona.id
            unread_count = _unread_count_for_agent(self.state, aid)
            yesterday_saw_activity = _main_chat_had_activity_on_day(
                self.state, main_chat_id, day - 1
            ) if day > 1 else True
            # Gate: skip if no unread AND yesterday was also quiet. Otherwise
            # there's no reason for this agent to open the phone unprompted.
            if unread_count == 0 and not yesterday_saw_activity:
                log("sched", f"  {aid} check-in skipped (no unread, quiet prior day)")
                continue
            # Secondary gate: even with unread, if the unread is all stale
            # admin-pings from >24h ago that this agent has already engaged
            # with, skip — they'd just re-escalate.
            if unread_count == 0:
                # Only old activity, no actual unread. Skip most agents but
                # allow a single random "conversation starter" per day so
                # the day isn't totally dead.
                if random.random() > 0.25:
                    log("sched", f"  {aid} check-in skipped (nothing new to respond to)")
                    continue
            window = TIME_OF_DAY_WINDOWS[agent.persona.time_of_day]
            window_start = (day - 1) * 24 * 60 + window[0] * 60
            window_span = min(120, (window[1] - window[0]) * 60)
            at = window_start + random.randint(0, window_span)
            if at > day_end:
                continue
            self.seq += 1
            self._push(QueueItem(at, self.seq, aid, f"checkin_d{day}_{aid}", depth=0))
            self.activated.add((aid, f"checkin_d{day}_{aid}"))
            log("sched", f"  {aid} morning check-in scheduled @min{at} (unread={unread_count})")
        log("sched", f"initial queue size: {len(self.queue)}")

        # Drain the queue in parallel batches. Each batch contains activations
        # that can run concurrently: at most one per agent, within a small
        # fictional-time window of the queue head. This is the main speedup
        # lever — 5 agents reacting to the same admin announcement go out
        # as parallel HTTP calls instead of sequential ones.
        activated_count = 0
        while self.queue:
            first = heapq.heappop(self.queue)
            window_end = first.at_minutes + BATCH_FICTIONAL_WINDOW_MIN
            batch: list[QueueItem] = [first]
            batch_agents: set[str] = {first.agent_id}
            # Greedy take: include items within window that don't duplicate agent
            while self.queue and self.queue[0].at_minutes <= window_end:
                peek = self.queue[0]
                if peek.agent_id in batch_agents:
                    break  # same agent repeated — defer to next batch
                item = heapq.heappop(self.queue)
                batch.append(item)
                batch_agents.add(item.agent_id)

            # Causality clamp per item (can't act before events we know about)
            clock_floor = self.state.clock.minutes_since_start
            for item in batch:
                if item.at_minutes < clock_floor:
                    log("sched", f"{item.agent_id} clamped {item.at_minutes}->{clock_floor}")
                    item.at_minutes = clock_floor

            log("sched", f"batch: {len(batch)} activations — {[(i.agent_id, i.at_minutes) for i in batch]}")

            # Clear chat-level queue entries for the activating agents — once
            # they're running, future messages in those chats can re-queue them.
            for it in batch:
                for k in list(self.chat_queued):
                    if k[0] == it.agent_id:
                        self.chat_queued.discard(k)

            # Run all activations concurrently
            tasks = [
                activate_agent(self.state, it.agent_id, it.at_minutes)
                for it in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            activated_count += len(batch)

            # Collect per-activation new messages and cascade
            max_time_reached = clock_floor
            for it, res in zip(batch, results):
                if isinstance(res, Exception):
                    log_error("sched", f"{it.agent_id} failed: {res!r}")
                    continue
                new_msgs = list(res.sent_messages_this_activation)
                for nm in new_msgs:
                    if nm.fictional_timestamp_minutes > max_time_reached:
                        max_time_reached = nm.fictional_timestamp_minutes
                    self.schedule_reactions(nm, depth=it.depth + 1)

            # Advance the clock to the latest fictional time reached
            self.state.clock.minutes_since_start = max(max_time_reached, clock_floor)

            # Persist after every batch so mid-day state is durable in Postgres.
            # Without this, agent messages live only in RAM until day_end —
            # a dyno restart, lost SSE packet, or UI refresh mid-day would make
            # them invisible to the client (even though they'd eventually land
            # at day_end). Cost: one UPSERT per ~5 agent activations.
            try:
                await save_run(self.state)
            except Exception as exc:
                log_error("sched", f"mid-batch save_run failed (will retry at day_end): {exc!r}")

        log("sched", f"=== DAY {day} END === activations={activated_count} total_msgs={len(self.state.messages)}")
        # End of day: clock to day_end
        self.state.clock.minutes_since_start = day_end_minutes(day)
        # Persist BEFORE publishing day_end so any client that refetches on
        # the event reads the up-to-date run (not the previous day's snapshot).
        await save_run(self.state)
        bus().publish(self.state.run_id, "day_end", {
            "day": day,
            "activations": activated_count,
            "total_messages": len(self.state.messages),
        })
        # Each agent writes their end-of-day diary entry into MEMORY.
        # This is what makes day N+1 feel different from day N.
        await memory.consolidate_day(self.state, day)
        # Re-save AFTER consolidation: agent.notes are cleared inside
        # _consolidate_one and that mutation needs to land in Postgres.
        await save_run(self.state)


_ACTIVE_LOOPS: dict[str, DayLoop] = {}


def active_loop(run_id: str) -> DayLoop | None:
    """Return the currently running day loop for a run, if any."""
    return _ACTIVE_LOOPS.get(run_id)


async def advance_to_next_day(state: RunState) -> None:
    """Wake up the next fictional morning and let the day play out."""
    if state.ended:
        return
    # If this is the start of the run, we're already on Day 1 — just run it.
    # Otherwise advance the day counter first.
    if state.clock.minutes_since_start > 0:
        state.clock.day += 1
        state.clock.minutes_since_start = day_start_minutes(state.clock.day)
    else:
        state.clock.minutes_since_start = day_start_minutes(state.clock.day)

    loop = DayLoop(state)
    _ACTIVE_LOOPS[state.run_id] = loop
    try:
        await loop.run()
    finally:
        _ACTIVE_LOOPS.pop(state.run_id, None)
