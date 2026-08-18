"""FastAPI app exposing the admin console + scheduler."""
from __future__ import annotations

import asyncio
import hmac
import json
import random
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import memory, timeline
from .building import build_run_state
from .config import (
    ADMIN_PASSWORD,
    BACKEND_PORT,
    DATA_DIR,
    DEBUG_ENDPOINTS,
    DISABLED,
    HOST,
    PROJECT_ROOT,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_SECONDS,
    SESSION_SECRET,
    TRUST_PROXY_HEADERS,
)
from .db import close_pool, init_pool
from .dials import apply_trust_from_votes, publish_deltas
from .events import TooManySubscribers, bus
from .events_pool import compute_suggestions
from .logging_utils import log, log_error, tail_logs
from .models import Message, Motion, RunState
from .motions import tally_motion
from .scheduler import active_loop, day_start_minutes, run_day, setup_day
from .storage import load_run, run_exists, save_run, state_lock


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
#
# There is NO `default_limits=[...]` here, and that is deliberate. slowapi
# 0.1.9 only consults the defaults from two places: `SlowAPIASGIMiddleware`,
# and the per-route wrapper that `@limiter.limit(...)` installs. An endpoint
# with no decorator is never checked at all — the default was silently
# covering nothing. Measured before this was fixed: 70 back-to-back
# `GET /api/debug/logs` all returned 200 while the decorated `/api/login`
# correctly 429'd.
#
# The middleware is NOT the fix. In 0.1.9 its `send_wrapper` re-emits
# `http.response.start` for every body chunk, which breaks the SSE
# `StreamingResponse` in `api_run_events`. So every mutating endpoint carries
# an explicit `@limiter.limit(...)` instead, and each one needs a
# `request: Request` parameter — the decorator looks the connection up by
# name and raises at import time if it can't find it.
#
# `key_style="endpoint"` buckets by view function rather than by URL. With
# the default ("url") every run id is its own bucket, so `advance_day` —
# the only endpoint that spends money — would be limited per run instead of
# per caller, which is no limit at all for anyone willing to create runs.
# The bucket key is (client IP, endpoint), so separate operators never share
# a budget — but only if uvicorn is actually told the client IP. Behind the
# Heroku router every request arrives from the router's own address, and
# uvicorn's `forwarded_allow_ips` defaults to 127.0.0.1, so it ignores
# X-Forwarded-For and `get_remote_address` collapses the whole internet onto
# one key. That is what `--proxy-headers --forwarded-allow-ips='*'` in the
# Procfile is for; without it these limits are global, not per-IP.
# ---------------------------------------------------------------------------


def _client_key(request: Request) -> str:
    """Rate-limit identity for a caller.

    `get_remote_address` alone is NOT usable behind `forwarded_allow_ips='*'`.
    With that setting uvicorn's ProxyHeadersMiddleware sets `always_trust` and
    rewrites `scope["client"]` to the **first** X-Forwarded-For entry — the one
    the caller wrote. Heroku's router appends its own view of the client to the
    chain rather than replacing it, so entry 0 is attacker-controlled and every
    limit below becomes a formality: rotating it hands out a fresh bucket per
    request. Measured against the real app: 40 POSTs to `/api/login` with a
    fixed X-Forwarded-For gave 10×429; rotating only entry 0 gave 0×429.

    The last entry is the one our own proxy appended, so that is the one we
    key on — and only when `TRUST_PROXY_HEADERS` says a proxy we control is
    actually in front of us. Unset, we ignore the header entirely; a client
    that invents one on a directly-exposed server gets nothing for it.
    """
    if TRUST_PROXY_HEADERS:
        chain = request.headers.get("x-forwarded-for", "")
        if chain:
            last = chain.rsplit(",", 1)[-1].strip()
            if last:
                return last
    return get_remote_address(request)


limiter = Limiter(key_func=_client_key, key_style="endpoint")
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
#
# Every free-text field is length-bounded, and the bounds are a spend control,
# not tidiness. Admin text is re-inlined into the prompt on every activation
# for the rest of the run — `admin_goal` twice per call (system prompt +
# notification prompt), an announce body through the context digest — so a
# caller-supplied 100 KB string is roughly a 100× token multiplier against the
# ~$0.000064 baseline. The run would die on `cost_cap_exceeded` a hundred
# activations later with nothing in the logs pointing at the cause. Bounding
# here turns that into a 422 at the door, and keeps the documented
# "prompt size is bounded, not growing" invariant true for caller-controlled
# text too. The prompt sinks in agent.py clamp defensively as well; this is
# the cheap outer layer.
# ---------------------------------------------------------------------------

