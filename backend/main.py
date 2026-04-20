"""FastAPI app exposing the admin console + scheduler."""
from __future__ import annotations

import random
from datetime import datetime
from uuid import uuid4

import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import BACKEND_PORT, FRONTEND_ORIGINS, HOST
from .dials import apply_trust_from_votes
from .events import bus
from .events_pool import compute_suggestions
from .logging_utils import log, log_error, tail_logs
from .models import Message, Motion, RunState
from .scenarios.heating_crisis import DEFAULT_OPENING_TEXT, build_run_state
from .scheduler import active_loop, advance_to_next_day, day_end_minutes, day_start_minutes
from .storage import list_runs, load_run, save_run


app = FastAPI(title="Condominio")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AnnouncePayload(BaseModel):
    text: str


class DMPayload(BaseModel):
    recipient_id: str
    text: str


class QuickActionPayload(BaseModel):
    action: str  # see TEMPLATE_ACTIONS below


class MotionPayload(BaseModel):
    title: str
    description: str


class VotePayload(BaseModel):
    agent_id: str  # resident id or "admin"
    choice: str  # "yes" | "no" | "abstain"


class AgentGoalPayload(BaseModel):
    goal: str  # empty string clears it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_run(run_id: str) -> RunState:
    state = load_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return state


def _resident_ids(state: RunState) -> set[str]:
    return {a.persona.id for a in state.agents}


def _append_admin_message(state: RunState, chat_id: str, text: str, audience: list[str]) -> Message:
    # Advance fictional time by a small amount so it sits after prior messages
    state.clock.minutes_since_start = max(
        state.clock.minutes_since_start + random.randint(1, 3),
        day_start_minutes(state.clock.day),
    )
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=chat_id,
        sender_id="admin",
        sender_kind="admin",
        sender_display_name="Amministratore",
        content=text.strip(),
        fictional_timestamp_minutes=state.clock.minutes_since_start,
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=state.clock.day,
        audience=audience,
    )
    state.messages.append(msg)
    return msg


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/runs")
def api_list_runs():
    return {"runs": list_runs()}


class CreateRunPayload(BaseModel):
    opening_text: str | None = None


@app.post("/api/runs")
def api_create_run(payload: CreateRunPayload | None = None):
    opening = (payload.opening_text if payload else None) or None
    state = build_run_state(opening_text=opening)
    save_run(state)
    return state.model_dump()


@app.get("/api/default_opening")
def api_default_opening():
    return {"text": DEFAULT_OPENING_TEXT}


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    return _get_run(run_id).model_dump()


@app.post("/api/runs/{run_id}/advance_day")
async def api_advance_day(run_id: str):
    # Per-run lock: reject the second click if a day is already advancing.
    lock = bus().lock(run_id)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Un giorno è già in corso. Attendi che finisca prima di avanzare."
        )
    async with lock:
        state = _get_run(run_id)
        if state.ended:
            return {"ok": False, "reason": "La partita è già conclusa.", "state": state.model_dump()}
        messages_before = len(state.messages)
        log("api", f"advance_day run={run_id} from day={state.clock.day}")
        try:
            await advance_to_next_day(state)
        except Exception as exc:
            log_error("api", f"advance_day failed: {exc!r}")
            bus().publish(run_id, "error", {"message": f"advance_day failed: {exc}"})
            raise HTTPException(status_code=500, detail=f"advance_day failed: {exc}")
        save_run(state)
        new_msgs = len(state.messages) - messages_before
        log("api", f"advance_day done. Produced {new_msgs} new messages. Now on day {state.clock.day}.")
        return {"ok": True, "new_messages": new_msgs, "state": state.model_dump()}


