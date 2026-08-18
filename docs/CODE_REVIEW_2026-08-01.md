# CondoSim — consolidated review

**Date:** 2026-08-01
**Commit reviewed:** `676bf2f` (branch `redesign/v2-smooth`)
**Status:** findings only — **no code was changed by this review**

**Method.** Six reviewers, one per subsystem (agent loop, tools, scheduler,
persistence, API/security, frontend), each reading the actual files rather
than grepping. Every finding was then handed to an independent adversarial
verifier instructed to *refute* it — re-open the cited line, default to
REJECTED when uncertain, and kill anything that was a lint nit, an
unreachable failure, or a fix that would break a documented invariant.
29 of 33 findings survived; several had their severity or proposed fix
corrected by the verifier. Line numbers are as of the commit above.

Findings were checked against real artefacts where possible — the 24 saved
runs in `data/runs/`, replays of the trust and mention predicates over them,
and offline reproductions against the real `ToolContext` and `DayLoop`.
Claims below that say "reproduced" or "measured" were executed, not reasoned.

29 verified findings → **22 items** after merge. Three findings (tools.py:1186, tools.py:839, scheduler.py:417) were the same defect seen from three angles and are now one item.

---

## Root causes — read this first

**RC1 — `Message` has no way to say "this is bookkeeping, not speech."**
`_close_motion_if_ready` (tools.py:839) and `api_close_motion` (main.py:512) both author vote tallies as `sender_kind="admin"`. Every consumer downstream reads them as the administrator talking:

