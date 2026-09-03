# Design: Multi-Session Support ("session splitting", tabs)

Date: 2026-09-03

## Bottom Line

Settled via a design interview (`AskUserQuestion`, two rounds) rather than
an open comparison — this doc records the decided direction and works out
the concrete mechanism, not a menu of options:

- **Session unit**: both — multiple independent chats *within one
  workspace* (e.g. codex refactoring the backend while claude writes tests,
  same folder) and multiple *different workspaces* open at once (like
  browser tabs).
- **Concurrency**: genuinely parallel provider execution, not
  one-at-a-time-but-switchable. This is the harder of the two options that
  were offered, chosen deliberately.
- **UI**: tabs first; a later, separate pass may add VS Code-style
  side-by-side split panes — explicitly out of scope here.
- **Persistence**: open tabs survive an app restart (closing/reopening the
  app restores the same tab set), the same "state.json/registry.json
  survive a restart" posture this project already has everywhere else.
- **Scope of this pass, as originally written**: design only, no code yet
  — matching how Phase 7 (`docs/research-phase7-framework.md`) and
  API-key mode (`docs/research-api-key-mode.md`) were both designed
  before being built. **Status as of 2026-09-03**: M1 (backend session
  model), M2 (frontend tab bar), and M3 (verified concurrent execution)
  have all since shipped — see "Proposed Milestones" below for what was
  actually built, including one correction to this doc's original
  concurrency design and one scope reduction in the frontend, both found
  and resolved during implementation rather than being re-designed from
  scratch. Only M4 (split-pane layout, a separate future decision) remains
  unstarted.

**Key finding that shapes everything below**: this is achievable as a
**pure Python (`handoff_webui.py`/`webui_*.py`) + JS (`webui/app.js`)
change**. No Tauri/Rust change is needed — the whole multi-session concept
can live entirely inside the single existing HTTP server process and the
single existing browser window, discriminated by a per-request session
identifier. "Tabs" here means browser-tab-like UI inside today's one
window, not multiple native OS windows.

## Current Architecture (why this isn't free)

- `AppState.workspace: Path | None` — exactly one workspace per running
  server process. Every handler in `handoff_webui.py` reads/writes this
  one field directly.
- `_RUN_LOCK = threading.Lock()` (`webui_bridge_run.py`) — a single,
  process-wide lock. Only one call to any provider CLI can be in flight
  anywhere in the app at any moment; a second concurrent `/api/run` gets
  `RunAlreadyInProgressError` (409), by design (DEC-era decision to avoid
  two runs writing into the same chat log's turn-pairing at once — see
  below).
- Chat storage (`webui_chat_storage.py`): one monthly log per workspace,
  `<workspace>/.handoff/webui/chat/YYYY-MM.jsonl[.gz]`
  (`CHAT_DIR_RELATIVE`). `pair_messages_into_turns()` walks this file in
  append order and attaches each agent reply to whichever user message it
  saw most recently — **two sessions in the same workspace appending to
  this same file concurrently would corrupt that pairing** (an interleaved
  write from session B could get attributed to session A's most recent
  message). This is the real reason `_RUN_LOCK` is global today, not
  per-workspace.
- Frontend (`webui/app.js`): module-level mutable state
  (`hasWorkspace`, `attachments`, `runInFlight`, the chat-thread DOM, the
  provider-select value, etc.) — implicitly "the one active session,"
  never an array of sessions.
- `fetchJSON()`/`postJSON()` (`webui/app.js`) are the **only two places**
  every API call in the entire frontend passes through — a fact the
  proposed mechanism below leans on directly.

## Proposed Model

### Session identity

- `session_id`: an opaque string generated server-side (e.g.
  `secrets.token_hex(8)`) when a tab is created.
- Carried on every request as a header, `X-AHB-Session`, attached
  automatically inside `fetchJSON()`/`postJSON()` — since those two
  functions are the universal choke point, this is a **two-function
  change**, not a change to every individual call site across the
  frontend.
- A request with no `X-AHB-Session` header (an old cached frontend build,
  or a direct API script like the ones `docs/cli-reference.md` documents)
  falls back to a fixed sentinel session id, `"default"` — so nothing
  that talks to this API today breaks.

### `AppState` shape