class AnnouncePayload(BaseModel):
    text: str = Field(max_length=4000)


class DMPayload(BaseModel):
    recipient_id: str = Field(max_length=64)
    text: str = Field(max_length=4000)


class MotionPayload(BaseModel):
    title: str = Field(max_length=200)
    description: str = Field(max_length=4000)


class VotePayload(BaseModel):
    agent_id: str = Field(max_length=64)  # resident id or "admin"
    choice: str = Field(max_length=16)  # "yes" | "no" | "abstain"


class AgentGoalPayload(BaseModel):
    goal: str = Field(max_length=2000)  # empty string clears it


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_run(run_id: str) -> RunState:
    state = await load_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return state


async def _require_run(run_id: str) -> None:
    """404 an unknown run id BEFORE any per-run state gets minted for it.

    `state_lock()`, `bus().lock()` and `bus().subscribe()` all read out of
    `defaultdict`s, so merely touching one with a garbage id creates a
    permanent entry — and the mutating endpoints have to take the lock
    *before* they can load the run (see `_get_run_for_mutation`), so the 404
    can't come first on its own. 70 POSTs to a nonexistent run id left 70
    locks behind in each dict. This probe is a bare existence check that
    materialises nothing.

    It is deliberately not `_get_run` (which deserializes the whole run) —
    the callers below load the state themselves once they hold the lock.
    """
    if not await run_exists(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")


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
    if bus().lock(run_id).locked():
        raise HTTPException(
            status_code=409,
            detail="Il giorno si sta chiudendo. Attendi la fine del consolidamento.",
        )
    return await _get_run(run_id)


def _resident_ids(state: RunState) -> set[str]:
    return {a.persona.id for a in state.agents}


def _find_agent_or_404(state: RunState, agent_id: str):
    agent = next((a for a in state.agents if a.persona.id == agent_id), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="Residente non trovato")
    return agent


def _append_admin_message(
    state: RunState,
    chat_id: str,
    text: str,
    audience: list[str],
    *,
    bookkeeping: bool = False,
) -> Message:
    """Append an admin-authored message at the front of the run's timeline.

    Pass `bookkeeping=True` for machine-authored records such as a vote
    tally — see `Message.bookkeeping`. Those land in the admin column but
    are not the administrator speaking, and nothing downstream should demand
    a reply to them.

    The timestamp goes through `timeline.allocate_minute`, which floors it at
    the newest message in the run. This matters because an admin can post
    while an agent is mid-activation: `state.clock` only catches up to that
    agent's messages once the activation finishes, so deriving the stamp from
    the clock alone used to place the admin's message *before* messages the
    browser had already rendered — the transcript then visibly reshuffled.
    """
    desired = max(
        state.clock.minutes_since_start + random.randint(1, 3),
        day_start_minutes(state.clock.day),
    )
    stamp = timeline.allocate_minute(state, desired)
    state.clock.minutes_since_start = max(state.clock.minutes_since_start, stamp)
    msg = Message(
        id=f"msg_{uuid4().hex[:8]}",
        chat_id=chat_id,
        sender_id="admin",
        sender_kind="admin",
        sender_display_name="Amministratore",
        content=text.strip(),
        fictional_timestamp_minutes=stamp,
        seq=timeline.next_seq(state),
        wall_clock_iso=datetime.utcnow().isoformat() + "Z",
        day=state.clock.day,
        audience=audience,
        cascaded=False,
        bookkeeping=bookkeeping,
    )
    state.messages.append(msg)
    return msg


# ---------------------------------------------------------------------------
# Endpoints
#
# Ordering rule for every mutating endpoint: `await save_run(state)` runs
# BEFORE any `bus().publish(...)` and before `loop.schedule_reactions(...)`.
# This is the same rule the day loop already obeys at day_end (see the
# comment above `save_run` in scheduler.DayLoop), applied to the admin
# endpoints, which used to invert it.
#
# It matters because `publish` is `put_nowait` — it returns without yielding,
# so the SSE subscriber is holding the message before the first `await` even
# happens. If the save then raises, the mutated `state` is a local `load_run`
# copy that gets discarded, while the browser keeps the orphan permanently:
# App.jsx merges messages and never removes them, and its `refetchAndMerge`
# replaces `motions`/`chats` wholesale, so the motion or DM chat the message
# referred to vanishes and the message stays.
#
# `schedule_reactions` goes after the save for a second reason: it stamps
# `cascaded=True` on the trigger message. Persisting that before the audience
# has actually reacted is the unsafe direction — it tells the next day's seed
# loop the obligation was already discharged. Persisting `False` costs at
# worst one redundant nudge.
#
# Corollary: `active_loop(run_id)` must be read AFTER that save, never before.
# `DayLoop.run` pops itself out of `_ACTIVE_LOOPS` without holding
# `state_lock`, so a loop looked up before an await can be dead by the time we
# use it — and a dead loop swallows the obligation while still stamping
# `cascaded=True`, which its own post-consolidation save then persists.
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


class CreateRunPayload(BaseModel):
    opening_text: str = Field(max_length=8000)
    building_id: str = Field(default="001", max_length=64)


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


@app.get("/api/opening_templates")
def api_opening_templates():
    path = DATA_DIR / "openings.json"
    if not path.exists():
        return {"templates": []}
    return {"templates": json.loads(path.read_text(encoding="utf-8"))}


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
        # schedule the next advance. Release first so a frontend POST that
        # races with day_done cannot hit a false 409.
        if lock.locked():
            lock.release()
        bus().publish(run_id, "day_done", {"ok": success, "day": final_day})


@app.post("/api/runs/{run_id}/advance_day")
# 120/minute, not the 30 the other mutating endpoints get. This endpoint is
# already serialised by the per-run lock — that, not the limiter, is what
# stops two days running at once — so the limit here is only a guard against a
# runaway client. 30 is below what the normal auto-advance chain produces: the
# frontend re-POSTs AUTO_ADVANCE_DELAY_MS (1.2s) after each `day_done`, so a
# fast day (tens of ms against the scripted LLM in scripts/dev_server.py)
# yields ~48 advances/minute. A 429 is not a 409, so `onAdvance` would take
# the fatal branch and stop the chain dead with no manual advance button to
# restart it.
@limiter.limit("120/minute")
async def api_advance_day(request: Request, run_id: str):
    """Kick off the next fictional day as a background task and return 202.

    A round-robin day with 4 rounds × 5 agents averages 60–250s of
    serialised LLM work, which would blow Heroku's 30s HTTP timeout if we
    awaited it inline. The frontend listens for the `day_done` SSE event
    to know the day finished and to chain the next advance.
    """
    await _require_run(run_id)
    lock = bus().lock(run_id)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Un giorno è già in corso. Attendi che finisca prima di avanzare."
        )
    # Acquire IMMEDIATELY after the check, with no await in between. These two
    # lines used to be separated by the `_get_run` below, which suspends on the
    # asyncpg round trip: two near-simultaneous POSTs both saw a free lock, the
    # second blocked in `acquire()` for the full 60-250s day and then started
    # an unrequested day N+1 — straight past the ⏸ Pausa brake, which only
    # cancels the frontend's own chain. `asyncio.Lock.acquire()` on a free lock
    # takes the uncontended fast path and returns without yielding, so nothing
    # can interleave here.
    await lock.acquire()
    started = False
    try:
        state = await _get_run(run_id)
        if state.ended:
            return {"ok": False, "reason": "La partita è già conclusa.", "state": state.model_dump()}
        asyncio.create_task(_run_day_bg(run_id, lock))
        started = True
    finally:
        # `_run_day_bg` is the only other place that releases this lock, and on
        # any path that doesn't reach `create_task` it never runs — so a 404, a
        # DB error or an already-ended run would wedge the run forever.
        if not started:
            lock.release()
    return {"ok": True, "status": "running"}


