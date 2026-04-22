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

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# Scheduler
DAY_START_HOUR = 8
DAY_END_HOUR = 23
PER_AGENT_DAILY_SOFT_BUDGET = 5  # messages past this get deprioritized
MAX_TOOL_CALLS_PER_ACTIVATION = 4  # hard cap on an agent's single wake-up
CASCADE_MAX_DEPTH = 2  # bounds recursive reaction cascades
AGENT_TEMPERATURE = 1.0  # tool-calling loop: Gemini 3's recommended default; any deviation risks looping
MEMORY_TEMPERATURE = 1.3  # day_end consolidation: no tools, pure Italian writing — push higher for voice diversity
AGENT_MAX_TOKENS = 180

# Logging
VERBOSE_LOGGING = True  # prints per-activation traces to stderr

# Server
HOST = "127.0.0.1"
BACKEND_PORT = 8001
FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
