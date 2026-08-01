"""HTTP surface, offline.

Exercises the real FastAPI app (routing, lifespan, admin endpoints) against
the in-memory store and the scripted LLM. Catches the integration mistakes
unit tests miss — a bad import in main.py, an endpoint that forgets the
timeline allocator, a lock that never releases.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app, limiter


@pytest.fixture
def client():
    # The per-IP rate limits (5 run creations/minute) are real production
    # behaviour, but every test here comes from the same "IP". Disable for
    # the duration and restore, so the limiter itself stays configured.
    was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        with TestClient(app) as c:
            yield c
    finally:
        limiter.enabled = was_enabled


def _create_run(client) -> dict:
    resp = client.post(
        "/api/runs",
        json={"opening_text": "La caldaia e' ferma da stanotte.", "building_id": "001"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_is_open(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_create_run_requires_an_opening(client):
    resp = client.post("/api/runs", json={"opening_text": "   ", "building_id": "001"})
    assert resp.status_code == 400, "the admin's first message IS the scenario"


def test_create_run_returns_a_playable_state(client):
    state = _create_run(client)
    assert state["run_id"].startswith("run_")
    assert len(state["agents"]) == 5
    assert len(state["messages"]) == 1
    assert state["messages"][0]["seq"] == 1
    assert state["ended"] is False
    assert state["ended_reason"] is None


def test_get_run_round_trips(client):
    created = _create_run(client)
    fetched = client.get(f"/api/runs/{created['run_id']}").json()
    assert fetched["run_id"] == created["run_id"]


def test_missing_run_is_404(client):
    assert client.get("/api/runs/run_doesnotexist").status_code == 404


def test_announce_appends_after_everything_else(client):
    """The admin's message must land at the END of the transcript."""
    state = _create_run(client)
    run_id = state["run_id"]

    resp = client.post(
        f"/api/runs/{run_id}/admin/announce",
        json={"text": "Aggiornamento: arriva il tecnico domani."},
    )
    assert resp.status_code == 200, resp.text
    posted = resp.json()["message"]

    fresh = client.get(f"/api/runs/{run_id}").json()
    ordered = sorted(
        fresh["messages"],
        key=lambda m: (m["fictional_timestamp_minutes"], m["seq"]),
    )
    assert ordered[-1]["id"] == posted["id"]
    assert posted["seq"] > 1


def test_announce_rejects_empty_text(client):
    state = _create_run(client)
    resp = client.post(
        f"/api/runs/{state['run_id']}/admin/announce", json={"text": "  "}
    )
    assert resp.status_code == 400


def test_dm_creates_the_chat_and_orders_correctly(client):
    state = _create_run(client)
    run_id = state["run_id"]

    resp = client.post(
        f"/api/runs/{run_id}/admin/dm",
        json={"recipient_id": "conti", "text": "Signora Conti, una parola."},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["message"]["chat_id"].startswith("dm_admin_conti")

    fresh = client.get(f"/api/runs/{run_id}").json()
    dm_chats = [c for c in fresh["chats"] if c["kind"] == "dm"]
    assert len(dm_chats) == 1
    assert set(dm_chats[0]["member_ids"]) == {"admin", "conti"}


def test_dm_to_unknown_recipient_is_rejected(client):
    state = _create_run(client)
    resp = client.post(
        f"/api/runs/{state['run_id']}/admin/dm",
        json={"recipient_id": "nessuno", "text": "ciao"},
    )
    assert resp.status_code == 400


def test_motion_lifecycle(client):
    state = _create_run(client)
    run_id = state["run_id"]

    filed = client.post(
        f"/api/runs/{run_id}/motions",
        json={"title": "Secondo preventivo", "description": "Chiediamo un altro tecnico."},
    )
    assert filed.status_code == 200, filed.text
    motion_id = filed.json()["motion"]["id"]

    closed = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")
    assert closed.status_code == 200
    assert closed.json()["motion"]["status"] in ("passed", "failed")

    # Closing twice is a no-op, not an error.
    again = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")
    assert again.json()["ok"] is False


def test_agent_goal_is_stored_and_returned(client):
    state = _create_run(client)
    run_id = state["run_id"]
    goal = "Ti è venuta voglia di vendere e andartene."

    resp = client.put(f"/api/runs/{run_id}/agents/conti/goal", json={"goal": goal})
    assert resp.status_code == 200
    assert resp.json()["goal"] == goal

    fresh = client.get(f"/api/runs/{run_id}").json()
    conti = next(a for a in fresh["agents"] if a["persona"]["id"] == "conti")
    assert conti["admin_goal"] == goal


def test_soul_and_memory_endpoints(client):
    state = _create_run(client)
    run_id = state["run_id"]

    soul = client.get(f"/api/runs/{run_id}/agents/conti/soul").json()["content"]
    assert soul.strip(), "SOUL is authored content and must not be empty"

    mem = client.get(f"/api/runs/{run_id}/agents/conti/memory").json()["content"]
    assert mem.strip(), "MEMORY is seeded at run creation"

    assert client.get(f"/api/runs/{run_id}/agents/nobody/soul").status_code == 404


def test_advance_day_returns_202_style_ack_immediately(client, fake_llm):
    """The POST must not block on the day loop — Heroku kills requests at 30s."""
    state = _create_run(client)
    resp = client.post(f"/api/runs/{state['run_id']}/advance_day")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "running"}