- Today: one `workspace: Path | None` field.
- Built in M1: `sessions: dict[str, SessionState]`, where `SessionState`
  holds just a `workspace: Path | None`. A small separate meta-lock (held
  only briefly) protects `sessions` itself from concurrent create/delete
  races.
- Provider/model selection, composer draft text, and attachments-in-
  progress are already frontend-local, per-tab-shaped state today (the
  server is stateless per request about which provider was picked) — they
  need no server-side session concept at all, only the frontend's own
  per-tab state object (see below).

### Concurrency

**Revised during M1's own implementation** — this section originally
proposed giving each *session* its own run lock (`SessionState.run_lock`).
That was wrong, caught before it shipped: `webui_bridge_run.py` identifies
"the record(s) a call produced" by diffing
`<workspace>/.handoff/state.json`'s history length before/after the
subprocess call, with no lock of its own around that diff — a mechanism
that is only race-free when exactly one lock guards every access to that
one shared file. Two sessions on two *different* workspaces have
independent `state.json` files and correctly need independent locks (a
per-session lock gets this right); two sessions on the *same* workspace
share one `state.json`, and per-session locks would let their before/after
reads interleave, silently misattributing or duplicating a chat message —
exactly the bug the pre-M1 single global lock existed to prevent, just
reintroduced for the same-workspace multi-session case specifically.

**What M1 actually built**: `AppState.get_run_lock_for(workspace)`, keyed
by workspace path rather than session id. Any two sessions pointed at the
same workspace resolve to the *same* lock object (their runs correctly
serialize against each other); sessions on different workspaces resolve to
different lock objects (their runs genuinely proceed in parallel). This
still delivers real parallelism for the case that actually has independent
state to race over, and stays safe (not silently wrong) for the case that
doesn't — `subprocess.run()` releases the GIL while waiting on the child
process, so two threads handling two different workspaces' `/api/run`
calls already run two real `codex`/`claude`/`gemini` subprocesses side by
side. `RunAlreadyInProgressError` keeps its existing meaning, re-scoped to
"a run against this workspace is already in flight" rather than "the whole
app has one in flight anywhere."

A **known, accepted limitation** left open by this: two sessions on the
same workspace cannot have literally concurrent provider runs — they
serialize, safely, rather than running in parallel. Removing that
constraint too would need `handoff_bridge.py`'s `run` subcommand to expose
a stable per-invocation identifier (it already generates one internally,
`run_id`/`run_dir` in `run_provider()`) so callers can identify their own
record(s) without relying on a position-based diff of a file two
concurrent writers might both be appending to. That is a separate, larger,
not-yet-scoped change — see "Open Implementation Questions" below.

### Chat storage

- A workspace's file tree and attachments stay genuinely shared across
  every session pointed at it (both tabs see the same files) — only
  conversation history needs to be session-scoped.
- Proposed path: `<workspace>/.handoff/webui/chat/<session_id>/YYYY-MM.jsonl[.gz]`
  for any session other than `"default"`.
- **Backward compatibility, not a migration**: the `"default"` sentinel
  session keeps using today's existing unscoped path
  (`<workspace>/.handoff/webui/chat/YYYY-MM.jsonl[.gz]`) unchanged — a
  workspace that already has chat history from before this feature existed
  keeps working with zero migration script needed. Only sessions created
  *after* this feature ships get their own subfolder.

### Persistence (tab restore across restarts)

- New file, `AUTO_WORKSPACE_BASE_DIR/sessions.json` — same location and
  read/write posture (function-based path, not a module constant, so
  tests can patch `AUTO_WORKSPACE_BASE_DIR`) as the existing
  `registry.json`/`credentials.json`. An ordered list of
  `{session_id, workspace, last_active_at}`, written whenever a tab is
  created, closed, or switched to.
- On boot, `handoff_webui.py` reads this file and the frontend rebuilds
  the same tab bar, each tab independently re-fetching its own chat
  history for its `(workspace, session_id)` pair — the same per-tab boot
  sequence today's single-session `boot()` already does, just run once per
  restored tab instead of once globally.

### API changes

- New endpoints: `POST /api/sessions` (create a tab; an optional
  `workspace` body field, or none — an empty-workspace tab is already a
  supported state today), `GET /api/sessions` (list open tabs, used on
  boot to restore them), `DELETE /api/sessions/:id` (close a tab, removing
  its `sessions.json` entry — does **not** delete its chat history on
  disk, only stops tracking it as an open tab).
