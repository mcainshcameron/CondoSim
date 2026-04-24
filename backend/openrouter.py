"""Thin async OpenRouter client with tool-use support + 429/fallback handling."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from .config import AGENT_FALLBACK_MODELS, OPENROUTER_API_KEY, OPENROUTER_BASE_URL
from .logging_utils import log, log_error


class OpenRouterError(RuntimeError):
    pass


# Retry policy for transient failures (429 rate-limits, 5xx upstream errors).
_RETRY_STATUSES = {429, 502, 503, 504}
_RETRY_BACKOFF_SEC = [1.5, 3.0]  # two retries then fall back to next model


async def _single_call(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    max_tokens: int,
    timeout: float,
    caller: str,
) -> tuple[int, dict[str, Any] | None, str]:
    """Return (status_code, response_json_or_None, error_body_text)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8001",
        "X-Title": "Condominio",
    }

    start = time.time()
    log("openrouter", f"-> {caller} model={model} messages={len(messages)} tools={len(tools) if tools else 0}")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
    except Exception as exc:
        log_error("openrouter", f"{caller} HTTP transport error: {exc}")
        return (0, None, f"transport: {exc}")

    elapsed = time.time() - start
    if resp.status_code != 200:
        body_snip = resp.text[:500]
        log_error("openrouter", f"{caller} status={resp.status_code} in {elapsed:.1f}s body={body_snip}")
        return (resp.status_code, None, body_snip)

    data = resp.json()
    usage = data.get("usage", {})
    choices = data.get("choices") or []
    msg = choices[0]["message"] if choices else None
    content_len = len(msg.get("content") or "") if msg else 0
    tool_calls = (msg.get("tool_calls") or []) if msg else []
    log(
        "openrouter",
        f"<- {caller} {elapsed:.1f}s content={content_len}ch tools={len(tool_calls)} "
        f"in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}",
    )
    return (200, data, "")


async def chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.8,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    caller: str = "?",
    fallback_models: list[str] | None = None,
) -> dict[str, Any]:
    """Try model with retries; on persistent 429/5xx, cascade through fallback_models."""
    if not OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and fill it in.")
    if fallback_models is None:
        fallback_models = list(AGENT_FALLBACK_MODELS)

    models_to_try = [model, *fallback_models]
    last_error = ""
    last_status = 0
    overall_start = time.time()

    for i, m in enumerate(models_to_try):
        call_caller = caller if i == 0 else f"{caller}(fallback:{m})"
        for attempt, backoff in enumerate([0.0, *_RETRY_BACKOFF_SEC]):
            if backoff > 0:
                log("openrouter", f"{call_caller} retry in {backoff}s (attempt {attempt + 1}/{len(_RETRY_BACKOFF_SEC) + 1})")
                await asyncio.sleep(backoff)
            attempt_tag = f"{call_caller}#att{attempt + 1}"
            status, data, err = await _single_call(
                model=m,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                caller=attempt_tag,
            )
            if status == 200 and data:
                choices = data.get("choices") or []
                if not choices:
                    raise OpenRouterError(f"No choices in response: {json.dumps(data)[:400]}")
                total = time.time() - overall_start
                # Only bother with the "took total X" line if there were any
                # retries/fallbacks — otherwise single-call log is sufficient.
                if attempt > 0 or i > 0:
                    log("openrouter", f"{caller} succeeded after model#{i + 1} attempt#{attempt + 1} total={total:.1f}s")
                return choices[0]["message"]
            last_status = status
            last_error = err
            # Only retry on transient statuses; fall through otherwise
            if status not in _RETRY_STATUSES:
                log("openrouter", f"{attempt_tag} non-retryable status {status}, falling through")
                break
        # All retries for this model exhausted; try the next model in the cascade
        log_error("openrouter", f"{call_caller} exhausted {len(_RETRY_BACKOFF_SEC) + 1} attempts (last status {last_status})")

    total = time.time() - overall_start
    raise OpenRouterError(
        f"All models failed after {total:.1f}s (last status {last_status}): {last_error}"
    )
