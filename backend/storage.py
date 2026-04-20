"""JSON file persistence for runs."""
from __future__ import annotations

import json
from pathlib import Path

from .config import RUNS_DIR
from .models import RunState


def run_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def save_run(state: RunState) -> None:
    path = run_path(state.run_id)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def load_run(run_id: str) -> RunState | None:
    path = run_path(run_id)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    return RunState.model_validate_json(raw)


def list_runs() -> list[str]:
    return sorted(p.stem for p in RUNS_DIR.glob("*.json"))
