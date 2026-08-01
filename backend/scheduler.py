"""Round-robin day scheduler.

Each fictional day is split into N rounds; in every round each agent takes
exactly one turn (in a seeded random order) and rolls a participation
probability. On a hit they activate and see the FULL up-to-date state
(every message every other agent has produced this day so far). On a miss
they publish `agent_skipped_turn` and the round continues.

Why this shape:
- Serial activation removes the cause of v1's near-duplicate problem —
  agents B/C/D no longer activate concurrently against the same snapshot
  and rephrase the same impulse. They see what was already said.
- Per-round participation rolls preserve the v1 engagement signal
  (responsiveness, time-of-day, mention boost, saturation, admin-ping
  damper) so Conti and Greco still feel different, just sequentially.
- Mid-day admin actions (announce/DM/motion) keep the v1 surface:
  `schedule_reactions(msg, force=True)` from main.py marks the recipients
  as owed-a-reaction so they bypass the roll on their next turn (and get
  a bonus drain pass if all planned rounds are already done).

All timing is fictional minutes since Day 1 00:00 — wall-clock-free.
"""
from __future__ import annotations

import hashlib
import random

from . import atmosphere, memory, timeline
from .agent import activate_agent
from .config import (
    DAY_END_HOUR,
    DAY_START_HOUR,
    PER_AGENT_DAILY_SOFT_BUDGET,
    ROUNDS_PER_DAY,
)
from .events import bus
from .logging_utils import log, log_error
from .models import Agent, Message, RunState
from .storage import save_run


# Fallback participation probability when persona.participation_probability
# is None. Mirrors v1's RESPONSIVENESS_BASE_PROB so existing residents.json
# behaviour is preserved.
_RESPONSIVENESS_DEFAULT_PROB: dict[str, float] = {
    "fast": 0.85,
    "medium": 0.65,
    "slow": 0.35,
}

# When within the day an agent prefers to be on the phone (hours-of-day).
TIME_OF_DAY_WINDOWS: dict[str, tuple[int, int]] = {
    "morning": (8, 13),
    "evening": (18, 23),
    "scattered": (8, 23),
}

# After the planned rounds, drain any pending admin-induced reactions with
# at most this many extra passes. Bounded so a late admin burst can't loop
# forever, but generous enough that announce + DM + motion in the same
# minute all get serviced.
_MAX_BONUS_ROUNDS = 2


def day_start_minutes(day: int) -> int:
    return (day - 1) * 24 * 60 + DAY_START_HOUR * 60


def day_end_minutes(day: int) -> int:
    return (day - 1) * 24 * 60 + DAY_END_HOUR * 60


def hour_of(minutes: int) -> int:
    return (minutes % (24 * 60)) // 60


def _baseline_probability(agent: Agent) -> float:
    explicit = agent.persona.participation_probability
    if explicit is not None:
        return max(0.0, min(1.0, float(explicit)))
    return _RESPONSIVENESS_DEFAULT_PROB.get(agent.persona.responsiveness, 0.5)


def _last_admin_message_min_in_chat(state: RunState, chat_id: str) -> int:
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
    low = (msg.content or "").lower()
    return any(tok in low for tok in _ADMIN_PING_WORDS)


def _unread_count_for_agent(state: RunState, agent_id: str) -> int:
    """Messages in the agent's chats not authored by them, today or yesterday."""
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
    return any(m.chat_id == chat_id and m.day == day for m in state.messages)


def _has_pending_admin_dm(state: RunState, agent_id: str, now: int) -> bool:
    """Is there an admin DM whose last message is from admin and the agent
    hasn't replied? Bypasses the participation roll — admin DMs always
    deserve a reply."""
    for chat in state.chats:
        if chat.kind != "dm" or "admin" not in chat.member_ids:
            continue
        if agent_id not in chat.member_ids:
            continue
        latest: Message | None = None
        for m in state.messages:
            if m.chat_id != chat.id:
                continue
            if m.fictional_timestamp_minutes > now:
                continue
            if latest is None or m.fictional_timestamp_minutes > latest.fictional_timestamp_minutes:
                latest = m
        if latest is not None and latest.sender_id == "admin":
            return True
    return False