@app.get("/api/runs/{run_id}/suggestions")
async def api_run_suggestions(run_id: str):
    state = await _get_run(run_id)
    loop = active_loop(run_id)
    if loop is not None:
        state = loop.state
    return {"suggestions": compute_suggestions(state)}


@app.post("/api/runs/{run_id}/motions")
@limiter.limit("20/minute")
async def api_file_motion(request: Request, run_id: str, payload: MotionPayload):
    if not payload.title.strip() or not payload.description.strip():
        raise HTTPException(status_code=400, detail="Titolo e descrizione obbligatori")
    await _require_run(run_id)
    from uuid import uuid4 as _uuid
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
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
        # Save first — see the ordering rule at the top of this section.
        await save_run(state)
        bus().publish(run_id, "motion_filed", {"motion": motion.model_dump()})
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
        # Re-read the registry after the save — see `api_admin_announce` for
        # why a loop captured before an await cannot be trusted afterwards.
        loop = active_loop(run_id)
        if loop is not None:
            loop.schedule_reactions(msg, depth=0, force=True)
    return {"ok": True, "motion": motion.model_dump()}


@app.get("/api/runs/{run_id}/agents/{agent_id}/soul")
async def api_get_agent_soul(run_id: str, agent_id: str):
    """Return the agent's immutable SOUL.md as raw markdown."""
    state = await _get_run(run_id)
    _find_agent_or_404(state, agent_id)
    return {"content": memory.read_soul(state, agent_id)}


