"""HTTP surface hardening, offline.

`test_api.py` covers the happy paths. This file covers the ways the API can
be abused or raced: the advance_day lock, the per-IP rate limits, the length
bounds on admin text, the debug-log gate, and the per-run bus/lock state that
used to be minted for any string a caller put in the URL.

Everything here runs against the real FastAPI app with the in-memory store —
no DATABASE_URL, no network, no spend.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from backend import events, main, scheduler, storage
from backend.main import app, limiter
from backend.models import RunState


class _OrderRecordingBus:
    """Wraps the real bus and notes every `publish` in a shared list, so a
    test can assert `save_run` ran first."""

    def __init__(self, inner, order: list[str]) -> None:
        self._inner = inner
        self._order = order

    def publish(self, *args, **kwargs):
        self._order.append("publish")
        return self._inner.publish(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _watch_save_and_publish(monkeypatch) -> list[str]:
    """Patch `main.save_run` / `main.bus` so their call order is observable.
    Returns the list they append to."""
    order: list[str] = []
    real_save = main.save_run
    real_bus = main.bus()

    async def tracking_save(state):
        order.append("save")
        return await real_save(state)

    monkeypatch.setattr(main, "save_run", tracking_save)
    monkeypatch.setattr(main, "bus", lambda: _OrderRecordingBus(real_bus, order))
    return order


@pytest.fixture
def client():
    """TestClient with the limiter off — same reasoning as test_api.py: every
    test here shares one 'IP', and the tests that care about limits turn them
    back on explicitly via the `limited_client` fixture."""
    was_enabled = limiter.enabled
    limiter.enabled = False
    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        limiter.enabled = was_enabled
        limiter.reset()


@pytest.fixture
def limited_client():
    """TestClient with the rate limiter LIVE. Storage is reset either side so
    one test's hits can't spill into the next."""
    was_enabled = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        limiter.enabled = was_enabled
        limiter.reset()


def _create_run(client) -> dict:
    resp = client.post(
        "/api/runs",
        json={"opening_text": "La caldaia e' ferma da stanotte.", "building_id": "001"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Fix now #1 — the advance_day 409 guard was a TOCTOU
# ---------------------------------------------------------------------------

async def test_two_simultaneous_advance_day_posts_start_exactly_one_day(monkeypatch):
    """The 409 guard has to survive an overlap, not just a repeat.

    `lock.locked()` and `lock.acquire()` used to be separated by the state
    load, which suspends on the asyncpg round trip. Both POSTs passed the
    check; the second blocked in `acquire()` for the whole 60-250s day and
    then ran an unrequested day N+1, past the ⏸ Pausa brake.

    The in-memory store has no yield point, so the race is unreachable
    without forcing one: `load_run` is patched to park the first caller
    exactly where the DB round trip used to. With the fix the lock is already
    held at that moment and the second POST 409s; without it the second POST
    sails through and starts a second day.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            "/api/runs",
            json={"opening_text": "Prova.", "building_id": "001"},
        )
        run_id = created.json()["run_id"]

        parked = asyncio.Event()
        release = asyncio.Event()
        real_load_run = storage.load_run

        async def parking_load_run(rid: str):
            # Only the first caller parks; anyone who gets this far after
            # that proceeds normally, which is what makes the un-fixed code
            # reach `acquire()` with a free lock.
            if not parked.is_set():
                parked.set()
                await release.wait()
            return await real_load_run(rid)

        started: list[str] = []

        async def fake_run_day_bg(rid: str, lock: asyncio.Lock) -> None:
            started.append(rid)
            if lock.locked():
                lock.release()

        monkeypatch.setattr(main, "load_run", parking_load_run)
        monkeypatch.setattr(main, "_run_day_bg", fake_run_day_bg)

        first = asyncio.create_task(ac.post(f"/api/runs/{run_id}/advance_day"))
        await asyncio.wait_for(parked.wait(), timeout=5.0)

        second = await ac.post(f"/api/runs/{run_id}/advance_day")
        assert second.status_code == 409, (
            "the second overlapping POST must be refused, not queued behind the day"
        )

        release.set()
        first_resp = await asyncio.wait_for(first, timeout=5.0)
        assert first_resp.status_code == 200
        assert first_resp.json() == {"ok": True, "status": "running"}

        # Let the (stubbed) background task run.
        for _ in range(5):
            await asyncio.sleep(0)
        assert started == [run_id], "exactly one day may be started"


