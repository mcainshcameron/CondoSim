"""Runtime configuration for Condominio."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Default models. Per-agent overrides live in the scenario file.
#
# DeepSeek V4 Flash 0731: $0.14 / $0.28 per M, 1M ctx, and the most-used model
# on OpenRouter's roleplay leaderboard. Measured against the real prompt (SOUL
# + memory seed) at AGENT_MAX_TOKENS: 6/6 activations produced output, ~0.5s
# median, ~$0.000064/call — about 7x cheaper than Gemini 3.1 Flash Lite for
# the same job. Requires DISABLE_MODEL_REASONING (see below); without it this
# model returns empty every time.
#
# Pinned to the dated 0731 revision, NOT ~deepseek/deepseek-v4-flash-latest:
# the tilde alias silently redirects to whatever ships next, which would move
# the ground under the canary metrics in backend/analyze.py mid-study.
DEFAULT_AGENT_MODEL = "deepseek/deepseek-v4-flash-0731"
AGENT_FALLBACK_MODELS = [
    # Fallback on 429/5xx. Different provider from the primary on purpose —
    # a fallback that shares an upstream is not a fallback. Verified 6/6 on
    # tool-calling under the real prompt.
    #
    # NOT google/gemini-2.5-flash-lite: it writes good Italian but answers in
    # plain text instead of calling a tool, which in this loop is a silent
    # no-op activation (measured 0/6).
    "google/gemini-3.1-flash-lite-preview",  # $0.25 / $1.50 per M
]
NARRATOR_MODEL = "anthropic/claude-haiku-4.5"
CLASSIFIER_MODEL = "anthropic/claude-haiku-4.5"

# Approximate public pricing in USD per 1M input/output tokens. Used only
# for in-app spend visibility; OpenRouter remains the billing source of truth.
MODEL_PRICING_USD_PER_M_TOKENS = {
    "deepseek/deepseek-v4-flash-0731": (0.14, 0.28),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
    "google/gemini-3.1-flash-lite-preview": (0.25, 1.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "meta-llama/llama-3.3-70b-instruct": (0.13, 0.40),
    "x-ai/grok-4.3": (1.25, 2.50),
    "mistralai/mistral-small-2603": (0.15, 0.60),
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
}

# Rate assumed for a model missing from the table above. Deliberately on the
# expensive side: an unknown model must OVER-estimate, never under-estimate.
# With a 0.0 fallback (the old behaviour) adding a model to residents.json
# without updating the table made its spend invisible — and a spend cap that
# reads $0.00 forever is not a cap.
UNKNOWN_MODEL_PRICING_USD_PER_M_TOKENS = (1.00, 5.00)

# ---------------------------------------------------------------------------
# Spend caps. These are the hard financial safety net — checked in
# backend/llm.py before every request leaves the process.
#
# MONTHLY_COST_CAP_USD defaults deliberately below a €10 OpenRouter
# key-level credit cap (~$11) so the app stops itself before the provider
# does, leaving a clear in-app error instead of an opaque 402. Set either to
# 0 to disable that cap.
# ---------------------------------------------------------------------------
RUN_COST_CAP_USD = float(os.getenv("RUN_COST_CAP_USD", "0.75"))
MONTHLY_COST_CAP_USD = float(os.getenv("MONTHLY_COST_CAP_USD", "8.0"))

# Paths (read-only — building authoring data, bundled in the deploy slug)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Scheduler
DAY_START_HOUR = 8
DAY_END_HOUR = 23
PER_AGENT_DAILY_SOFT_BUDGET = 5  # messages past this get deprioritized
# Hard cap on LLM round trips in one wake-up. Most activations now finish in
# ONE: the recent transcript is pre-read into the prompt (see
# agent.build_context_digest) and the loop exits as soon as the agent has
# actually produced something, instead of spending a round trip on an
# explicit `done`.
MAX_TOOL_CALLS_PER_ACTIVATION = 3
# How much chat history to inline per chat. Bounded so prompt size stays
# flat as a run gets long.
MAIN_CHAT_CONTEXT = 14
DM_CONTEXT = 6
# Recent diary entries carried into the prompt (the seed is always kept).
# Older days stay in the database and in the MEMORY viewer.
MEMORY_DAYS_IN_PROMPT = 6

# Chance that a given day has an ambient world event (see atmosphere.py).
# Roughly one every other day: frequent enough that the building feels alive,
# rare enough that it doesn't upstage the admin's own storyline.
WORLD_EVENT_PROBABILITY = float(os.getenv("WORLD_EVENT_PROBABILITY", "0.45"))
ROUNDS_PER_DAY = 4  # round-robin: 5 agents × 4 rounds × ~0.65 prob ≈ 13 activations/day
AGENT_TEMPERATURE = 1.0  # tool-calling loop: Gemini 3's recommended default; any deviation risks looping
MEMORY_TEMPERATURE = 1.3  # day_end consolidation: no tools, pure Italian writing — push higher for voice diversity
AGENT_MAX_TOKENS = 180

# Reasoning models spend their token budget *thinking* before they emit
# anything. Against AGENT_MAX_TOKENS (180) that is fatal: DeepSeek V4 Flash
# burns ~200 reasoning tokens and returns finish_reason="length" with EMPTY
# content — a silently failed activation, every time. Measured 0/6 activations
# produced output with reasoning on, 6/6 with it off. Grok 4.3 is worse (~628
# reasoning tokens, 1/6).
#
# There is also nothing to reason *about* here: a resident glancing at a group
# chat and firing off one colloquial line is not a chain-of-thought task. So
# reasoning is disabled globally. OpenRouter accepts and ignores the parameter
# on non-reasoning models, so this is safe to send unconditionally.
#
# Set to 0 only if you deliberately want to trade cost and latency for
# deliberation — and raise AGENT_MAX_TOKENS well above 180 if you do, or every
# activation will come back empty.
DISABLE_MODEL_REASONING = os.getenv("DISABLE_MODEL_REASONING", "1") not in (
    "0", "false", "False", "",
)

# Logging. Env-overridable so the offline simulator and the test suite can
# run quiet without editing the file.
VERBOSE_LOGGING = os.getenv("VERBOSE_LOGGING", "1") not in ("0", "false", "False", "")

# Server. PORT comes from Heroku at runtime; HOST/BACKEND_PORT are local-dev defaults.
HOST = "127.0.0.1"
BACKEND_PORT = int(os.getenv("PORT", "8001"))

# Auth + kill switch (production-only behavior; in local dev these may be unset)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_COOKIE_NAME = "condosim_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
# Cookie sent only over HTTPS by default. In local http dev, set
# SESSION_COOKIE_SECURE=0 in .env so the browser will accept the cookie.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "1") not in ("0", "false", "False", "")
DISABLED = os.getenv("DISABLED", "").strip() == "1"
