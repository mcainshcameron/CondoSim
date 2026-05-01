"""Runtime configuration for Condominio."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Default models. Per-agent overrides live in the scenario file.
# Agent model. Gemini 3.1 Flash Lite Preview: $0.25 / $1.50 per M, 1M ctx,
# strong Italian, fast, reliable tool-calling.
DEFAULT_AGENT_MODEL = "google/gemini-3.1-flash-lite-preview"
AGENT_FALLBACK_MODELS = [
    "google/gemini-2.0-flash-lite-001",  # $0.075 / $0.30 per M, fallback on 429/5xx
]
NARRATOR_MODEL = "anthropic/claude-haiku-4.5"
CLASSIFIER_MODEL = "anthropic/claude-haiku-4.5"

# Paths (read-only — building authoring data, bundled in the deploy slug)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# Scheduler
DAY_START_HOUR = 8
DAY_END_HOUR = 23
PER_AGENT_DAILY_SOFT_BUDGET = 5  # messages past this get deprioritized
MAX_TOOL_CALLS_PER_ACTIVATION = 4  # hard cap on an agent's single wake-up
ROUNDS_PER_DAY = 4  # round-robin: 5 agents × 4 rounds × ~0.65 prob ≈ 13 activations/day
AGENT_TEMPERATURE = 1.0  # tool-calling loop: Gemini 3's recommended default; any deviation risks looping
MEMORY_TEMPERATURE = 1.3  # day_end consolidation: no tools, pure Italian writing — push higher for voice diversity
AGENT_MAX_TOKENS = 180

# Logging
VERBOSE_LOGGING = True  # prints per-activation traces to stderr

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
