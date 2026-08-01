"""Spend caps — the financial safety net.

The OpenRouter key has a hard credit cap. These tests prove the app stops
itself first, with a readable reason, instead of walking into a provider-side
402 mid-run.
"""
from __future__ import annotations

import pytest

from backend import llm, scheduler


async def test_run_cap_ends_the_run_cleanly(run_state, fake_llm, monkeypatch):
    # A cap low enough that the first activation blows through it.
    monkeypatch.setattr(llm, "RUN_COST_CAP_USD", 0.0005)
    monkeypatch.setattr(llm, "MONTHLY_COST_CAP_USD", 0.0)

    await scheduler.advance_to_next_day(run_state)

    assert run_state.ended is True
    assert run_state.ended_reason == "cost_cap_exceeded"


async def test_monthly_cap_blocks_before_spending(run_state, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RUN_COST_CAP_USD", 0.0)
    monkeypatch.setattr(llm, "MONTHLY_COST_CAP_USD", 0.001)
    # Pretend this month already spent the lot.
    llm._MEM_MONTHLY_SPEND[llm._current_month()] = 5.0
    llm._spend_cache = None

    await scheduler.advance_to_next_day(run_state)

    assert run_state.ended is True
    assert run_state.ended_reason == "monthly_budget_exhausted"
    assert fake_llm.call_count == 0, "not a single request left the process"


async def test_uncapped_run_is_unaffected(run_state, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RUN_COST_CAP_USD", 0.0)
    monkeypatch.setattr(llm, "MONTHLY_COST_CAP_USD", 0.0)

    await scheduler.advance_to_next_day(run_state)

    assert run_state.ended is False
    assert run_state.ended_reason is None


async def test_spend_is_accumulated_across_calls(run_state, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RUN_COST_CAP_USD", 0.0)
    monkeypatch.setattr(llm, "MONTHLY_COST_CAP_USD", 0.0)

    await scheduler.advance_to_next_day(run_state)

    assert run_state.metrics.llm_calls > 0
    assert run_state.metrics.prompt_tokens > 0
    month_total = await llm.monthly_spend_usd(use_cache=False)
    assert month_total == pytest.approx(run_state.metrics.estimated_cost_usd, rel=1e-6)


async def test_ended_run_does_not_advance_again(run_state, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "RUN_COST_CAP_USD", 0.0005)
    await scheduler.advance_to_next_day(run_state)
    assert run_state.ended is True

    calls_after_stop = fake_llm.call_count
    await scheduler.advance_to_next_day(run_state)
    assert fake_llm.call_count == calls_after_stop, "no further spend once ended"