def test_advance_day_on_an_ended_run_releases_the_lock(client):
    """The `state.ended` early return happens *after* acquire now, so it has
    to hand the lock back — otherwise the run is wedged and every later POST
    gets a false 409."""
    state = _create_run(client)
    run_id = state["run_id"]

    ended = RunState.model_validate_json(storage._MEM_RUNS[run_id])
    ended.ended = True
    ended.ended_reason = "test"
    storage._MEM_RUNS[run_id] = ended.model_dump_json()

    resp = client.post(f"/api/runs/{run_id}/advance_day")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert not events.bus().lock(run_id).locked(), (
        "the early return must release the lock"
    )
    # And the run is not wedged: a second POST gets the same answer, not a 409.
    assert client.post(f"/api/runs/{run_id}/advance_day").json()["ok"] is False


def test_advance_day_on_unknown_run_is_404_and_leaves_no_lock(client):
    """404 before any per-run state is minted — see `_require_run`."""
    assert client.post("/api/runs/run_nope/advance_day").status_code == 404
    assert "run_nope" not in events.bus()._run_locks
    assert "run_nope" not in storage._state_locks


# ---------------------------------------------------------------------------
# Fix now #3 — every mutating endpoint carries an explicit limit
# ---------------------------------------------------------------------------

_MUTATING_ENDPOINTS = [
    "api_create_run",
    "api_advance_day",
    "api_file_motion",
    "api_set_agent_goal",
    "api_close_motion",
    "api_admin_announce",
    "api_admin_dm",
    "api_run_events",
    "api_debug_logs",
]


@pytest.mark.parametrize("view_name", _MUTATING_ENDPOINTS)
def test_every_spending_endpoint_declares_a_limit(view_name):
    """`default_limits` never covered undecorated routes — slowapi only reads
    them from the middleware we deliberately don't install, or from a route
    that already carries a decorator. So the decorator is the whole
    enforcement surface, and a new endpoint added without one is silently
    unlimited."""
    key = f"backend.main.{view_name}"
    assert key in limiter._route_limits, f"{view_name} has no @limiter.limit"
    assert limiter._route_limits[key], f"{view_name} declares an empty limit list"


def test_the_limiter_actually_refuses_a_flood(limited_client):
    """`/api/debug/logs` is where the reviewer measured 70 unlimited 200s.
    Its limit is now 30/minute — and the limiter runs before the endpoint
    body, so the first 30 are the gate's own 404, not a 200."""
    codes = [limited_client.get("/api/debug/logs").status_code for _ in range(40)]
    assert 429 not in codes[:30], f"the first 30 must reach the endpoint: {codes[:30]}"
    assert codes[30:] == [429] * 10, "everything past 30/minute must be refused"


def test_no_default_limits_are_configured():
    """Kept as a tripwire: re-adding `default_limits` would look like it
    protects undecorated routes, and it does not."""
    assert not limiter._default_limits, (
        "default_limits are inert without SlowAPIASGIMiddleware, which breaks SSE"
    )


def test_advance_day_limit_clears_the_auto_advance_chain():
    """The frontend re-POSTs 1.2s after every `day_done`, so a fast day (the
    scripted LLM finishes one in tens of ms) produces ~48 advances/minute. A
    limit below that stops the run dead: 429 is not 409, so `onAdvance` takes
    the fatal branch and there is no manual advance button to restart it."""
    limits = limiter._route_limits["backend.main.api_advance_day"]
    per_minute = [
        lim.limit.amount for lim in limits if lim.limit.GRANULARITY[1] == "minute"
    ]
    assert per_minute, "advance_day must carry a per-minute limit"
    assert min(per_minute) >= 60, (
        "must sit above the ~48/minute the 1.2s auto-advance chain can produce"
    )


@pytest.fixture
def proxied_client():
    """Rate-limiter live, behind the SAME proxy shim production runs under.

    A bare TestClient reports `request.client.host == "testclient"` for every
    request, so it cannot see this bug at all — the whole defect is that
    uvicorn's ProxyHeadersMiddleware *rewrites* `scope["client"]` from a
    caller-supplied header. Wrapping the app in the real middleware with
    `trusted_hosts="*"` reproduces the Procfile exactly.
    """
    was_enabled = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        with TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*")) as c:
            yield c
    finally:
        limiter.enabled = was_enabled
        limiter.reset()