# Pre-baked admin quick actions. action_id -> (label, main-chat body)
QUICK_ACTIONS = {
    "request_second_quote": (
        "Richiedi altro preventivo",
        lambda state: (
            "Ho contattato un'altra ditta per un preventivo alternativo. "
            "Vi aggiorno appena arriva."
        ),
    ),
    "call_emergency_assembly": (
        "Convoca assemblea urgente",
        lambda state: (
            "🏛️ Convoco un'assemblea condominiale straordinaria per domani "
            "sera alle 21:00 (seconda convocazione). Ordine del giorno: "
            "questioni aperte del condominio. La vostra presenza è necessaria."
        ),
    ),
    "share_morosi_status": (
        "Stato morosi",
        lambda state: (
            "📋 Aggiornamento sui pagamenti: al momento alcuni condomini sono "
            "indietro con le quote. Li contatterò direttamente."
        ),
    ),
}


@app.post("/api/runs/{run_id}/admin/quick_action")
def api_quick_action(run_id: str, payload: QuickActionPayload):
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    action = QUICK_ACTIONS.get(payload.action)
    if action is None:
        raise HTTPException(status_code=400, detail=f"Azione sconosciuta: {payload.action}")
    _label, body_fn = action
    body = body_fn(state)
    audience = sorted(_resident_ids(state))
    msg = _append_admin_message(state, "main", body, audience)
    bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
    if loop is not None:
        loop.schedule_reactions(msg, depth=0, force=True)
    save_run(state)
    return {"ok": True, "message": msg.model_dump()}


@app.get("/api/runs/{run_id}/suggestions")
def api_run_suggestions(run_id: str):
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    return {"suggestions": compute_suggestions(state)}


@app.get("/api/quick_actions")
def api_list_quick_actions():
    return {
        "actions": [{"id": k, "label": v[0]} for k, v in QUICK_ACTIONS.items()],
        "motion_templates": [],
    }


@app.post("/api/runs/{run_id}/motions")
def api_file_motion(run_id: str, payload: MotionPayload):
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    if not payload.title.strip() or not payload.description.strip():
        raise HTTPException(status_code=400, detail="Titolo e descrizione obbligatori")
    from uuid import uuid4 as _uuid
    motion = Motion(
        id=f"m_{_uuid().hex[:8]}",
        title=payload.title.strip(),
        description=payload.description.strip(),
        proposer_id="admin",
        proposer_display_name="Amministratore",
        day_proposed=state.clock.day,
        proposed_at_fictional_min=state.clock.minutes_since_start,
    )
    state.motions.append(motion)
    audience = sorted(_resident_ids(state))
    body = (
        f"📋 Mozione depositata: \"{motion.title}\"\n"
        f"{motion.description}\n"
        f"(votate con sì / no / astenuto — codice mozione: {motion.id})"
    )
    msg = _append_admin_message(state, "main", body, audience)
    bus().publish(run_id, "motion_filed", {"motion": motion.model_dump()})
    bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
    if loop is not None:
        loop.schedule_reactions(msg, depth=0, force=True)
    save_run(state)
    return {"ok": True, "motion": motion.model_dump()}


