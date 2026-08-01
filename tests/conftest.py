"""Shared fixtures. Every test here runs offline: no DATABASE_URL, no
OPENROUTER_API_KEY, no network, no cost."""
from __future__ import annotations

import os

import pytest

# Force the offline path before backend.config is imported.
#
# Set to "" rather than popped: backend.config calls load_dotenv(), which
# would otherwise repopulate DATABASE_URL from the developer's real .env and
# point the suite at live Supabase. load_dotenv does not override keys that
# are already present in os.environ — even when the value is empty — so this
# pins the in-memory store.
os.environ["DATABASE_URL"] = ""
os.environ["OPENROUTER_API_KEY"] = "test-key-not-used"
os.environ["RUN_COST_CAP_USD"] = "0"
os.environ["MONTHLY_COST_CAP_USD"] = "0"
os.environ["VERBOSE_LOGGING"] = "0"
# Auth is opt-in (needs BOTH of these). Clearing them keeps the API tests
# focused on behaviour rather than session cookies.
os.environ["ADMIN_PASSWORD"] = ""
os.environ["SESSION_SECRET"] = ""
os.environ["DISABLED"] = ""

from backend import building, llm, memory, storage  # noqa: E402
from backend.models import RunState  # noqa: E402

from .fake_llm import FakeLLM  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_state():
    """Wipe the in-memory stores between tests so runs can't leak into
    each other."""
    storage._MEM_RUNS.clear()
    memory._MEM_AGENT_MEMORY.clear()
    llm._MEM_MONTHLY_SPEND.clear()
    llm._spend_cache = None
    yield
    storage._MEM_RUNS.clear()
    memory._MEM_AGENT_MEMORY.clear()


@pytest.fixture
def fake_llm():
    """Install a FakeLLM as the transport for the duration of a test."""
    fake = FakeLLM()
    previous = llm.set_transport(fake)
    try:
        yield fake
    finally:
        llm.set_transport(previous)


@pytest.fixture
async def run_state() -> RunState:
    """A freshly created run with memory seeded, ready for a day loop."""
    state = building.build_run_state(
        building_id="001",
        opening_text="Buongiorno a tutti, la caldaia e' ferma da stanotte.",
    )
    await storage.save_run(state)
    await memory.initialize_run_memory(state)
    return state