- Every existing workspace/chat/tree/run endpoint resolves
  `state.sessions[session_id]` (from the `X-AHB-Session` header, falling
  back to `"default"`) as its first step, instead of reading
  `state.workspace` directly — mechanical but touches most handlers in
  `handoff_webui.py`.

### Frontend changes

- A tab bar UI element (new, above or integrated into the existing
  titlebar), each tab showing its workspace name and a busy/unread
  indicator while a background run is in flight.
- Today's module-level single-session variables become one state object
  per session id, with an `activeSessionId` pointer deciding what's
  currently painted into the DOM.
- **As originally proposed here**: switching tabs would swap which
  object's *cached* state renders — no network round-trip needed if that
  tab already loaded its data once, matching how a real browser tab or
  IDE pane feels. **What M2 actually shipped instead** (a deliberate,
  reviewed scope reduction — see M2's entry in "Proposed Milestones"
  below for why): switching tabs re-fetches that session's workspace/
  tree/chat from the server every time, the same round trip Open Folder/
  a History-drawer item already pays today. Only small, cheap-to-keep
  state (attachments-in-progress, the composer draft, provider/model
  selection) is actually cached per tab — chat messages and the file tree
  are not.
- A background tab's in-flight `/api/run` call is just an async function
  already (`sendMessage()`) — the only real change is that `runInFlight`
  stops being one global boolean and becomes a per-session flag, so a
  background tab's request can resolve and update *that* tab's badge
  without being blocked by, or blocking, whatever the active tab is doing.

## Explicitly Out Of Scope This Pass

- Split-pane / side-by-side simultaneous view of two sessions — deferred
  to a later, separate milestone per the user's explicit "탭으로 시작,
  나중에 스플릿 추가" choice.
- Any `src-tauri/` (Rust) change — everything above is achievable without
  touching the Tauri shell, sidecar spawning, or window model at all.
