"""Shared logging helpers.

Prints to stderr (flows through uvicorn log) and keeps the last N lines in a
ring buffer exposed via /api/debug/logs for in-browser inspection.
"""
from __future__ import annotations

import sys
import time
from collections import deque

from .config import VERBOSE_LOGGING

_BUFFER: deque[str] = deque(maxlen=2000)


def _emit(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
    _BUFFER.append(line)


def log(tag: str, msg: str) -> None:
    if not VERBOSE_LOGGING:
        return
    ts = time.strftime("%H:%M:%S")
    _emit(f"[{ts}] [{tag}] {msg}")


def log_error(tag: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    _emit(f"[{ts}] [{tag}] ERROR {msg}")


def tail_logs(n: int = 300) -> list[str]:
    return list(_BUFFER)[-n:]


def clear_logs() -> None:
    _BUFFER.clear()
