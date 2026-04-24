"""FastAPI app exposing the admin console + scheduler."""
from __future__ import annotations

import asyncio
import hmac
import random
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import memory
from .config import (
    ADMIN_PASSWORD,
    BACKEND_PORT,
    DISABLED,
    HOST,
    PROJECT_ROOT,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_SECONDS,
    SESSION_SECRET,
)
from .db import close_pool, init_pool
from .dials import apply_trust_from_votes
from .events import bus
from .events_pool import compute_suggestions
from .logging_utils import log, log_error, tail_logs
from .models import Message, Motion, RunState
from .building import build_run_state
from .scheduler import active_loop, advance_to_next_day, day_end_minutes, day_start_minutes, run_day, setup_day
from .storage import list_runs, load_run, save_run, state_lock


# ---------------------------------------------------------------------------
# Lifespan: open Postgres pool on startup, close on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(title="Condominio", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Rate limiter (per-IP). slowapi installs an exception handler that returns
# 429 when a route's limit is exceeded.
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Auth + kill switch
# ---------------------------------------------------------------------------

# Session cookie signer. itsdangerous wraps the payload with an HMAC + a
# timestamp so we can both verify integrity and expire old cookies. The
# salt is just a namespace to keep these tokens distinct from any other
# itsdangerous use in the project.
_serializer = URLSafeTimedSerializer(
    SESSION_SECRET or "dev-only-insecure-secret",
    salt="condosim-session",
)

# /api/* paths that bypass the auth gate. Login itself can't require auth
# (chicken-and-egg); health is used by the frontend to discover whether
# the user already has a valid cookie.
_PUBLIC_API_PATHS = frozenset({"/api/health", "/api/login", "/api/logout"})


def _auth_required() -> bool:
    """Auth is opt-in: only enforced when BOTH ADMIN_PASSWORD and
    SESSION_SECRET are set. Leaving one unset means "open beta / public
    demo" — anyone can use the site. The €10 OpenRouter cap + per-IP rate
    limits are the financial safety net in that mode.
    """
    return bool(ADMIN_PASSWORD and SESSION_SECRET)


def _is_authenticated(request: Request) -> bool:
    """True iff the request carries a valid, non-expired session cookie
    — OR auth is disabled entirely on the server."""
    if not _auth_required():
        return True
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


@app.middleware("http")
async def auth_kill_switch_mw(request: Request, call_next):
    path = request.url.path
    # Non-API paths (the static SPA) are never gated.
    if not path.startswith("/api/"):
        return await call_next(request)
    # Kill switch: stops every /api/* except /api/health so an operator
    # can flip a config var to halt all LLM-spending endpoints in seconds.
    if DISABLED and path != "/api/health":
        return JSONResponse(
            status_code=503,
            content={"detail": "Servizio temporaneamente disattivato."},
        )
    if path in _PUBLIC_API_PATHS:
        return await call_next(request)
    # If auth isn't configured, let all /api/* through.
    if not _auth_required():
        return await call_next(request)
    if not _is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Non autorizzato."},
        )
    return await call_next(request)


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

async def _get_run(run_id: str) -> RunState:
    state = await load_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return state


async def _get_run_for_mutation(run_id: str) -> RunState:
    """Fetch the authoritative state for a mutation. If a day loop is
    running, return its in-memory state object so writes go to the same
    instance the scheduler is iterating. Otherwise load from DB.

    MUST be called inside `async with state_lock(run_id):` so that the
    "is a loop registered?" check and the subsequent mutation/save form
    one critical section. Without the lock, a `_run_day_bg` racing in
    parallel could load+register between this check and the save, and
    its later save would clobber the admin's write.
    """
    loop = active_loop(run_id)
    if loop is not None:
        return loop.state
    return await _get_run(run_id)


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
        cascaded=False,
    )
    state.messages.append(msg)
    return msg


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(request: Request):
    return {
        "ok": True,
        "authenticated": _is_authenticated(request),
        "auth_configured": _auth_required(),
        "disabled": DISABLED,
    }


class LoginPayload(BaseModel):
    password: str


