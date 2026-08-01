"""A deterministic stand-in for OpenRouter.

Lets the entire day loop — scheduler, activation, tools, containment, memory
consolidation — run offline, instantly, and for free. That matters here for
two reasons: there is a hard credit cap on the real key, and the only
previous regression check (`scripts/run_smoketest.py`) cost money and took
minutes per run, so in practice nothing got tested.

The fake speaks the OpenAI tool-calling wire format that
`backend.openrouter.chat_completion` returns: a dict with `content`,
optional `tool_calls`, plus the `_usage` / `_model` metadata that
`record_usage` reads.
"""
from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


def tool_call(name: str, **arguments: Any) -> dict[str, Any]:
    """Build one OpenAI-format tool call."""
    return {
        "id": f"call_{name}_{next(_ids)}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


_ids = itertools.count(1)


def reply(
    *calls: dict[str, Any],
    content: str = "",
    prompt_tokens: int = 900,
    completion_tokens: int = 40,
    model: str = "fake/model",
) -> dict[str, Any]:
    """Build one assistant response."""
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "_model": model,
    }
    if calls:
        msg["tool_calls"] = list(calls)
    return msg


@dataclass
class FakeLLM:
    """Records every call and answers via a scripted responder.

    `responder` receives the kwargs the gateway was called with and returns
    an assistant message. The default behaviour is "say something plausible
    in the main group, once per activation", which exercises the send path,
    the containment filter and the implicit-done exit.
    """

    responder: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    # Monotonic per-activation counter, used only to vary the default script's
    # message text. It used to send a byte-identical line on every activation,
    # which no real model does and which the self-repeat guard in tools.py now
    # (correctly) refuses — an unvarying fake would both fail that guard and
    # hide the very bug it exists to catch.
    _turn: int = 0

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.responder is not None:
            return self.responder(kwargs)
        return self._default(kwargs)

    # -- helpers ---------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def callers(self) -> list[str]:
        return [c.get("caller", "?") for c in self.calls]

    def activation_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if str(c.get("caller", "")).startswith("agent:")]

    def calls_for(self, agent_id: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if f"agent:{agent_id}:" in str(c.get("caller", ""))]

    def last_prompt(self) -> str:
        msgs = self.calls[-1]["messages"]
        return "\n\n".join(str(m.get("content") or "") for m in msgs)

    # -- default script --------------------------------------------------

    def _default(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        caller = str(kwargs.get("caller", ""))
        if caller.startswith("memory:"):
            return reply(
                content="Cosa è successo:\nGiornata normale, si è parlato della caldaia.\n\n"
                "Cose da ricordare:\n- Controllare il preventivo.",
                completion_tokens=60,
            )
        agent_id = _agent_id_from_caller(caller)
        step = _step_from_caller(caller)
        # One send on the first step; if the loop ever comes back for another
        # step, close the phone so a test can't spin forever.
        if step == 0:
            self._turn += 1
            return reply(
                tool_call(
                    "send_message",
                    chat_id="Condominio Via Garibaldi",
                    text=f"Messaggio di prova {self._turn} da {agent_id}.",
                )
            )
        return reply(tool_call("done"))


_CALLER_RE = re.compile(r"^agent:([^:]+):step(\d+)")


def _agent_id_from_caller(caller: str) -> str:
    m = _CALLER_RE.match(caller)
    return m.group(1) if m else "?"


def _step_from_caller(caller: str) -> int:
    m = _CALLER_RE.match(caller)
    return int(m.group(2)) if m else 0