def test_rotating_the_first_x_forwarded_for_entry_does_not_buy_a_fresh_bucket(
    proxied_client, monkeypatch
):
    """The rate-limit key must be the entry OUR proxy appended, not the one
    the caller wrote.

    `--forwarded-allow-ips='*'` makes uvicorn trust X-Forwarded-For[0], and
    Heroku's router *appends* to the chain rather than replacing it — so entry
    0 is whatever the client sent. Keyed off that, every limit here is a
    formality: rotate it and each request lands in its own bucket.
    """
    monkeypatch.setattr(main, "TRUST_PROXY_HEADERS", True)
    codes = [
        proxied_client.get(
            "/api/debug/logs",
            headers={"X-Forwarded-For": f"203.0.113.{i}, 10.0.0.7"},
        ).status_code
        for i in range(40)
    ]
    assert codes[30:] == [429] * 10, (
        f"spoofed leading XFF entries must share one bucket, got {codes}"
    )


def test_a_genuinely_different_proxy_appended_ip_gets_its_own_bucket(
    proxied_client, monkeypatch
):
    """The other half: the fix must not collapse real callers together, which
    is the failure `--proxy-headers` was added to cure in the first place."""
    monkeypatch.setattr(main, "TRUST_PROXY_HEADERS", True)
    codes = [
        proxied_client.get(
            "/api/debug/logs", headers={"X-Forwarded-For": f"203.0.113.9, 10.0.0.{i}"}
        ).status_code
        for i in range(40)
    ]
    assert 429 not in codes, f"distinct clients must not share a bucket, got {codes}"


def test_x_forwarded_for_is_ignored_unless_a_proxy_is_declared(
    limited_client, monkeypatch
):
    """Default OFF is the fail-safe direction. A directly-exposed server runs
    without `--proxy-headers` too, so nothing has vetted the header — and a
    caller who invents one must get nothing for it. Deliberately NOT the
    proxied fixture: that one installs the always-trust middleware, which is
    the very thing `TRUST_PROXY_HEADERS` is declaring the presence of."""
    monkeypatch.setattr(main, "TRUST_PROXY_HEADERS", False)
    codes = [
        limited_client.get(
            "/api/debug/logs", headers={"X-Forwarded-For": f"198.51.100.{i}"}
        ).status_code
        for i in range(40)
    ]
    assert codes[30:] == [429] * 10, (
        f"the header must buy nothing when no proxy is declared, got {codes}"
    )


# ---------------------------------------------------------------------------
# Fix now #9 — save before publish
# ---------------------------------------------------------------------------

def test_announce_is_persisted_before_it_is_streamed(client, monkeypatch):
    """A subscriber must never hold a message the database has not seen:
    `bus().publish` is `put_nowait`, so an inverted order streams first and
    can then lose the save, leaving an orphan the frontend never removes."""
    state = _create_run(client)
    order = _watch_save_and_publish(monkeypatch)

    resp = client.post(
        f"/api/runs/{state['run_id']}/admin/announce",
        json={"text": "Il tecnico arriva domani."},
    )
    assert resp.status_code == 200, resp.text
    assert order and order[0] == "save", f"save must come first, got {order}"