def _was_mentioned_recently(state: RunState, agent: Agent, since_min: int, now: int) -> bool:
    """Anyone @-mention or surname-call this agent in [since_min, now]?"""
    needle = agent.persona.display_name.split()[-1].lower()
    if len(needle) < 3:
        return False
    chats_with_me = {c.id for c in state.chats if agent.persona.id in c.member_ids}
    for m in state.messages:
        if m.chat_id not in chats_with_me:
            continue
        if m.sender_id == agent.persona.id:
            continue
        if m.fictional_timestamp_minutes < since_min or m.fictional_timestamp_minutes > now:
            continue
        if needle in (m.content or "").lower():
            return True
    return False


def _admin_recently_announced(state: RunState, since_min: int, now: int) -> bool:
    main_id = next((c.id for c in state.chats if c.kind == "main"), "main")
    for m in state.messages:
        if m.chat_id != main_id or m.sender_kind != "admin":
            continue
        if since_min <= m.fictional_timestamp_minutes <= now:
            return True
    return False


def _participation_probability(
    agent: Agent,
    state: RunState,
    fictional_minute: int,
    is_first_round: bool,
) -> float:
    """Per-round participation probability, preserving v1's engagement signal.

    Modifiers carried over from the v1 cascade engagement-roll function
    (`_engagement_probability` in the old scheduler.py): mention boost,
    admin-announcement boost, saturation, soft per-agent budget,
    admin-ping damper. Time-of-day window is folded in here too instead
    of being a hard fictional-minute clamp.
    """
    base = _baseline_probability(agent)
    aid = agent.persona.id
    day = state.clock.day
    main_id = next((c.id for c in state.chats if c.kind == "main"), "main")

    # Look back ~3 fictional hours from this turn for "recent" signals.
    look_back_min = max(0, fictional_minute - 180)

    # Mention boost — someone called this agent by name recently.
    if _was_mentioned_recently(state, agent, look_back_min, fictional_minute):
        base = max(base, 0.95)

    # Admin announcement / DM / motion in the recent window pulls everyone
    # back toward engagement.
    if _admin_recently_announced(state, look_back_min, fictional_minute):
        base = min(1.0, base + 0.15)

    # Time-of-day preference: outside the agent's preferred window the
    # probability is dampened. Replaces v1's hard fictional-minute clamp.
    window = TIME_OF_DAY_WINDOWS.get(agent.persona.time_of_day, (8, 23))
    hour = hour_of(fictional_minute)
    if hour < window[0] or hour > window[1]:
        base *= 0.4

    # Soft per-agent daily budget.
    if agent.messages_sent_today >= PER_AGENT_DAILY_SOFT_BUDGET:
        base *= 0.3

    # Per-chat saturation: if the agent has already filled a chat today,
    # don't keep piling on.
    sent_in_main = sum(
        1 for m in state.messages
        if m.sender_id == aid and m.chat_id == main_id and m.day == day
    )
    if sent_in_main >= 3:
        base *= 0.20
    elif sent_in_main >= 2:
        base *= 0.50

    # Admin-ping damper: if the most recent main-chat message is a
    # resident question/ping aimed at admin and admin still hasn't replied,
    # agents who already spoke today should hold off rather than escalate.
    last_admin_min = _last_admin_message_min_in_chat(state, main_id)
    last_main_msg: Message | None = None
    for m in state.messages:
        if m.chat_id != main_id or m.fictional_timestamp_minutes > fictional_minute:
            continue
        if last_main_msg is None or m.fictional_timestamp_minutes > last_main_msg.fictional_timestamp_minutes:
            last_main_msg = m
    if (
        last_main_msg is not None
        and last_main_msg.sender_kind == "resident"
        and _looks_like_admin_ping(last_main_msg)
        and sent_in_main >= 1
    ):
        hours_since_admin = (fictional_minute - last_admin_min) / 60.0 if last_admin_min >= 0 else 999
        if hours_since_admin < 6.0:
            base *= 0.20

    # Day-1-morning / quiet-prior-day gate, but only on the first round of
    # the day — later rounds are allowed to engage even if the morning was
    # quiet, since the day may have woken up by then.
    if is_first_round and day > 1:
        unread = _unread_count_for_agent(state, aid)
        yesterday_active = _main_chat_had_activity_on_day(state, main_id, day - 1)
        if unread == 0 and not yesterday_active:
            base *= 0.10

    return max(0.0, min(1.0, base))