- tools.py:1186 credits the tally to whichever resident's `vote` call tripped it → false ack + premature implicit-done (**Fix now #2**)
- agent.py:342-349 quotes the tally to residents as *"Cosa ha detto, parole sue"* and demands a verbal reply
- scheduler.py:491-496 force-activates all five residents the next morning to answer a scoreboard (**Fix now #8**)
- scheduler.py:433-439 stamps it `cascaded` only *because* of the bug in the first bullet

**These fixes must ship together.** Guarding the tracker append at tools.py:1186 without touching `cascaded` trades the false-ack bug for the next-morning mass-force, because scheduler.py:439 is currently the only thing suppressing it. One additive `bookkeeping: bool = False` field on `Message` closes all four (**Worth doing A**). Do not widen `SenderKind` — App.jsx branches on it in nine places (627, 849, 1299, 1893, 1929).

**RC2 — three different definitions of "the agent produced output."**
`agent.py:630/664` counts sends + reactions. `scheduler.py:417/546` counts sends only, author-blind. `scheduler.py:310` marks the obligation discharged at *schedule* time, before anyone activates. Four findings fall out of this split (Fix now #2, #5; Worth doing E). Pick one predicate and route all three call sites through it.

**RC3 — unanchored substring matching over Italian prose.**
`_content_rule_violation` (tools.py:98), `on_message_attack` (dials.py:195), `_was_mentioned_recently` (scheduler.py:160), `tool_vote`'s title fallback (tools.py:850) and `_resolve_chat` (tools.py:379) all do bare `in`/first-match resolution. Measured false-positive rates: 33% of attack deltas, 65 of 245 mention hits, and a documented collision between the phone-fiction blocklist and `data/world_events.json`.

---

## Fix now

### 1. `advance_day` 409 guard is a TOCTOU — a second POST queues an unrequested extra day
`backend/main.py:392-403`. `lock.locked()` (393) and `await lock.acquire()` (401) are separated by `await _get_run(run_id)`, which suspends on the asyncpg round trip. Two near-simultaneous POSTs both pass the check; the second blocks in `acquire()` for the full 60-250s day, then starts day N+1 — bypassing the ⏸ Pausa brake. Reproduced: `asyncio.gather` of two calls ran `run_day` twice, no 409.
**Fix:** move `await lock.acquire()` immediately after the `locked()` check (the free-lock fast path does not suspend), then load state inside try/except that releases on the `state.ended` early return and on any exception before `create_task`. Regression test must patch `load_run` to `await asyncio.sleep(0)` — the in-memory path has no yield point.
**Effort:** trivial.

### 2. Motion auto-close credits an admin-authored line to the voting resident *(merged ×3)*
`backend/tools.py:1186` (`ctx.sent_messages_this_activation.append(msg)`, no sender filter) fed by `tools.py:839` (`_create_and_append_message(ctx, main_chat, "admin", body)` — the only call site passing a sender other than `ctx.agent_id`). Consumed by `agent.py:664-671` (implicit done) and `scheduler.py:417` / `:546` (`acknowledged = bool(...)`).
Reproduced with the real `ToolContext`: greco casts the threshold-crossing vote, ends with `sent_tracker=[('admin', ...)]`, authors zero words, and is discarded from `pending_admin_reactions` at scheduler.py:428 with **no WARNING** — the exact silent-ignore the acknowledgment guarantee exists to prevent. Also cuts an ordinary voter's turn short so they cannot comment on the outcome they just triggered.
**Fix:** `if sender_id == ctx.agent_id:` guard on the append at tools.py:1186, **plus** author-aware ack at scheduler.py:417 and :546 (`any(m.sender_id == aid for m in ...)`), **plus** the RC1 pairing (set `cascaded=True` on the tally at tools.py:832-839 in the same commit, or land Worth-doing A). Test: forced agent casts the decisive vote, sends nothing, must stay in `pending_admin_reactions`.
**Effort:** small. `_close_motion_if_ready` has demonstrably never fired in any of the 24 saved runs — untested code.

### 3. `default_limits` is inert; every mutating endpoint is unrate-limited
`backend/main.py:66`. `Limiter(..., default_limits=["60/minute"])` with no `app.add_middleware` anywhere in the repo. In slowapi 0.1.9 the defaults are only applied via `middleware.py:47`; the decorator path computes `all_limits = []` and never appends `_default_limits`. Empirically: 70 back-to-back `GET /api/debug/logs` all returned 200; decorated `/api/login` correctly 429s. So announce, dm, motions, motions/close, goal, SSE and debug/logs are unlimited — and main.py:93-94 calls per-IP limits "the financial safety net" for open-beta mode.
**Fix:** explicit `@limiter.limit(...)` on each mutating endpoint. **Do not install `SlowAPIASGIMiddleware`** — in 0.1.9 `send_wrapper` re-emits `http.response.start` per body chunk and would break the SSE `StreamingResponse` at main.py:545. Drop or comment the misleading `default_limits`. Separately add `--proxy-headers --forwarded-allow-ips='*'` to the Procfile: uvicorn defaults `forwarded_allow_ips` to 127.0.0.1, so behind the Heroku router every client collapses onto one rate-limit key.
**Effort:** small.

### 4. `_resolve_chat` ignores `current_agent_id`; same-named DMs shadow each other
`backend/tools.py:379-402` — the parameter is dead, never referenced in the body. All DM chats are named `f"DM con {other_name}"` (tools.py:674, 960), so resolution returns the first insertion-order match and the caller then fails `_is_member` and refuses. Reproduced on `data/runs/run_6cbd6b87.json`: three chats named `DM con Amministratore`; `_resolve_chat(state, 'DM con Amministratore', 'greco')` returns romano's. **9 of 24 saved runs have at least one duplicated DM display name.** The model only ever sees display names (agent.py:725, :465), and `send_message`'s own schema (tools.py:173) invites it to address "una chat privata già aperta" by name — via the broken path. Each refusal burns one of three `MAX_TOOL_CALLS_PER_ACTIVATION` steps at ~10.4k system-prompt chars each. `send_dm` is unaffected (it matches by `frozenset(member_ids)`).
**Fix:** collect all matches per stage, prefer one where `current_agent_id in c.member_ids`. Optionally make DM display names unique per pair. Test: two same-named DMs, each member resolves to their own.
**Effort:** small.

### 5. A 👍 satisfies implicit-done but not the scheduler's ack → forced agent loops all day
`backend/agent.py:630-633` and `:664-667` count `sent + reactions`; `scheduler.py:417/546` count sends only. tools.py:1053 blocks reacting to the *admin's* message under force but permits reacting to a neighbour's — exactly what the options menu at agent.py:226 offers. Reproduced: forced ctx, react-only → `implicit_done_would_fire=True, scheduler_acknowledged=False`. Turn cut after one call with 2 of 3 steps unused, re-forced every remaining round plus both bonus rounds (~6 activations, ≈$0.0035/agent/admin-message) before the WARNING at scheduler.py:566-573 drops it.
**Fix:** in agent.py compute `landed = len(sent) if forced_for_admin else len(sent) + len(reactions)`, used for `produced_before`, `produced_after` and the `produced_so_far` nudge gate at agent.py:599-602 — a forced agent that only reacts then keeps its remaining steps and can still write words in the same activation.
**Effort:** trivial. **⚠ Touches a documented invariant** — see callout.

### 6. Phone-fiction blocklist kills activations about broken lifts and door phones
`backend/tools.py:77-78`. `"fa le bizze"`, `"fanno le bizze"`, `"problemi tecnici"`, `"problemi al telefono"` are subject-free substrings; `_content_rule_violation` (tools.py:94-103) tests them against the whole outgoing text and the four call sites set `ctx.done = True`. This collides head-on with the ambient layer: `data/world_events.json:13` is *"Il citofono fa i capricci"*, plus `ascensore_fermo`, `corrente_saltata`, `riscaldamento_tarda`, `wifi_condominio` — injected verbatim into every resident's prompt by agent.py:125-131. Reproduced against the real dispatcher: `"il citofono fa le bizze da settimane, qualcuno ha chiamato il tecnico?"` → refused, activation discarded, zero output, with a nonsensical in-fiction explanation.
**Fix:** replace those four entries with regexes anchored on chat/phone nouns, e.g. `r"(telefono|cellulare|chat|cronologia|messaggi|app)\b[^.!?]{0,30}\b(fa|fanno) le bizze"`. Leave the rest of the list and all of `_BLOCKED_PHRASES_MEETING` as substrings. Test both polarities.
**Effort:** small. Narrows the filter; does not remove the containment layer.

### 7. Bonus drain can activate an agent behind the head of the transcript
`backend/scheduler.py:532-533`: `base = max(latest_minute+5, day_end-60)` then `target = min(base, day_end-1)` — the cap is applied *after* the floor, so when `latest_minute > day_end-6` the target moves backwards. The planned-round path applies its floor last (352-354) and adds the owed-message clamp (362-372); both are absent here. Every context reader filters `<= now` (agent.py:707, 410, 347, 376), so the agent activates blind. Reproduced with the real DayLoop: romano and conti both activated at 1379 against a head of 1382/1383 — two agents answering the same admin question from stale snapshots, the exact condition round-robin exists to remove. Narrow reachability (needs the transcript within 6 min of `DAY_END_HOUR`); zero test coverage for the drain.
**Fix:** `target = max(min(max(latest+5, day_end-60), day_end-1), latest)` — keep the cap, stop it winning over the floor. Port the owed-message clamp into the bonus body. Regression test: assert `fictional_minutes_now >= timeline.latest_minute(state)` at every `activate_agent` across a day including the drain.
**Effort:** trivial. **⚠ Touches the causality clamp.**

### 8. Closing a motion from the UI force-activates all five residents the next morning
`backend/main.py:505-515`. Unlike announce (573), dm (610) and file-motion (442), `api_close_motion` never calls `loop.schedule_reactions`, and `_append_admin_message` sets `cascaded=False` (main.py:239). The tally survives to the next `DayLoop.run()`, where scheduler.py:491-496 seeds all five residents into `pending_admin_reactions` → `prob = 1.0` at scheduler.py:373-376, bypassing `participation_probability`, the quiet-morning gate (251-256), the saturation damper and the soft budget. Reachable from App.jsx:1500 "Chiudi votazione". They are then told at agent.py:161-171 that *"Una reazione emoji non basta … deve vedere parole tue"* — about a scoreboard.
**Fix (stopgap):** set `msg.cascaded = True` on the message returned at main.py:512. Real fix is Worth-doing A.
**Effort:** trivial.

### 9. Five admin endpoints publish SSE before saving — inverse of the day_end rule
`backend/main.py:444` (file motion), `476` (agent goal), `515` (close), `575` (announce), `612` (DM). `scheduler.py:582-591` does the opposite with an explicit comment. `bus().publish` is `put_nowait`, and `await save_run` is the next yield point. Real damage is confined to admin actions taken *between* days (no live loop): the mutated state is a local `load_run` copy discarded on exception, while the motion, the new DM chat and the admin message have already been streamed and sit in the 60s replay buffer. `refetchAndMerge`'s `{...fresh}` (App.jsx:1869) then wipes `motions`/`chats` while the orphaned message stays merged in permanently — messages are only ever merged, never removed.
**Fix:** move `await save_run(state)` above the `publish` calls and above `loop.schedule_reactions(...)` in all five. All already hold `state_lock(run_id)` across the block. Persisting `cascaded=True` before the audience has reacted is the unsafe direction; persisting `False` costs only a redundant nudge.
**Effort:** trivial.

### 10. No length bound on any admin text field; `admin_goal` is re-inlined every activation
`backend/main.py:163-164` (`goal: str`, unconstrained), same for `AnnouncePayload.text` (144), `DMPayload.text` (148), `MotionPayload` (153), `CreateRunPayload.opening_text` (292). Zero `max_length`/`constr` anywhere in backend. `agent.py:58` and `agent.py:184` both append `admin_goal` untruncated and both prompts ship in the same request — twice per call, for the rest of the run. A 100 KB goal ≈ 50k input tokens/activation ≈ $0.007 vs the ~$0.000064 baseline (config.py:19): **~100× multiplier**, `RUN_COST_CAP_USD` trips in ~100 activations with `cost_cap_exceeded` and no hint of the cause. `build_context_digest` (agent.py:732) also inlines bodies untruncated, while agent.py:157-158 does clamp a preview to 320 chars.
**Fix:** `Field(max_length=...)` on the request models (goal 2000, text 4000, opening_text 8000, title 200) so oversize is a 422, plus a defensive clamp at both prompt sinks and in the digest. API test for a 4001-char announce.
**Effort:** small. **⚠ Touches the "prompt size is bounded" invariant** — in the direction of enforcing it.

### 11. Consolidation prompt is the only one exempt from memory windowing
`backend/memory.py:304`: `memory_so_far = await read_memory(state, aid)`, interpolated raw at :255. `agent.py:37-39` wraps the identical read in `window_memory(..., MEMORY_DAYS_IN_PROMPT)`. Measured on disk: `run_5cd1a537/memory/greco.md` is 15,060 bytes over 16 entries, ~800 chars/agent/day of growth. Cost is negligible (~$0.008/16-day run at $0.14/M) — the real issue is that `window_memory`'s own docstring (memory.py:110-113) warns the model "drowns in undifferentiated history," and the one call that decides what a resident remembers sees 16 days while the resident it writes for sees 6.
**Fix:** `window_memory(await read_memory(state, aid), MEMORY_DAYS_IN_PROMPT)`, importing the constant alongside line 20. Ignore memoisation — local DB reads are noise against LLM latency.
**Effort:** trivial.

### 12. `trust_updated` refetches the whole run to copy 25 floats
`frontend/src/App.jsx:2111-2116` — handler takes no event argument and does `api.getRun(runId)`, discarding everything but `state.trust`. `data/runs/run_0c355245.json` is 153,816 bytes; replaying the dials predicates over it gives ~5-7 `trust_updated` per fictional day → **~0.75-1 MB/day per connected browser**, each a full Supabase jsonb read and pydantic serialize on the same Eco dyno running the day loop. `motion_closed` (2072-2091) piggybacks another one, so a single close costs two 150KB refetches.
**Fix:** apply `JSON.parse(e.data).data.deltas` in place (entries are `{from,to,delta,cause}`, dials.py:97), clamping to ±1 to mirror dials.py:60; the `day_done` refetch (2020) re-syncs authoritatively. Drop the refetch in `motion_closed`.
**Effort:** trivial.

### Trivial hygiene — batch into one commit

| Item | Location | Fix |
|---|---|---|
| Mention boost fires on `continua`/`conteggio` (measured 65 false positives in 245 hits; 3.9% of Conti's turn-slots) | `scheduler.py:147-162` | Match case-**sensitively** on the capitalized surname with `\b…\b` against `m.content` — removes all 65 while keeping all 180 real hits. Precompile per agent. Leave `_looks_like_admin_ping` alone (its token list contains a bare `?`). |
| Mojibake in 7 agent-facing refusal strings (`che Ã¨`, `giÃ¹`, `â€"`) — verified at byte level | `tools.py:760,761,765,995,996,1000,1064` | Repair to UTF-8, drop the stray U+00A0 on 1064. The block is duplicated 4× and two copies drifted — extract `_refusal_text(category, phrase, verb)`. Add a test asserting no U+00C3/U+00E2/U+FFFD under `backend/`. |
| `/api/debug/logs` unauthenticated in open-beta mode, leaks 32-bit run ids and 80 chars of raw chat text per tool call | `main.py:556-558`, `agent.py:643-644` | Gate behind `DEBUG_ENDPOINTS=1` (default off) or require auth regardless of mode. Log tool name + `len(text)`, never the text. No `n` clamp needed (deque maxlen=2000). |
| `consolidate_day` discards the `gather(return_exceptions=True)` list; `read_memory`/`_append_memory` sit outside `_consolidate_one`'s try | `memory.py:304, 343, 354-357` | Bind the result and `log_error` per agent; wrap line 343. A DB blip *after* the paid call loses a diary day with zero log output. Skip the `initialize_run_memory` transaction suggestion — that path 500s run creation and is unreachable. |
| `fade-in` class on `.msg-row`, only stylesheet rule is `.msg.fade-in` — the `msgIn` keyframe is dead | `App.jsx:1317`, `App.css:461,497` | Move the class to the bubble at App.jsx:1330. Also strip `isNew` in `mergeMessagesChronologically` (61-68) — the spread at line 65 cannot delete it, so once the selector matches, every chat switch replays the animation on all messages at once. |
| SSE reconnect timer id discarded, no backoff, no cap — 1 Hz forever against a 503/401 | `App.jsx:2122-2130`, cleanup at 2147 | Store the id, `clearTimeout` in cleanup, exponential backoff capped ~30s. The backoff is the part that pays; the leak path is narrow. |

---

## Worth doing

### A. Add `bookkeeping: bool = False` to `Message` (subsumes Fix-now #2 and #8)
Set it at `tools.py:839` and `main.py:512`; skip such messages in `_latest_unanswered_admin_in_main` (agent.py:342-349), the forced-agent causality clamp (scheduler.py:364-368) and the day-start seeding filter (scheduler.py:492). This is the root fix for RC1 and retires two stopgaps. Additive field, no migration (state is jsonb). Do **not** add a `SenderKind` literal.
**Effort:** small-medium.

### B. Motion subsystem: one tally, one resolver, first tests
- `_close_motion_if_ready` (tools.py:806-818) tallies by headcount (`total//2+1`); `api_close_motion` (main.py:489-501) applies quorum + 500/1000 millesimi. Divergence is confined to the `cast >= total` branch — e.g. greco+ferrari yes (470 millesimi), conti no, two abstain → tools says *passed*, main says *failed*. Auto-close wins by construction (main.py:487-488 short-circuits on `status != "open"`). Trust deltas are unaffected: `apply_trust_from_votes` reads only `motion.votes`.
- `tool_vote`'s title fallback (tools.py:850) does not filter `status == "open"` and returns the **oldest** insertion-order match. In `run_0c355245`, `"amministratore"` matches both open motions and lands a vote on *Mozione di sfiducia* instead of *Nomina nuovo amministratore* — the politically opposite act — then reports success naming the wrong one. ~4% of saved runs have two motions open at once.
**Fix:** extract a shared millesimi+quorum tally called from both paths; in `tool_vote`, try exact id first, then substring over `reversed([open motions])`, then closed motions only to produce the "già chiusa" message, returning both codes on ambiguity. Motions and votes currently have **zero** tally coverage; pin the 470-millesimi case.
**Effort:** medium.

### C. Attack-by-name detector is 33% wrong-signed
`backend/dials.py:174-205`. The comment at :194 says "word-ish boundaries"; :195 is a bare `any(cand in low for cand in candidates)`. Candidates for `Sig.ra Conti` reduce to `['sig.ra conti', 'conti']`, so `continua`/`continuare` match. And no relationship is required between the aggression term and the name, so alliance-building is scored as attack. Replayed over all 24 runs: **70 attack deltas fire, 23 provably wrong-signed** — `"concordo con ferrari. basta perdite di tempo."` penalises romano toward Ferrari; `"chi continua a voler pagare… basta perdere tempo"` penalises Sig.ra Conti, who is not mentioned. Note `grep trust backend/scheduler.py` returns nothing — contrary to CLAUDE.md's "feeds scheduler dampers," trust has **no behavioural feedback today**, so this corrupts an observation surface (App.jsx:571, :763), not the simulation.
**Fix:** `\b`-anchor the name; split on `[.!?;]` and require term and name in the same clause; suppress when the name is preceded within ~30 chars by `concordo con` / `d'accordo con` / `ha ragione` / `come dice` / `sono con`. `grep -rn 'dials' tests/` returns nothing — add `tests/test_dials.py` pinning both cases above to zero.
**Effort:** medium.

### D. Frontend render queue has no identity or flush discipline
Two bugs, one root — the pacing queue is a bag of timers with no id set and no flush primitive.
- **Admin sends bypass the queue** (`App.jsx:1928-1933`): the admin branch renders immediately, touching neither `renderTimersRef` (1667/1982) nor `nextRenderSlotRef`. Since `timeline.allocate_minute` guarantees the admin message sorts *after* everything already in state, the queued residents land **above** the admin bubble for up to `MAX_QUEUE_LAG_MS` = 7s. Nothing already rendered ever swaps — this is a render-layer cousin of the v1 reshuffle, not a reintroduction of it — but "puoi scrivere quando vuoi" (2263) makes the precondition a first-class use case.
- **Replay re-queues rendered messages** (`App.jsx:1939`): `onVisibility` calls `connect()` unconditionally (2138-2142), which resubscribes and re-receives up to 60s / 200 events. The only dedupe is inside `renderIncomingMessage`'s setState (1885), reached up to 7s later — `nextRenderSlotRef`, `lastRenderedFicMinRef` and `setQueuedMessages` are all mutated first. On replay every `ficDelta` is 0 so each message adds 1400ms, saturating at +7000ms: phantom "N messaggi in arrivo…" for messages already on screen, and the next genuinely new message held the full 7s. Also fires on any refresh or transient drop.
**Fix:** make `renderTimersRef` a `Map` of id → payload; add `seenMessageIdsRef` and early-return in the handler before line 1939; in the admin branch, flush all pending payloads in queue order, reset `nextRenderSlotRef`, then render the admin message. Reset the seen-set in the existing run_id effect (1678-1682).
**Effort:** small (both).

### E. `cascaded` is set at schedule time, so an exhausted obligation can never be retried
`backend/scheduler.py:310` sets `trigger.cascaded = True` before any agent activates. The give-up branch (566-573) logs a WARNING and clears `pending_admin_reactions` without touching `cascaded`, and the only retry mechanism — the next-day seed at 491-496 — tests `not m.cascaded`, permanently false. The day-start seed loop has the same shape. Reproduced with the real scheduler: one resident scripted to narrate instead of calling a tool → after day 1 the opening admin message is `cascaded=True`, pending is empty, day 2 seeds `[]`, and that resident authored **0 messages for the entire run**. Same silent path at the exception-branch `discard(aid)` on scheduler.py:541.
**Fix:** flip `cascaded` on discharge, not on schedule. Minimal: in the give-up branch, before `clear()`, set `cascaded = False` on any non-resident message whose audience intersects the pending set. Bound it with a `retry_count` or day-age cutoff so a permanently mute model can't force-activate forever. (The module comment at 586-589 is *not* contradicted — it describes the `loop is None` path, which is correct.)
**Effort:** small, but needs a deliberate retry-bound decision.

---

## Consider

### Building name and city are hardcoded in the system prompt
`agent.py:42` (`"…del Condominio Via Garibaldi, a Milano."`) and `agent.py:69` (group name), plus the five surnames and the group name baked into the tool schema descriptions (`tools.py:162, 199, 283`). The real name is data (`building.py:42-45`, `:127-131`). This directly contradicts CLAUDE.md's "no code changes required" authoring claim. Verified consequence: rename the main chat and `_resolve_chat(state, "Condominio Via Garibaldi")` returns `None` — the agent is told the chat isn't on their phone and burns a step. Latent today: 001 is the only building and its name matches the literal.
**Do the cheap half now** (2 lines): interpolate from `next((c.display_name for c in state.chats if c.kind == "main"), "")`, and add optional `city` to `BuildingConfig` + `building.json`. The schema half is a real refactor — `TOOL_SCHEMAS` is a module-level constant and would need building per-run.
**Effort:** small / medium.

### One unanswered admin DM force-activates a resident forever
`scheduler.py:125-144` has no day, recency or attempt bound; `:373-377` short-circuits the whole damper stack to `prob = 1.0`. Only the resident replying *in that DM* clears it. Mechanically unbounded — but measured across 24 runs: **22 admin-DM threads, exactly 1 ends on an admin message, and that one is on the final day**, so it has never produced a stuck turn. The prompt front-loading (agent.py:136-141) and the reaction refusal (tools.py:1053) evidently work.
**Fix if you want the insurance:** bound to today/yesterday, or cap at 2-3 forced activations mirroring `_MAX_BONUS_ROUNDS`. Cheap, but it changes forcing semantics for no observed problem.
**Effort:** trivial.

### Bus and lock dicts grow forever, keyed by unvalidated run ids
`events.py:44/45/49` and `storage.py:23/31` are `defaultdict`s with no eviction; `unsubscribe` (58-59) leaves the empty set; `REPLAY_TTL_SEC` filters at read time only. Measured 3253 bytes per `message_sent` event → ~635 KB retained per run id. `api_run_events` (main.py:519-522) and the mutating endpoints touch the dicts **before** the 404: 70 POSTs to `run_nope` left permanent entries in `bus._run_locks` and `storage._state_locks`. But the documented deployment is a Heroku **Eco** dyno that sleeps after 30 min idle and cycles daily, so "process lifetime" is hours of active traffic — 92 MB needs 150 runs in one wake window.
**The actionable part** is the missing existence check before creating per-id state, plus a cap on concurrent SSE subscribers (each holds a `Queue(maxsize=500)`, ~1.6 MB if it backs up). The eviction plumbing is optional.
**Effort:** small.

---

## ⚠ Items that touch a documented invariant — think before applying

| Item | What it touches | Note |
|---|---|---|
| Fix now #5 | **CLAUDE.md:253-254** and IMPLEMENTATION.md both say `reactions_added_this_activation` and `sent_messages_this_activation` "together define an acknowledgment." | The code disagrees in three places that are all consistent with each other (tools.py:346-348, tools.py:1053, the prompt at agent.py:169-171: *"Una reazione emoji non basta"*). The scheduler side is the deliberate one; the implicit-done side was never updated. **Fix the code to match the scheduler and correct the docs**, not the reverse. |
| Fix now #6 | Containment layer (CLAUDE.md:280-303, "new containment terms go in `FORBIDDEN_TERMS` / `_content_rule_violation`"). | The fix narrows four patterns and adds no call sites; the layer and its location are unchanged. It resolves a direct contradiction with the ambient world-event design (CLAUDE.md:430-434). |
| Fix now #7 | The causality clamp and the fictional-time total order. | The fix strengthens the clamp — it does not sort by minute alone or introduce wall-clock. Currently the bonus drain silently violates the clamp the planned path enforces. |
| Fix now #9 | The day-end ordering rule ("save run BEFORE publishing"). | Applies the *same* rule to the five admin endpoints that currently invert it. Also moves `schedule_reactions` after the save, which is the safe direction (`cascaded=True` persisted pre-ack is the dangerous one). |
| Fix now #10 | "Prompt size is bounded, not growing… ~10.4k prompt chars on both day 10 and day 20." | Today that holds only for well-behaved admin input; the invariant is unenforced against caller-controlled text. |
| Worth doing A | `Message` shape / `SenderKind`. | Additive boolean only. Widening `SenderKind` would ripple into nine App.jsx branches (bubble alignment, pacing) — don't. |
| Worth doing C | CLAUDE.md says the trust scalar "feeds scheduler dampers." | It does not — `grep trust backend/scheduler.py` is empty. Either wire it or correct the doc; until then this is an observation-surface bug, which is why it is not in "Fix now." |

## Test-coverage gaps this review exposed

`grep -rn 'dials\|on_message_attack' tests/` → nothing. `grep -rn 'bonus\|pending_admin' tests/` → one comment. Motions and votes have no tally coverage. `_close_motion_if_ready` has **never executed** in any of the 24 saved runs — several runs have 5/5-yes motions still `status: "open"`, meaning it postdates them. Three of the confirmed defects above live in code that has never run in production and has no test.