@app.put("/api/runs/{run_id}/agents/{agent_id}/goal")
def api_set_agent_goal(run_id: str, agent_id: str, payload: AgentGoalPayload):
    """Admin sets an additional goal that the agent internalises as their own
    in the next activation. Framed in-fiction, never as 'admin said X'."""
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    agent = next((a for a in state.agents if a.persona.id == agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Residente non trovato")
    agent.admin_goal = (payload.goal or "").strip()
    bus().publish(run_id, "agent_goal_updated", {
        "agent_id": agent_id,
        "has_goal": bool(agent.admin_goal),
    })
    save_run(state)
    return {"ok": True, "agent_id": agent_id, "goal": agent.admin_goal}


@app.post("/api/runs/{run_id}/motions/{motion_id}/close")
def api_close_motion(run_id: str, motion_id: str):
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    motion = next((m for m in state.motions if m.id == motion_id), None)
    if motion is None:
        raise HTTPException(status_code=404, detail="Mozione non trovata")
    if motion.status != "open":
        return {"ok": False, "motion": motion.model_dump()}
    # Tally: one vote per resident, plus millesimi weighting per Italian law
    yes_ids = [aid for aid, v in motion.votes.items() if v == "yes"]
    no_ids = [aid for aid, v in motion.votes.items() if v == "no"]
    millesimi_by_id = {a.persona.id: a.persona.millesimi for a in state.agents}
    yes_mil = sum(millesimi_by_id.get(a, 0) for a in yes_ids)
    no_mil = sum(millesimi_by_id.get(a, 0) for a in no_ids)
    headcount_yes = len(yes_ids)
    headcount_no = len(no_ids)
    attending = headcount_yes + headcount_no
    # Seconda convocazione: ≥1/3 attending of 5 = 2. 500/1000 millesimi threshold.
    quorum_ok = attending >= 2
    passed = quorum_ok and headcount_yes > headcount_no and yes_mil >= 500
    motion.status = "passed" if passed else "failed"
    motion.closed_at_fictional_min = state.clock.minutes_since_start
    motion.outcome_note = (
        f"Sì: {headcount_yes} ({yes_mil}/1000 millesimi) · "
        f"No: {headcount_no} ({no_mil}/1000 millesimi) · "
        f"{'Approvata' if passed else 'Respinta'}"
    )
    apply_trust_from_votes(state, motion)

    audience = sorted(_resident_ids(state))
    body = f"🗳️ Chiusa votazione: \"{motion.title}\" — {motion.outcome_note}"
    msg = _append_admin_message(state, "main", body, audience)
    bus().publish(run_id, "motion_closed", {"motion": motion.model_dump()})
    bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
    save_run(state)
    return {"ok": True, "motion": motion.model_dump()}


@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str):
    """SSE stream of live events for a run: typing indicators, new messages, day lifecycle."""
    queue = bus().subscribe(run_id)

    async def gen():
        # Initial "connected" so the client knows the stream is open
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield ev.to_sse()
                except asyncio.TimeoutError:
                    # Keep-alive ping every 15s so proxies don't close the connection
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            bus().unsubscribe(run_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/debug/logs")
def api_debug_logs(n: int = 300):
    return {"lines": tail_logs(n)}


@app.post("/api/runs/{run_id}/admin/announce")
def api_admin_announce(run_id: str, payload: AnnouncePayload):
    state = _get_run(run_id)
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    # If a day loop is currently running, operate on ITS state instance so
    # the new message is in the shared object the scheduler is iterating on.
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    audience = sorted(_resident_ids(state))
    msg = _append_admin_message(state, "main", payload.text, audience)
    bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
    # Kick off reactions if a day is live — force so an admin announcement
    # always gets responses rather than being filtered by probability rolls.
    if loop is not None:
        loop.schedule_reactions(msg, depth=0, force=True)
    save_run(state)
    return {"ok": True, "message": msg.model_dump()}


@app.post("/api/runs/{run_id}/admin/dm")
def api_admin_dm(run_id: str, payload: DMPayload):
    state = _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    if payload.recipient_id not in _resident_ids(state):
        raise HTTPException(status_code=400, detail="Destinatario non valido")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    members = frozenset(["admin", payload.recipient_id])
    chat = None
    for c in state.chats:
        if c.kind == "dm" and frozenset(c.member_ids) == members:
            chat = c
            break
    if chat is None:
        from .models import Chat
        other_name = next(a.persona.display_name for a in state.agents if a.persona.id == payload.recipient_id)
        chat = Chat(
            id=f"dm_admin_{payload.recipient_id}_{uuid4().hex[:4]}",
            kind="dm",
            display_name=f"DM con {other_name}",
            member_ids=["admin", payload.recipient_id],
            created_day=state.clock.day,
        )
        state.chats.append(chat)
    msg = _append_admin_message(state, chat.id, text, [payload.recipient_id])
    bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": chat.model_dump()})
    # Force reaction: a private DM from the admin always deserves a reply
    # from the recipient, not a probability roll.
    if loop is not None:
        loop.schedule_reactions(msg, depth=0, force=True)
    save_run(state)
    return {"ok": True, "message": msg.model_dump()}


def main() -> None:
    import uvicorn

    uvicorn.run("backend.main:app", host=HOST, port=BACKEND_PORT, reload=False)


if __name__ == "__main__":
    main()