- Cross-session actions (e.g. "send this file from session A into session
  B's conversation") — never requested, no design exists for it here.

## Proposed Milestones

Mirrors how Phase 7b was broken into M1..M6 rather than shipped as one
giant PR:

- **M1 — Backend session model. ✅ Done** (PR #50, merged 2026-09-03).
  `AppState.sessions`, workspace-keyed `get_run_lock_for()` (revised from
  this doc's original per-session-lock proposal — see "Concurrency"
  above), the `X-AHB-Session` header contract with `"default"` fallback,
  `POST`/`GET`/`DELETE /api/sessions`, chat-storage path scoping. No
  frontend change — the existing single-session frontend keeps working
  completely unchanged via the `"default"` fallback, confirmed by all 585
  pre-existing tests passing with zero modification to their expected
  behavior.
- **M2 — Frontend tab bar. ✅ Done** (branch `feature/multi-session-m2-frontend`,
  2026-09-03). A tab bar (titlebar-adjacent, `webui/index.html`'s
  `#tab-bar`) backed by `sessionMetaById: Map<sessionId, meta>` in
  `webui/app.js` — one shared set of DOM elements (chat thread, tree,
  composer) is repainted for whichever session is active; switching tabs
  re-fetches that session's workspace/tree/chat rather than caching N
  independent DOM subtrees (a deliberate scope reduction from this doc's
  original "no network round trip on switch" framing — see its own note
  below). What *is* kept in memory per tab, cheaply: attachments-in-
  progress, the composer draft, and the provider/model selection.
  `sessions.json` persistence + restore-on-boot shipped as part of M1's
  backend work already (`write_persisted_sessions()`/
  `restore_persisted_sessions()` in `handoff_webui.py`), so M2 only had
  to read `GET /api/sessions` at boot and render the restored tabs — no
  separate persistence milestone was actually needed. `sendMessage()`
  (and `switchWorkspaceTo()`) capture their owning session id up front and
  guard every DOM mutation with `sessionId === activeSessionId`, so a
  background run finishing while a different tab is active never paints
  into the wrong tab's chat thread — it sets a `hasUnseenReply` flag
  (rendered as a tab-bar badge) instead, resolved the next time that tab
  becomes active and re-fetches its real history.
  **Scope reduction from the original plan**: this doc's "Frontend
  changes" section originally described switching as instant/cached,
  "no network round trip needed if that tab already loaded its data
  once." What shipped instead re-fetches on every switch (the same round
  trip Open Folder/a History-drawer item already pays) -- a deliberate,
  lower-risk simplification given this codebase has zero automated
  frontend/interaction test coverage (all existing tests are HTTP/
  backend-level) and no browser-automation tooling available in this
  session's environment to verify a fully cached model. **Consequence**:
  no interactive click-through verification (create/switch/close tabs
  in a real browser) has been done for this milestone -- confirmed
  correct via full manual code review, real dev-server `curl` checks of
  every changed HTTP-facing behavior, `node --check` on both JS files,
  and the ko/en i18n key-parity script, but the actual UI interaction
  needs the user's own confirmation once used for real.
- **M3 — Verified concurrent execution. ✅ Done** (2026-09-03). New
  `RealConcurrentExecutionTests` in `tests/test_handoff_webui.py`, using
  a deliberately slow fake provider script (`FAKE_CODEX_SLOW_SUCCESS`,
  a real subprocess that sleeps 1.2s) driven through the real HTTP
  server with two real threads — timing and status codes prove what a
  mocked lock (M1's `GetRunLockForTests`/`MultiSessionLiveServerTests`)
  can't actually demonstrate on its own:
  - **Different workspaces genuinely overlap**: two concurrent
    `/api/run` calls against two different workspaces both return 200
    in well under 2x the single-call duration (would be >=2.4s if
    serialized; observed comfortably under 2.0s) — real parallel
    subprocess execution, not just "structurally allowed to be."
  - **Same workspace correctly rejects the overlap, not just delays
    it**: `run_lock.acquire(blocking=False)` (`webui_bridge_run.py`)
    means the *correct* behavior for two genuinely concurrent same-
    workspace calls is one 200 + one immediate 409, not a queued wait —
    so this is a status-code assertion, not a timing one (elapsed time
    can't distinguish "correctly serialized" from "buggy: both ran
    concurrently," since both produce the same ~1.2s wall-clock
    result). Confirmed both calls do land as exactly one 200 and one
    409 when run through the real server with a real overlapping
    subprocess window.
  - **Sequential (non-overlapping) same-workspace calls each get
    exactly their own record**: reproduces M1's original bug scenario
    end-to-end (real HTTP server + real subprocess, not a
    webui_bridge_run.py-level unit test) — two sequential runs from two
    different sessions on the same workspace each see exactly one new
    message, confirming the position-based history diff isn't
    misattributing across sessions when there's no actual race.
  The tab-bar busy/badge indicators this milestone's own description
  called for were already built as part of M2 (`meta.runInFlight`/
  `meta.hasUnseenReply` in `webui/app.js`) — nothing further was needed
  there.
- **M4 (separate future decision, not part of this feature at all)** —
  split-pane layout, if/when greenlit separately. A fully-cached,
  zero-round-trip tab switch (the capability this doc originally
  described for M2) would also be a natural candidate for a future pass,
  if the current re-fetch-on-switch latency ever proves to matter in
  practice.

## Open Implementation Questions

- Exact session-id generation scheme: resolved during M1 as
  `secrets.token_hex(8)` (16 hex characters).
- Whether closing the last remaining tab should auto-create a fresh empty
  one (matching today's permanent "no workspace" empty state) or allow a
  genuinely tab-less window: resolved during M2 by construction — the
  default session can never be closed at all (server- and client-side),
  so there is always at least one tab; this question doesn't arise.
- Whether an unbounded number of concurrently open sessions needs a soft
  cap, given each one can have a real CLI subprocess running at once
  (resource/cost concern, not a technical blocker): still open.
- **Found during M1**: same-workspace sessions cannot run literally
  concurrently (they safely serialize instead, via
  `get_run_lock_for()`'s workspace-keyed lock) — removing that would need
  `handoff_bridge.py run` to expose a stable per-invocation identifier
  instead of `webui_bridge_run.py` relying on a position-based diff of
  `.handoff/state.json`'s history. Separate, larger, not yet scoped; not
  blocking M2/M3.
- **Found during M2**: switching tabs re-fetches from the server instead
  of the fully cached model this doc originally proposed (see M2's own
  entry above for why) — not blocking, but means tab-switch latency
  scales with network/disk I/O rather than being instant.