@pytest.mark.parametrize("endpoint", ["dm", "motion", "goal", "close"])
def test_the_other_admin_endpoints_persist_before_streaming(client, monkeypatch, endpoint):
    """Same rule for the remaining four. `close` publishes twice and mutates
    the trust matrix as well, so an inverted order streams a `motion_closed`
    the database never recorded."""
    state = _create_run(client)
    run_id = state["run_id"]

    motion_id = None
    if endpoint == "close":
        filed = client.post(
            f"/api/runs/{run_id}/motions",
            json={"title": "Secondo preventivo", "description": "Un altro tecnico."},
        )
        motion_id = filed.json()["motion"]["id"]

    order = _watch_save_and_publish(monkeypatch)

    if endpoint == "dm":
        resp = client.post(
            f"/api/runs/{run_id}/admin/dm",
            json={"recipient_id": "conti", "text": "Una parola, signora."},
        )
    elif endpoint == "motion":
        resp = client.post(
            f"/api/runs/{run_id}/motions",
            json={"title": "Preventivo", "description": "Chiediamo un tecnico."},
        )
    elif endpoint == "goal":
        resp = client.put(
            f"/api/runs/{run_id}/agents/conti/goal", json={"goal": "Vuoi vendere."}
        )
    else:
        resp = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")

    assert resp.status_code == 200, resp.text
    assert order and order[0] == "save", f"save must come first, got {order}"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/runs/{rid}/admin/announce", {"text": "Chi ha chiamato il tecnico?"}),
        ("/api/runs/{rid}/admin/dm", {"recipient_id": "conti", "text": "Una parola."}),
        ("/api/runs/{rid}/motions", {"title": "Preventivo", "description": "Un tecnico."}),
    ],
)
def test_a_day_that_ends_during_the_save_leaves_the_message_for_tomorrow(
    client, monkeypatch, path, body
):
    """The save that Fix #9 moved up is an await, and it sits between the
    `active_loop()` lookup and its use.

    `DayLoop.run` pops itself out of `_ACTIVE_LOOPS` without holding
    `state_lock`, so the loop captured before the save can be dead by the time
    we call `schedule_reactions` on it. That records the obligation on a loop
    nobody will ever run again — while still stamping `cascaded=True`, which
    the loop's own post-consolidation save then persists. The next day's seed
    tests `not m.cascaded`, so the admin message is discharged forever without
    a single resident having seen it. Re-reading the registry after the save
    sends it down the `loop is None` path instead, which is exactly what the
    seed loop is for.
    """
    state = _create_run(client)
    run_id = state["run_id"]

    live = RunState.model_validate_json(storage._MEM_RUNS[run_id])
    loop = scheduler.setup_day(live)
    assert loop is not None
    assert scheduler.active_loop(run_id) is loop

    real_save = main.save_run

    async def save_then_end_the_day(st):
        # Park exactly where DayLoop.run's own pop lands: after the endpoint
        # captured the loop, before it uses it.
        await asyncio.sleep(0)
        scheduler._ACTIVE_LOOPS.pop(run_id, None)
        return await real_save(st)

    monkeypatch.setattr(main, "save_run", save_then_end_the_day)
    try:
        resp = client.post(path.format(rid=run_id), json=body)
        assert resp.status_code == 200, resp.text
        msg = loop.state.messages[-1]
        assert msg.sender_kind == "admin"
        assert msg.cascaded is False, (
            "a dead loop cannot service the obligation, so the message must "
            "stay in the pool for tomorrow's seed"
        )
        assert not loop.pending_admin_reactions, (
            "and nothing may be recorded on a loop that will never run again"
        )
    finally:
        scheduler._ACTIVE_LOOPS.pop(run_id, None)


def test_close_motion_streams_trust_deltas_only_after_the_save(client, monkeypatch):
    """`trust_updated` used to escape the rule by firing from *inside*
    `apply_trust_from_votes`, thirteen lines before the save.

    It matters more than the other publishes on this endpoint: the frontend no
    longer refetches on `trust_updated`, it applies the deltas in place, so a
    failed save leaves the alleanza panel holding numbers the database never
    recorded until the next `day_done`.
    """
    state = _create_run(client)
    run_id = state["run_id"]
    filed = client.post(
        f"/api/runs/{run_id}/motions",
        json={"title": "Preventivo", "description": "Chiediamo un tecnico."},
    )
    motion_id = filed.json()["motion"]["id"]

    # Two aligned resident votes so `apply_trust_from_votes` has something to
    # publish — with no deltas the event never fires and the test proves
    # nothing.
    stored = RunState.model_validate_json(storage._MEM_RUNS[run_id])
    motion = next(m for m in stored.motions if m.id == motion_id)
    motion.votes = {"conti": "yes", "greco": "yes"}
    storage._MEM_RUNS[run_id] = stored.model_dump_json()

    order: list[str] = []
    real_save = main.save_run
    real_bus = main.bus()

    async def tracking_save(st):
        order.append("save")
        return await real_save(st)

    class _EventNamingBus:
        def publish(self, run, event, payload):
            order.append(event)
            return real_bus.publish(run, event, payload)

        def __getattr__(self, name):
            return getattr(real_bus, name)

    monkeypatch.setattr(main, "save_run", tracking_save)
    monkeypatch.setattr(main, "bus", lambda: _EventNamingBus())
    monkeypatch.setattr("backend.dials.bus", lambda: _EventNamingBus())

    resp = client.post(f"/api/runs/{run_id}/motions/{motion_id}/close")
    assert resp.status_code == 200, resp.text
    assert "trust_updated" in order, "the aligned votes must produce deltas"
    assert order.index("save") < order.index("trust_updated"), (
        f"trust deltas must not outrun the save, got {order}"
    )


# ---------------------------------------------------------------------------
# Fix now #10 — length bounds on admin text
# ---------------------------------------------------------------------------

def test_oversize_announce_is_422(client):
    """4001 chars. Admin text is re-inlined into the prompt for the rest of
    the run, so an unbounded field is a spend multiplier, not a cosmetic
    problem — it has to be refused at the door."""
    state = _create_run(client)
    resp = client.post(
        f"/api/runs/{state['run_id']}/admin/announce", json={"text": "a" * 4001}
    )
    assert resp.status_code == 422
    # And the boundary value still works.
    ok = client.post(
        f"/api/runs/{state['run_id']}/admin/announce", json={"text": "b" * 4000}
    )
    assert ok.status_code == 200, ok.text