def _allocate_round_window(day: int, round_idx: int, total_rounds: int) -> tuple[int, int]:
    """Even split of the day's [day_start, day_end] across rounds."""
    start = day_start_minutes(day)
    end = day_end_minutes(day)
    span = end - start
    chunk = span // total_rounds
    win_start = start + round_idx * chunk
    win_end = start + (round_idx + 1) * chunk if round_idx < total_rounds - 1 else end
    return win_start, win_end


def _seed_for(run_id: str, day: int, round_idx: int) -> int:
    raw = f"{run_id}:day:{day}:round:{round_idx}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


class DayLoop:
    """Holds per-day state for the round-robin scheduler.

    Public surface kept for compatibility with main.py admin endpoints:
      - `state` attribute (mutated by admin handlers under state_lock)
      - `schedule_reactions(msg, depth=0, force=True)` — called when admin
        posts mid-day; marks the audience as owed-a-reaction so they
        bypass the participation roll on their next turn (or get a bonus
        drain pass if planned rounds already finished).
    """

    def __init__(self, state: RunState):
        self.state = state
        # agent_ids that owe a reaction to a fresh admin input.
        self.pending_admin_reactions: set[str] = set()
        # Set to a BudgetExceeded reason as soon as a spend cap trips, which
        # aborts the remaining rounds and ends the run.
        self.budget_stop: str | None = None

    def _resident_ids(self) -> set[str]:
        return {a.persona.id for a in self.state.agents}

    def schedule_reactions(self, trigger: Message, depth: int = 0, force: bool = False) -> None:
        """Compatibility shim — main.py calls this after admin actions.

        In round-robin there is no separate priority queue: agents see all
        messages on their next turn naturally. We just record who owes
        admin a reply so the participation roll is bypassed for them.
        """
        residents = self._resident_ids()
        for aid in trigger.audience:
            if aid == trigger.sender_id or aid not in residents:
                continue
            self.pending_admin_reactions.add(aid)
        trigger.cascaded = True
        log(
            "sched",
            f"schedule_reactions force={force} owed={sorted(self.pending_admin_reactions)} "
            f"(msg from {trigger.sender_display_name!r})",
        )

    async def _run_one_round(
        self,
        round_idx: int,
        total_rounds: int,
        is_first_round: bool,
    ) -> int:
        """Run a single round of the day loop. Returns activations executed."""
        agents_in_run = list(self.state.agents)
        seed = _seed_for(self.state.run_id, self.state.clock.day, round_idx)
        rng = random.Random(seed)
        order = rng.sample(agents_in_run, k=len(agents_in_run))

        win_start, win_end = _allocate_round_window(
            self.state.clock.day, round_idx, total_rounds
        )
        span = max(1, win_end - win_start)
        tick = max(1, span // max(1, len(order)))

        log(
            "sched",
            f"round {round_idx + 1}/{total_rounds} window=[{win_start},{win_end}] "
            f"order={[a.persona.id for a in order]}",
        )

        activated = 0
        for i, agent in enumerate(order):
            aid = agent.persona.id
            target = win_start + i * tick + rng.randint(0, max(1, tick // 3))
            target = min(target, win_end - 1)

            # Causality clamp: an agent can't act before anything already in
            # the transcript. Floor on the newest MESSAGE, not on
            # `state.clock` — the clock lags behind messages produced by an
            # in-flight activation and by mid-day admin posts, so using it
            # let an agent be stamped earlier than content they could see.
            clock_floor = timeline.latest_minute(self.state)
            if target < clock_floor:
                target = clock_floor

            owed = aid in self.pending_admin_reactions
            # If this agent is owed a reaction to admin input, push their
            # target past the latest admin/external message addressed to
            # them — otherwise they'd activate before the message exists in
            # fictional time and not see it via read_inbox / _thread_status
            # (both filter by `fictional_timestamp_minutes <= now`).
            if owed:
                latest_owed_min = -1
                for m in self.state.messages:
                    if m.sender_kind == "resident" or aid not in m.audience:
                        continue
                    if m.fictional_timestamp_minutes > latest_owed_min:
                        latest_owed_min = m.fictional_timestamp_minutes
                if latest_owed_min >= 0 and target <= latest_owed_min:
                    # Small "I picked up the phone shortly after" delay so the
                    # agent is reacting in the same minute, not racing the post.
                    target = latest_owed_min + rng.randint(2, 25)
            pending_dm = _has_pending_admin_dm(self.state, aid, target)
            if owed or pending_dm:
                prob = 1.0
                forced_reason = "owed_admin_reaction" if owed else "pending_admin_dm"
            else:
                prob = _participation_probability(
                    agent, self.state, target, is_first_round=is_first_round
                )
                forced_reason = None

            roll = rng.random()
            if forced_reason is None and roll >= prob:
                bus().publish(self.state.run_id, "agent_skipped_turn", {
                    "agent_id": aid,
                    "display_name": agent.persona.display_name,
                    "round": round_idx,
                    "prob": round(prob, 3),
                    "roll": round(roll, 3),
                    "fictional_minutes": target,
                    "day": self.state.clock.day,
                })
                log(
                    "sched",
                    f"  {aid} skip round {round_idx + 1} (prob={prob:.2f} roll={roll:.2f}) @min{target}",
                )
                continue

            log(
                "sched",
                f"  {aid} ACTIVATE round {round_idx + 1} @min{target} "
                f"prob={prob:.2f} roll={roll:.2f}"
                + (f" [{forced_reason}]" if forced_reason else ""),
            )
            try:
                ctx = await activate_agent(
                    self.state, aid, target, forced_for_admin=bool(forced_reason)
                )
            except Exception as exc:
                log_error("sched", f"{aid} activation failed: {exc!r}")
                continue
            if ctx.budget_exceeded:
                self.budget_stop = ctx.budget_exceeded
                log_error("sched", f"budget cap hit ({ctx.budget_exceeded}) — stopping day")
                break
            acknowledged = bool(ctx.sent_messages_this_activation)
            if forced_reason is not None and not acknowledged:
                # Forced agent closed the phone without sending.
                # Keep them in pending so the bonus drain retries them — the
                # admin's message must not be silently ignored.
                self.pending_admin_reactions.add(aid)
                log(
                    "sched",
                    f"  {aid} forced ({forced_reason}) closed phone with no ack — kept owed",
                )
            else:
                self.pending_admin_reactions.discard(aid)
            activated += 1

            # Advance the clock so subsequent agents in this round see the
            # messages this one just sent.
            new_msgs = list(ctx.sent_messages_this_activation)
            for nm in new_msgs:
                if nm.fictional_timestamp_minutes > self.state.clock.minutes_since_start:
                    self.state.clock.minutes_since_start = nm.fictional_timestamp_minutes
                # Mark these as cascaded so an admin-mutation racing the loop
                # doesn't try to re-schedule them.
                nm.cascaded = True

        # Persist after every round so mid-day state is durable in Postgres.
        try:
            await save_run(self.state)
        except Exception as exc:
            log_error("sched", f"end-of-round save_run failed: {exc!r}")
        return activated

    async def _end_run_on_budget(self, total_activations: int) -> None:
        """Terminate the run because a spend cap was reached.

        Ends deliberately rather than crashing: the transcript stays intact,
        the UI gets a reason to show, and no further LLM calls are made
        (including the day-end memory consolidation, which would itself cost
        one call per agent).
        """
        reason = self.budget_stop or "cost_cap_exceeded"
        day = self.state.clock.day
        self.state.ended = True
        self.state.ended_reason = reason
        log_error(
            "sched",
            f"=== DAY {day} ABORTED ({reason}) === activations={total_activations}",
        )
        _ACTIVE_LOOPS.pop(self.state.run_id, None)
        await save_run(self.state)
        bus().publish(self.state.run_id, "run_ended", {
            "reason": reason,
            "day": day,
            "estimated_cost_usd": self.state.metrics.estimated_cost_usd,
        })

    async def run(self) -> None:
        # Reset daily counters
        for a in self.state.agents:
            a.messages_sent_today = 0

        day = self.state.clock.day
        log("sched", f"=== DAY {day} START === rounds={ROUNDS_PER_DAY}")
        # Today's ambient event, if any. Handed to every resident as
        # something they noticed themselves — never posted as a system
        # message. Costs nothing (no LLM call).
        world_event = atmosphere.pick_world_event(self.state)
        bus().publish(self.state.run_id, "day_start", {
            "day": day,
            "world_event": world_event,
        })
        # Seed pending reactions from any uncascaded non-resident message.
        # Mirrors v1's "seed from any non-resident message that hasn't been
        # cascaded yet" behaviour — admin messages sent between days, during
        # consolidation, or before the first advance still get serviced.
        for m in self.state.messages:
            if m.sender_kind != "resident" and not m.cascaded:
                for aid in m.audience:
                    if aid != m.sender_id and aid in self._resident_ids():
                        self.pending_admin_reactions.add(aid)
                m.cascaded = True
        if self.pending_admin_reactions:
            log("sched", f"seeded pending reactions: {sorted(self.pending_admin_reactions)}")

        # Planned rounds
        total_activations = 0
        for round_idx in range(ROUNDS_PER_DAY):
            total_activations += await self._run_one_round(
                round_idx,
                ROUNDS_PER_DAY,
                is_first_round=(round_idx == 0),
            )
            if self.budget_stop:
                break

        if self.budget_stop:
            await self._end_run_on_budget(total_activations)
            return

        # Bonus drain rounds: admin may have posted very late in the day,
        # or some forced agents skipped earlier rounds because their turn
        # came before the admin message landed. Run extra passes through
        # just the agents who still owe a reaction, bounded by
        # _MAX_BONUS_ROUNDS.
        bonus = 0
        while self.pending_admin_reactions and bonus < _MAX_BONUS_ROUNDS:
            log("sched", f"bonus drain round {bonus + 1}: owed={sorted(self.pending_admin_reactions)}")
            owed_now = list(self.pending_admin_reactions)
            day_end = day_end_minutes(day)
            for aid in owed_now:
                agent = next((a for a in self.state.agents if a.persona.id == aid), None)
                if agent is None:
                    self.pending_admin_reactions.discard(aid)
                    continue
                # Bonus retries are spread across the remaining day so multiple
                # owed agents don't all stamp the same minute.
                base = max(timeline.latest_minute(self.state) + 5, day_end - 60)
                target = min(base, day_end - 1)
                log("sched", f"  {aid} BONUS activate @min{target}")
                try:
                    ctx = await activate_agent(
                        self.state, aid, target, forced_for_admin=True
                    )
                except Exception as exc:
                    log_error("sched", f"{aid} bonus activation failed: {exc!r}")
                    self.pending_admin_reactions.discard(aid)
                    continue
                if ctx.budget_exceeded:
                    self.budget_stop = ctx.budget_exceeded
                    break
                acknowledged = bool(ctx.sent_messages_this_activation)
                if acknowledged:
                    self.pending_admin_reactions.discard(aid)
                else:
                    log("sched", f"  {aid} bonus activation no ack — will retry next bonus round")
                total_activations += 1
                for nm in ctx.sent_messages_this_activation:
                    if nm.fictional_timestamp_minutes > self.state.clock.minutes_since_start:
                        self.state.clock.minutes_since_start = nm.fictional_timestamp_minutes
                    nm.cascaded = True
            bonus += 1
            try:
                await save_run(self.state)
            except Exception as exc:
                log_error("sched", f"bonus-round save_run failed: {exc!r}")
            if self.budget_stop:
                break
        if self.budget_stop:
            await self._end_run_on_budget(total_activations)
            return
        if self.pending_admin_reactions:
            log(
                "sched",
                f"WARNING: pending_admin_reactions still non-empty after "
                f"{_MAX_BONUS_ROUNDS} bonus rounds: {sorted(self.pending_admin_reactions)}. "
                f"Giving up so the day can end.",
            )
            self.pending_admin_reactions.clear()

        log(
            "sched",
            f"=== DAY {day} END === activations={total_activations} "
            f"total_msgs={len(self.state.messages)}",
        )
        # End of day: clock to day_end so consolidation sees the full window.
        self.state.clock.minutes_since_start = day_end_minutes(day)
        # Persist BEFORE publishing day_end so any client that refetches on
        # the event reads the up-to-date run.
        await save_run(self.state)
        # Pop the active-loop entry NOW, before consolidation. From this
        # point on the loop is no longer servicing turns, so admin endpoints
        # must NOT see this loop as "live" — any admin message arriving
        # during consolidation routes through the `loop is None` branch and
        # gets picked up by the next day's seed via Message.cascaded=False.
        _ACTIVE_LOOPS.pop(self.state.run_id, None)
        bus().publish(self.state.run_id, "day_end", {
            "day": day,
            "activations": total_activations,
            "total_messages": len(self.state.messages),
        })
        # Each agent writes their end-of-day diary entry into MEMORY.
        await memory.consolidate_day(self.state, day)
        # Re-save AFTER consolidation: agent.notes are cleared inside
        # consolidate and that mutation needs to land in Postgres.
        await save_run(self.state)


_ACTIVE_LOOPS: dict[str, DayLoop] = {}


def active_loop(run_id: str) -> DayLoop | None:
    """Return the currently running day loop for a run, if any."""
    return _ACTIVE_LOOPS.get(run_id)


def setup_day(state: RunState) -> "DayLoop | None":
    """Synchronously bump the clock to the next day's start, build the
    DayLoop, and register it in _ACTIVE_LOOPS. Returns the loop, or None if
    the run is already ended.

    Split from `run_day` so callers can hold a lock across DB-load +
    registration without holding it through the slow round drain.
    """
    if state.ended:
        return None
    if state.clock.minutes_since_start > 0:
        state.clock.day += 1
        state.clock.minutes_since_start = day_start_minutes(state.clock.day)
    else:
        state.clock.minutes_since_start = day_start_minutes(state.clock.day)
    loop = DayLoop(state)
    _ACTIVE_LOOPS[state.run_id] = loop
    return loop


async def run_day(loop: "DayLoop") -> None:
    """Drain the registered loop's rounds and run consolidation. Pops the
    loop out of _ACTIVE_LOOPS regardless of outcome (DayLoop.run pops
    itself early; this finally is a safety net)."""
    try:
        await loop.run()
    finally:
        _ACTIVE_LOOPS.pop(loop.state.run_id, None)


async def advance_to_next_day(state: RunState) -> None:
    """Wake up the next fictional morning and let the day play out.

    Backward-compat wrapper: callers that need to hold a lock through the
    setup but not the run should call setup_day + run_day directly.
    """
    loop = setup_day(state)
    if loop is None:
        return
    await run_day(loop)