@app.get("/api/runs/{run_id}/agents/{agent_id}/memory")
async def api_get_agent_memory(run_id: str, agent_id: str):
    """Return the agent's growing MEMORY (bio seed + day-end diary entries)."""
    state = await _get_run(run_id)
    _find_agent_or_404(state, agent_id)
    return {"content": await memory.read_memory(state, agent_id)}


@app.put("/api/runs/{run_id}/agents/{agent_id}/goal")
@limiter.limit("20/minute")
async def api_set_agent_goal(
    request: Request, run_id: str, agent_id: str, payload: AgentGoalPayload
):
    """Admin sets an additional goal that the agent internalises as their own
    in the next activation. Framed in-fiction, never as 'admin said X'."""
    await _require_run(run_id)
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        agent = _find_agent_or_404(state, agent_id)
        agent.admin_goal = (payload.goal or "").strip()
        # Save first — see the ordering rule at the top of this section.
        await save_run(state)
        bus().publish(run_id, "agent_goal_updated", {
            "agent_id": agent_id,
            "has_goal": bool(agent.admin_goal),
        })
    return {"ok": True, "agent_id": agent_id, "goal": agent.admin_goal}


@app.post("/api/runs/{run_id}/motions/{motion_id}/close")
@limiter.limit("20/minute")
async def api_close_motion(request: Request, run_id: str, motion_id: str):
    await _require_run(run_id)
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        motion = next((m for m in state.motions if m.id == motion_id), None)
        if motion is None:
            raise HTTPException(status_code=404, detail="Mozione non trovata")
        if motion.status != "open":
            return {"ok": False, "motion": motion.model_dump()}
        # Tally: quorum plus millesimi weighting per Italian law. The rule
        # itself lives in backend/motions.py because the residents' own `vote`
        # tool can auto-close a motion too, and the two paths used to disagree.
        tally = tally_motion(state, motion)
        motion.status = tally.outcome  # type: ignore[assignment]
        motion.closed_at_fictional_min = state.clock.minutes_since_start
        motion.outcome_note = (
            f"Sì: {tally.headcount_yes} ({tally.yes_millesimi}/1000 millesimi) · "
            f"No: {tally.headcount_no} ({tally.no_millesimi}/1000 millesimi) · "
            f"{'Approvata' if tally.passed else 'Respinta'}"
        )
        # `publish=False`: the trust deltas are an SSE event like any other and
        # must not outrun the save below. They used to fire from inside this
        # call, which made this the one endpoint that still inverted the
        # ordering rule — and the frontend now applies deltas in place rather
        # than refetching, so a failed save would leave the alleanza panel
        # holding numbers the database never recorded.
        trust_deltas = apply_trust_from_votes(state, motion, publish=False)

        audience = sorted(_resident_ids(state))
        body = f"🗳️ Chiusa votazione: \"{motion.title}\" — {motion.outcome_note}"
        # A scoreboard, not the admin talking. Unlike announce/dm/file-motion
        # this endpoint deliberately does not call `schedule_reactions`, and
        # `bookkeeping=True` is what stops the next day's seed loop force-
        # activating all five residents to answer a tally.
        msg = _append_admin_message(state, "main", body, audience, bookkeeping=True)
        # Save first — see the ordering rule at the top of this section. This
        # endpoint mutates the motion itself and the trust matrix, so an
        # inverted order streams a `motion_closed` whose close the DB never
        # recorded.
        await save_run(state)
        bus().publish(run_id, "motion_closed", {"motion": motion.model_dump()})
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
        publish_deltas(state, trust_deltas, "motion")
    return {"ok": True, "motion": motion.model_dump()}