def test_oversize_goal_is_422(client):
    """`admin_goal` is the worst offender: agent.py appends it to BOTH the
    system prompt and the notification prompt, so it ships twice per call for
    every remaining activation."""
    state = _create_run(client)
    run_id = state["run_id"]
    resp = client.put(
        f"/api/runs/{run_id}/agents/conti/goal", json={"goal": "x" * 2001}
    )
    assert resp.status_code == 422
    ok = client.put(f"/api/runs/{run_id}/agents/conti/goal", json={"goal": "x" * 2000})
    assert ok.status_code == 200, ok.text


def test_oversize_dm_motion_and_opening_are_422(client):
    state = _create_run(client)
    run_id = state["run_id"]

    dm = client.post(
        f"/api/runs/{run_id}/admin/dm",
        json={"recipient_id": "conti", "text": "y" * 4001},
    )
    assert dm.status_code == 422

    motion = client.post(
        f"/api/runs/{run_id}/motions",
        json={"title": "t" * 201, "description": "ok"},
    )
    assert motion.status_code == 422

    opening = client.post(
        "/api/runs", json={"opening_text": "z" * 8001, "building_id": "001"}
    )
    assert opening.status_code == 422


# ---------------------------------------------------------------------------
# Hygiene — /api/debug/logs is gated
# ---------------------------------------------------------------------------

def test_debug_logs_is_off_by_default(client):
    """It hands out live run ids and raw resident chat text, and in open-beta
    mode every /api/* is public."""
    assert main.DEBUG_ENDPOINTS is False
    assert client.get("/api/debug/logs").status_code == 404


def test_debug_logs_opens_when_the_env_flag_is_set(client, monkeypatch):
    monkeypatch.setattr(main, "DEBUG_ENDPOINTS", True)
    resp = client.get("/api/debug/logs")
    assert resp.status_code == 200
    assert "lines" in resp.json()


# ---------------------------------------------------------------------------
# Consider — bus/lock dicts keyed by unvalidated run ids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/runs/{rid}/admin/announce", {"text": "ciao"}),
        ("post", "/api/runs/{rid}/admin/dm", {"recipient_id": "conti", "text": "ciao"}),
        ("post", "/api/runs/{rid}/motions", {"title": "t", "description": "d"}),
        ("post", "/api/runs/{rid}/motions/m_1/close", None),
        ("put", "/api/runs/{rid}/agents/conti/goal", {"goal": "g"}),
        ("post", "/api/runs/{rid}/advance_day", None),
        ("get", "/api/runs/{rid}/events", None),
    ],
)
def test_unknown_run_id_mints_no_per_run_state(client, method, path, body):
    """Every one of these dicts is a `defaultdict`, so a single touch with a
    junk id leaves a permanent entry. The 404 has to come first."""
    rid = "run_definitelynotreal"
    url = path.format(rid=rid)
    resp = getattr(client, method)(url, **({"json": body} if body is not None else {}))
    assert resp.status_code == 404, f"{method.upper()} {url} -> {resp.status_code}"

    bus = events.bus()
    assert rid not in bus._run_locks
    assert rid not in bus._subscribers
    assert rid not in bus._recent
    assert rid not in storage._state_locks
    assert rid not in storage._save_locks


def test_sse_subscribers_are_capped_per_run():
    """Each stream owns a Queue(maxsize=500); an uncapped reconnect storm can
    pin tens of megabytes on a 512 MB dyno."""
    bus = events.RunEventBus()
    queues = [bus.subscribe("run_x")[0] for _ in range(events.MAX_SUBSCRIBERS_PER_RUN)]
    assert len(queues) == events.MAX_SUBSCRIBERS_PER_RUN

    with pytest.raises(events.TooManySubscribers):
        bus.subscribe("run_x")

    # Dropping one stream makes room again — the cap is on concurrency, not
    # on lifetime connections.
    bus.unsubscribe("run_x", queues[0])
    q, _ = bus.subscribe("run_x")
    assert q is not None


def test_subscriber_cap_check_does_not_mint_the_run_entry():
    bus = events.RunEventBus()
    assert "run_never_seen" not in bus._subscribers
    # The length check itself must not create the set.
    assert len(bus._subscribers.get("run_never_seen", ())) == 0
    assert "run_never_seen" not in bus._subscribers