@app.post("/api/login")
@limiter.limit("10/minute")
async def api_login(request: Request, response: Response, payload: LoginPayload):
    if not ADMIN_PASSWORD or not SESSION_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Auth non configurata: ADMIN_PASSWORD o SESSION_SECRET mancanti.",
        )
    if not hmac.compare_digest(payload.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Password errata.")
    token = _serializer.dumps({"u": "admin"})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/runs")
async def api_list_runs():
    return {"runs": await list_runs()}


class CreateRunPayload(BaseModel):
    opening_text: str
    building_id: str = "001"


@app.post("/api/runs")
@limiter.limit("5/minute")
async def api_create_run(request: Request, payload: CreateRunPayload):
    try:
        state = build_run_state(building_id=payload.building_id, opening_text=payload.opening_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await save_run(state)
    await memory.initialize_run_memory(state)
    return state.model_dump()


@app.get("/api/default_opening")
def api_default_opening():
    # The scenario is whatever the admin types. No canned opening.
    return {"text": ""}


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    state = await _get_run(run_id)
    return state.model_dump()


async def _run_day_bg(run_id: str, lock: asyncio.Lock) -> None:
    """Execute a day in the background. Always publishes a `day_done` SSE
    event and releases the lock — so a crashed or stuck day still unblocks
    the next advance instead of wedging the run forever.

    `advance_to_next_day` saves the run + runs memory consolidation
    internally (see scheduler.DayLoop.run); this helper just shepherds
    errors and signals completion.
    """
    success = False
    final_day: int | None = None
    state = None
    day_loop = None
    try:
        # state_lock guards load + setup_day. We MUST register the loop in
        # _ACTIVE_LOOPS before releasing the lock, so any admin endpoint
        # waiting on state_lock will then see the loop via active_loop()
        # and operate on the shared in-memory state instead of re-loading
        # a stale copy from the DB. Once setup_day returns, the lock can
        # safely be released — admin mutations land on loop.state and the
        # scheduler picks them up.
        async with state_lock(run_id):
            state = await load_run(run_id)
            if state is None:
                log_error("api", f"_run_day_bg: run {run_id} not found")
                return
            if state.ended:
                log("api", f"_run_day_bg: run {run_id} already ended, skipping")
                return
            messages_before = len(state.messages)
            log("api", f"advance_day(bg) run={run_id} from day={state.clock.day}")
            day_loop = setup_day(state)
        # state_lock released. Run the day; admin endpoints can now mutate
        # loop.state safely under their own state_lock acquisitions.
        try:
            await run_day(day_loop)
        except Exception as exc:
            log_error("api", f"advance_day(bg) failed: {exc!r}")
            bus().publish(run_id, "error", {"message": f"advance_day failed: {exc}"})
            return
        success = True
        final_day = state.clock.day
        new_msgs = len(state.messages) - messages_before
        log("api", f"advance_day(bg) done. {new_msgs} new msgs. Now day {final_day}.")
    finally:
        # day_done is the chain trigger — frontend listens for this to
        # schedule the next advance. Fire it BEFORE releasing the lock so
        # a frontend POST that races with day_done won't beat the release
        # and 409. (We publish first; subscribers receive asynchronously.)
        bus().publish(run_id, "day_done", {"ok": success, "day": final_day})
        if lock.locked():
            lock.release()


@app.post("/api/runs/{run_id}/advance_day")
@limiter.limit("30/minute")
async def api_advance_day(request: Request, run_id: str):
    """Kick off the next fictional day as a background task and return 202.

    The day itself can take 60–200s (full LLM cascade), which would blow
    Heroku's 30s HTTP timeout if we awaited it inline. The frontend listens
    for the `day_done` SSE event to know the day finished and to chain the
    next advance.
    """
    lock = bus().lock(run_id)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Un giorno è già in corso. Attendi che finisca prima di avanzare."
        )
    state = await _get_run(run_id)
    if state.ended:
        return {"ok": False, "reason": "La partita è già conclusa.", "state": state.model_dump()}
    await lock.acquire()
    asyncio.create_task(_run_day_bg(run_id, lock))
    return {"ok": True, "status": "running"}


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
async def api_quick_action(run_id: str, payload: QuickActionPayload):
    action = QUICK_ACTIONS.get(payload.action)
    if action is None:
        raise HTTPException(status_code=400, detail=f"Azione sconosciuta: {payload.action}")
    _label, body_fn = action
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        loop = active_loop(run_id)
        body = body_fn(state)
        audience = sorted(_resident_ids(state))
        msg = _append_admin_message(state, "main", body, audience)
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
        if loop is not None:
            loop.schedule_reactions(msg, depth=0, force=True)
        await save_run(state)
    return {"ok": True, "message": msg.model_dump()}


@app.get("/api/runs/{run_id}/suggestions")
async def api_run_suggestions(run_id: str):
    state = await _get_run(run_id)
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
async def api_file_motion(run_id: str, payload: MotionPayload):
    if not payload.title.strip() or not payload.description.strip():
        raise HTTPException(status_code=400, detail="Titolo e descrizione obbligatori")
    from uuid import uuid4 as _uuid
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        loop = active_loop(run_id)
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
        await save_run(state)
    return {"ok": True, "motion": motion.model_dump()}


@app.get("/api/runs/{run_id}/agents/{agent_id}/soul")
async def api_get_agent_soul(run_id: str, agent_id: str):
    """Return the agent's immutable SOUL.md as raw markdown."""
    state = await _get_run(run_id)
    if not any(a.persona.id == agent_id for a in state.agents):
        raise HTTPException(status_code=404, detail="Residente non trovato")
    return {"content": memory.read_soul(state, agent_id)}


@app.get("/api/runs/{run_id}/agents/{agent_id}/memory")
async def api_get_agent_memory(run_id: str, agent_id: str):
    """Return the agent's growing MEMORY (bio seed + day-end diary entries)."""
    state = await _get_run(run_id)
    if not any(a.persona.id == agent_id for a in state.agents):
        raise HTTPException(status_code=404, detail="Residente non trovato")
    return {"content": await memory.read_memory(state, agent_id)}


@app.put("/api/runs/{run_id}/agents/{agent_id}/goal")
async def api_set_agent_goal(run_id: str, agent_id: str, payload: AgentGoalPayload):
    """Admin sets an additional goal that the agent internalises as their own
    in the next activation. Framed in-fiction, never as 'admin said X'."""
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        agent = next((a for a in state.agents if a.persona.id == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail="Residente non trovato")
        agent.admin_goal = (payload.goal or "").strip()
        bus().publish(run_id, "agent_goal_updated", {
            "agent_id": agent_id,
            "has_goal": bool(agent.admin_goal),
        })
        await save_run(state)
    return {"ok": True, "agent_id": agent_id, "goal": agent.admin_goal}


@app.post("/api/runs/{run_id}/motions/{motion_id}/close")
async def api_close_motion(run_id: str, motion_id: str):
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
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
        await save_run(state)
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
async def api_admin_announce(run_id: str, payload: AnnouncePayload):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        loop = active_loop(run_id)
        audience = sorted(_resident_ids(state))
        msg = _append_admin_message(state, "main", payload.text, audience)
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
        # Kick off reactions if a day is live — force so an admin announcement
        # always gets responses rather than being filtered by probability rolls.
        if loop is not None:
            loop.schedule_reactions(msg, depth=0, force=True)
        await save_run(state)
    return {"ok": True, "message": msg.model_dump()}


@app.post("/api/runs/{run_id}/admin/dm")
async def api_admin_dm(run_id: str, payload: DMPayload):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        loop = active_loop(run_id)
        if payload.recipient_id not in _resident_ids(state):
            raise HTTPException(status_code=400, detail="Destinatario non valido")
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
        await save_run(state)
    return {"ok": True, "message": msg.model_dump()}


# ---------------------------------------------------------------------------
# Static SPA mount. Runs LAST so /api/* routes are matched first. Heroku
# build runs `npm run build` which writes to frontend/dist/. In local dev
# without that build, the mount is skipped and the user runs `vite dev`
# on the side.
# ---------------------------------------------------------------------------

_dist_dir = PROJECT_ROOT / "frontend" / "dist"
if _dist_dir.exists():
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="spa")
else:
    log("server", f"frontend/dist not found at {_dist_dir}; skipping SPA mount (dev mode)")


def main() -> None:
    import uvicorn

    uvicorn.run("backend.main:app", host=HOST, port=BACKEND_PORT, reload=False)


if __name__ == "__main__":
    main()
