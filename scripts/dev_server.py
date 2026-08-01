"""Run the real server with a scripted LLM and in-memory storage.

Click through the entire admin console — create a run, watch days advance,
announce, DM, file motions — without an OpenRouter key, without Supabase,
and without spending a cent. Useful for UI work, where the simulation only
needs to produce *plausible* traffic.

    python scripts/dev_server.py            # http://127.0.0.1:8001

Residents say canned lines here. For real dialogue, run `python -m
backend.main` against a live key.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DATABASE_URL"] = ""
os.environ.setdefault("OPENROUTER_API_KEY", "dev-fake")
os.environ.setdefault("SESSION_COOKIE_SECURE", "0")

from backend import config, llm  # noqa: E402
from tests.fake_llm import reply, tool_call  # noqa: E402

# Enough variety that the chat list, unread badges, pacing and reactions all
# get exercised. Deliberately mundane — this is scaffolding, not writing.
_LINES = [
    "ma quando arrivano questi preventivi?",
    "io non ci sto a pagare tutto adesso",
    "scusate ma qualcuno ha visto il tecnico?",
    "concordo con quello che ha detto prima",
    "non mi convince per niente",
    "ok per me va bene",
    "mah, vedremo",
    "e i millesimi come li calcoliamo?",
    "ci risiamo tutti gli anni uguale",
    "va bene, aspetto notizie",
]


def _responder(kwargs):
    caller = str(kwargs.get("caller", ""))
    if caller.startswith("memory:"):
        return reply(
            content="Cosa è successo:\nSolita giornata di discussioni sul palazzo.\n\n"
            "Cose da ricordare:\n- Tenere d'occhio chi si lamenta di più."
        )
    if not caller.endswith(":step0"):
        return reply(tool_call("done"))

    roll = random.random()
    if roll < 0.12:
        return reply(tool_call(
            "react_to_message",
            chat="Condominio Via Garibaldi",
            message_excerpt=random.choice(_LINES)[:20],
            emoji=random.choice(["👍", "🙄", "😡", "💯"]),
        ))
    if roll < 0.22:
        return reply(tool_call(
            "send_dm",
            recipient_id=random.choice(["Conti", "Greco", "Ferrari", "Marchetti", "Romano"]),
            text=random.choice(_LINES),
        ))
    return reply(tool_call(
        "send_message",
        chat_id="Condominio Via Garibaldi",
        text=random.choice(_LINES),
    ))


def main() -> None:
    import uvicorn

    from tests.fake_llm import FakeLLM

    llm.set_transport(FakeLLM(responder=_responder))
    print("\n  DEV MODE — scripted LLM, in-memory storage, no spend.")
    print(f"  http://{config.HOST}:{config.BACKEND_PORT}\n")
    uvicorn.run("backend.main:app", host=config.HOST, port=config.BACKEND_PORT, reload=False)


if __name__ == "__main__":
    main()