@app.get("/api/runs/{run_id}/events")
@limiter.limit("60/minute")
async def api_run_events(request: Request, run_id: str):
    """SSE stream of live events for a run: typing indicators, new messages, day lifecycle."""
    await _require_run(run_id)
    try:
        queue, replay = bus().subscribe(run_id)
    except TooManySubscribers:
        # Each live stream owns a Queue(maxsize=500) of ~3.2 KB events. The
        # frontend reconnects on its own, so shedding here is cheaper than
        # letting a reconnect storm pin tens of megabytes on an Eco dyno.
        raise HTTPException(
            status_code=429,
            detail="Troppe connessioni aperte su questa partita. Riprova tra poco.",
        )

    async def gen():
        # Initial "connected" so the client knows the stream is open
        yield "event: connected\ndata: {}\n\n"
        # Replay recent events so subscribers that connected mid-burst (e.g. a
        # fresh run mounting just as day 1 starts publishing) don't miss the
        # opening flurry. Frontend dedupes by message id.
        for ev in replay:
            yield ev.to_sse()
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
@limiter.limit("30/minute")
def api_debug_logs(request: Request, n: int = 300):
    # 404 rather than 403 when switched off: with auth optional, this route's
    # very existence is information. `n` needs no clamp — `tail_logs` reads a
    # deque capped at 2000 lines.
    if not DEBUG_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Non disponibile.")
    return {"lines": tail_logs(n)}


@app.post("/api/runs/{run_id}/admin/announce")
@limiter.limit("30/minute")
async def api_admin_announce(request: Request, run_id: str, payload: AnnouncePayload):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    await _require_run(run_id)
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
        audience = sorted(_resident_ids(state))
        msg = _append_admin_message(state, "main", payload.text, audience)
        # Save first — see the ordering rule at the top of this section.
        await save_run(state)
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": None})
        # Kick off reactions if a day is live — force so an admin announcement
        # always gets responses rather than being filtered by probability rolls.
        # Re-read the registry: `save_run` is an await, and DayLoop.run pops
        # itself out of _ACTIVE_LOOPS without holding state_lock, so the loop
        # captured above can be dead by now. Calling schedule_reactions on a
        # dead loop records the obligation nowhere yet still stamps
        # `cascaded=True` — and the loop's post-consolidation save persists
        # that, so tomorrow's seed skips the message and nobody ever answers
        # it. Falling through to the `is None` path leaves it cascaded=False,
        # which is exactly what the seed loop is for.
        loop = active_loop(run_id)
        if loop is not None:
            loop.schedule_reactions(msg, depth=0, force=True)
    return {"ok": True, "message": msg.model_dump()}


@app.post("/api/runs/{run_id}/admin/dm")
@limiter.limit("30/minute")
async def api_admin_dm(request: Request, run_id: str, payload: DMPayload):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")
    await _require_run(run_id)
    async with state_lock(run_id):
        state = await _get_run_for_mutation(run_id)
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
        # Save first — see the ordering rule at the top of this section. This
        # path also creates the DM chat, and an unsaved chat plus a streamed
        # message is exactly the orphan `refetchAndMerge` can never clear.
        await save_run(state)
        bus().publish(run_id, "message_sent", {"message": msg.model_dump(), "chat": chat.model_dump()})
        # Force reaction: a private DM from the admin always deserves a reply
        # from the recipient, not a probability roll. Re-read the registry for
        # the same reason as in `api_admin_announce` — the loop can have ended
        # during the save above, and stamping `cascaded=True` on a dead loop
        # discharges the message forever.
        loop = active_loop(run_id)
        if loop is not None:
            loop.schedule_reactions(msg, depth=0, force=True)
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
