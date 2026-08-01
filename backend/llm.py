"""The single choke point for every LLM call in the simulation.

Everything that spends money goes through `complete()`. That buys three
things the old direct-to-openrouter calls could not provide:

1. **Hard spend caps.** A per-run cap and a calendar-month cap are checked
   *before* the request leaves the process. The month cap is the one that
   protects the OpenRouter key-level credit limit — a runaway day loop can
   no longer walk it up.
2. **A stub seam.** Tests swap `set_transport()` for a deterministic fake,
   so the whole day loop runs offline at zero cost.
3. **One place that records usage.** Token/cost accounting used to live at
   each call site and was easy to forget.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Protocol

from .config import MONTHLY_COST_CAP_USD, RUN_COST_CAP_USD
from .db import has_database, pool
from .logging_utils import log_error
from .models import RunState
from .openrouter import OpenRouterError, chat_completion, record_usage


class BudgetExceeded(RuntimeError):
    """Raised when a spend cap would be breached. Carries a machine-readable
    reason so the caller can end the run with the right terminal state."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class Transport(Protocol):
    def __call__(self, **kwargs: Any) -> Awaitable[dict[str, Any]]: ...


_transport: Transport = chat_completion


def set_transport(fn: Transport | None) -> Transport:
    """Swap the underlying completion function. Returns the previous one so
    tests can restore it. Passing None resets to the real OpenRouter client."""
    global _transport
    previous = _transport
    _transport = fn if fn is not None else chat_completion
    return previous


# ---------------------------------------------------------------------------
# Month-to-date spend
# ---------------------------------------------------------------------------

_MEM_MONTHLY_SPEND: dict[str, float] = {}
_spend_cache: tuple[str, float, float] | None = None  # (month, usd, cached_at)
_SPEND_CACHE_TTL_SEC = 30.0


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def monthly_spend_usd(*, use_cache: bool = True) -> float:
    """Month-to-date spend across every run. Cached briefly — the guard runs
    before every LLM call and a DB round-trip each time would dominate the
    latency of a cheap model."""
    global _spend_cache
    month = _current_month()
    if use_cache and _spend_cache is not None:
        cached_month, cached_usd, cached_at = _spend_cache
        if cached_month == month and (_now() - cached_at) < _SPEND_CACHE_TTL_SEC:
            return cached_usd

    if not has_database():
        usd = _MEM_MONTHLY_SPEND.get(month, 0.0)
    else:
        try:
            async with pool().acquire() as conn:
                row = await conn.fetchrow(
                    "select cost_usd from llm_spend where month = $1", month
                )
            usd = float(row["cost_usd"]) if row else 0.0
        except Exception as exc:
            # Never let a bookkeeping failure block the simulation. Fall back
            # to the in-process tally, which is at worst an under-count for
            # this dyno's lifetime.
            log_error("llm", f"monthly spend read failed: {exc!r}")
            usd = _MEM_MONTHLY_SPEND.get(month, 0.0)

    _spend_cache = (month, usd, _now())
    return usd


async def _record_monthly_spend(cost_usd: float) -> None:
    global _spend_cache
    if cost_usd <= 0:
        return
    month = _current_month()
    _MEM_MONTHLY_SPEND[month] = _MEM_MONTHLY_SPEND.get(month, 0.0) + cost_usd
    # Keep the cache in step so the next guard sees this spend even inside
    # the TTL window — otherwise a fast burst can overshoot the cap.
    if _spend_cache is not None and _spend_cache[0] == month:
        _spend_cache = (month, _spend_cache[1] + cost_usd, _spend_cache[2])

    if not has_database():
        return
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                """
                insert into llm_spend (month, cost_usd)
                values ($1, $2)
                on conflict (month) do update
                  set cost_usd = llm_spend.cost_usd + excluded.cost_usd,
                      updated_at = now()
                """,
                month,
                cost_usd,
            )
    except Exception as exc:
        log_error("llm", f"monthly spend write failed: {exc!r}")


def _now() -> float:
    import time

    return time.monotonic()


# ---------------------------------------------------------------------------
# Guarded completion
# ---------------------------------------------------------------------------

async def check_budget(state: RunState) -> None:
    """Raise BudgetExceeded if this run (or the month) is out of budget."""
    if RUN_COST_CAP_USD > 0:
        spent = float(state.metrics.estimated_cost_usd or 0.0)
        if spent >= RUN_COST_CAP_USD:
            raise BudgetExceeded(
                "cost_cap_exceeded",
                f"Run {state.run_id} reached its cost cap "
                f"(${spent:.4f} >= ${RUN_COST_CAP_USD:.2f})",
            )
    if MONTHLY_COST_CAP_USD > 0:
        month_usd = await monthly_spend_usd()
        if month_usd >= MONTHLY_COST_CAP_USD:
            raise BudgetExceeded(
                "monthly_budget_exhausted",
                f"Month-to-date spend ${month_usd:.4f} reached the cap "
                f"${MONTHLY_COST_CAP_USD:.2f}",
            )


async def complete(
    *,
    state: RunState,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.8,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    caller: str = "?",
) -> dict[str, Any]:
    """Budget-guarded chat completion. Records usage into the run metrics and
    the month-to-date tally.

    Raises `BudgetExceeded` before spending anything when a cap is already
    reached, and `OpenRouterError` on upstream failure (unchanged semantics
    for callers that already handle it).
    """
    await check_budget(state)

    before = float(state.metrics.estimated_cost_usd or 0.0)
    reply = await _transport(
        model=model,
        messages=messages,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        caller=caller,
    )
    record_usage(state, reply)
    delta = float(state.metrics.estimated_cost_usd or 0.0) - before
    await _record_monthly_spend(max(0.0, delta))
    return reply


__all__ = [
    "BudgetExceeded",
    "OpenRouterError",
    "check_budget",
    "complete",
    "monthly_spend_usd",
    "set_transport",
]
