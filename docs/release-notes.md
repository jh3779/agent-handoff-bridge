# Release Notes

## Unreleased

- Web UI MVP:
  - added `handoff_webui.py` — local, read-only stdlib HTTP server for the
    v0.2 chat-redesign concept in `docs/design-system/`; no provider is
    called yet, only workspace file browsing and drag/click-to-attach;
  - added `webui/index.html`, `webui/app.css`, `webui/app.js`;
  - added path-traversal-safe `/api/tree` and `/api/file` endpoints
    (`safe_join()`), covered by `tests/test_handoff_webui.py` including a
    live-server integration test and a symlink-escape test;
  - fixed a real `install`/`check` gap found while wiring this in:
    `docs/release-process.md` was required by `check` but never copied by
    `install`, so a downstream `install` + `check` would fail immediately;
  - added optional native app window support via `pywebview`
    (`choose_ui_mode()`, `--browser`/`--no-browser` flags) so the MVP tests
    as a real program instead of a browser tab, with automatic fallback to
    a browser tab when `pywebview` isn't installed; verified end-to-end on
    macOS with a real rendered-window screenshot, not just a code review;
  - added VS Code-style **Open Folder**: the workspace is now switchable at
    runtime (`POST /api/open-folder`, `AppState`) instead of fixed to
    whatever `--workspace` was at startup — native OS folder picker via a
    `pywebview` JS-API bridge (`Api.pick_folder()`), manual absolute-path
    prompt fallback in plain browser mode;
  - added local, per-workspace chat history: messages persist to
    `<workspace>/.handoff/webui/chat/YYYY-MM.jsonl` (`POST`/`GET
    /api/chat`), travels with the project folder like `.handoff/current.md`
    already does; past months are gzip-compressed automatically
    (`archive_old_months()`) so history doesn't grow unbounded;
  - `.handoff/webui/chat/` added to `.handoff/.gitignore` — local chat
    drafts default to not being committed;
  - 19 new tests (chat storage, workspace-candidate validation, isolated
    per-test live-server coverage for the two new endpoints) — 92 total.

- Web UI Phase 1 (provider connection, supersedes "no provider is called
  yet" above):
  - added `POST /api/run`: shells out to `handoff_bridge.py run <provider>
    --execute --auto-fallback` (subprocess, not an in-process call --
    `chdir_workspace()`'s cwd-relative paths aren't safe to call from a
    `ThreadingHTTPServer` request thread) and diffs `.handoff/state.json`'s
    `history[]` before/after to read back the new record(s), including
    every record an auto-fallback chain produced in one call;
  - `classify_run_status()` maps `classify_handoff()`'s
    `(handoff_needed, reason)` to `success`/`handoff`/`fail`, rendered as a
    real status badge on each agent message;
  - DEC-02 (confirm only the first send per browser session, then run
    immediately) and DEC-03 (fenced ```code``` blocks render as monospace,
    via `textContent` only -- never `innerHTML`, since a provider's
    response isn't fully trusted input) both actually implemented, not
    just designed;
  - a provider picker (`auto`/`codex`/`claude`) in the titlebar;
  - `append_chat_message()` gained an `"agent"` role (`provider`/`status`/
    `reason` fields, `agent`-only) alongside the existing `user`/`system`;
  - verified with fake `codex`/`claude` shell scripts injected onto `PATH`
    -- deterministic, no tokens spent, no network -- including a real
    auto-fallback chain (rate-limited codex -> successful claude) producing
    two history records and two agent messages in one call
    (`RunProviderViaBridgeTests`, `ApiRunLiveServerTests`);
  - fixed a real CI-only bug found after this landed: the prompt was
    appended to `handoff_bridge.py`'s argv as a trailing positional, which
    Python 3.11's argparse rejected ("unrecognized arguments") when
    interleaved after `--instruction-type <value>` even though 3.14
    accepted it -- `run_provider_via_bridge()` now writes the prompt to a
    temp file and passes `--prompt-file` instead, sidestepping both that
    and the separate argv-length/process-list-exposure concern;
  - fixed a real gap: `POST /api/chat` accepted `role: "agent"` from the
    client, letting a raw POST forge a fake agent reply with no provider
    having actually run, contradicting the documented contract that only
    `POST /api/run` writes `agent` messages -- now rejected with 400
    (`CLIENT_WRITABLE_CHAT_ROLES`);
  - fixed a real gap: the Web UI's 600s timeout only killed the outer
    `handoff_bridge.py` wrapper, not the real codex/claude child it
    spawned -- `subprocess.run()` signals just the immediate child, and
    neither process runs in its own process group, so a hung provider
    could keep running (and spending tokens) after the Web UI gave up on
    it. `--timeout-seconds` is now forwarded so the budget is enforced on
    the actual provider subprocess, which can really terminate it; the
    outer wrapper keeps a wider hard-kill backstop
    (`OUTER_SUBPROCESS_TIMEOUT_SECONDS = 600 * 2 + 60`, sized for two
    sequential auto-fallback timeouts plus `handoff_bridge.WriteLock`
    contention) for cases outside normal provider execution, and appends a
    synthetic "timed out" agent message if that backstop ever fires
    mid-fallback rather than silently showing only the first reply;
  - fixed a real crash found once `--timeout-seconds` was actually being
    forwarded: `subprocess.TimeoutExpired.stdout`/`.stderr` can still be
    `bytes` even under `text=True` -- CPython's `_communicate()` only
    decodes on the successful-return path, not the timeout path -- so a
    provider timing out mid-partial-JSONL-output would crash
    `run_provider()` before its history record was ever saved
    (`decode_timeout_output()`, `handoff_bridge.py`);
  - fixed a real schema violation: the no-history synthetic-failure record
    in `run_provider_via_bridge()` could persist `provider: "auto"`
    literally when a caller requested `"auto"` and the subprocess failed
    before any real history record existed to resolve it from --
    `docs/webui-chat-storage.md`'s schema says `provider` is "never
    `auto`"; now resolved via `choose_auto_provider()` on that path;
  - fixed a real UX gap: attachments reached `POST /api/chat` (the chat
    log) but never `POST /api/run` -- the provider itself never saw a file
    the user thought they'd attached, and an attachments-only send (no
    typed text, which the composer allows) failed outright with "text is
    required". `build_run_prompt()` folds attachment name/content into the
    actual `--prompt-file` text; `/api/run` now accepts text-or-attachments
    instead of requiring text;
  - refreshed startup/help text and the `webui/app.js` header comment,
    which still said "no provider is called" after Phase 1 wired
    `POST /api/run` up for real;
  - fixed a real race found in an independent adversarial pass: two
    concurrent `POST /api/run` calls (the Enter-key send path never
    checked whether a run was already in flight, and typing while one was
    pending could re-enable the disabled send button) diffed
    `.handoff/state.json`'s history length with no lock in between, so the
    second call to finish could duplicate the first call's already-saved
    record as a second agent chat message. A process-wide `_RUN_LOCK`
    (not `handoff_bridge.WriteLock` -- the contention is between HTTP
    threads in one process, not separate CLI processes, and WriteLock's
    10s default timeout is far too short for a provider call) now makes a
    second concurrent call fail fast with `409`
    (`RunAlreadyInProgressError`) instead of hanging or racing; the
    composer also disables itself while a run is pending so this is
    normally a backstop, not something hit directly;
  - fixed a real pre-existing bug this PR's auto-fallback UX now made
    load-bearing: `handoff_bridge.py`'s `--auto-fallback` recursion
    replaced the user's actual prompt with the literal string "Continue
    after provider handoff." before calling the fallback provider, so a
    rate-limited codex auto-falling-back into claude meant claude never
    saw what the user actually asked (or, via `build_run_prompt()`, any
    attached file content) -- `run_provider()` now threads the original
    `user_prompt` through the recursive call instead;
  - added tests for all of the above -- run
    `python3 -m unittest discover -s tests -v` for the current pass/fail
    count rather than trusting a number pinned here; it drifts every time a
    test is added (see prior review finding on this exact line).

- Web UI Phase 2 (`--workspace` becomes optional, SCR-05):
  - a pre-implementation design interview resolved DEC-04~07
    (`docs/design-system/flutter-mapping.html#s1c`) before any code
    changed, including a real revision mid-review: the first cut of DEC-04
    ("no workspace" only when cwd is invalid) would have almost never
    fired, since a running process's cwd essentially always exists --
    corrected to "cwd has no `.handoff/` marker yet";
  - `AppState.workspace` is now `Path | None`. Omitting `--workspace`
    opens cwd directly only if it's already an initialized handoff
    workspace (`has_handoff_marker()`); otherwise the server starts with
    no workspace selected instead of assuming an arbitrary cwd (e.g.
    wherever a launcher was double-clicked from) is the intended project.
    An explicitly-given `--workspace` that doesn't exist still fails
    loudly, exactly as before -- `resolve_startup_workspace()`;
  - every GET endpoint degrades gracefully instead of crashing when
    `workspace is None`: `/api/info` returns `{workspace: null}`,
    `/api/tree` and `/api/chat` return empty results, `/api/file` and
    `/api/run` return a clear 400;
  - sending the first message (attachments-only sends included) with no
    workspace selected auto-creates
    `~/Documents/Agent Handoff Bridge/<date>-<slug>/` and scaffolds it
    exactly like a manually-picked folder --
    `create_workspace_for_first_message()` shells out to `handoff_bridge.py
    init` (which installs the standard files too) for the same
    chdir-safety reason `run_provider_via_bridge()` already does;
  - `slugify_for_folder_name()` is local-only (no provider call just to
    name a folder) and Unicode-aware (`\w`), so Korean text survives
    intact instead of being stripped the way an ASCII-only slugify
    library would; name collisions get a numeric suffix, never reuse an
    existing folder;
  - the "새 폴더 자동 생성" button doesn't actually create anything --
    creation is deferred all the way to whichever message is sent first,
    so the button-first and message-first UI paths converge to one
    trigger instead of needing separate code;
  - verified with 28 new tests (pure-function coverage for the resolution/
    slugify/naming logic, a `AUTO_WORKSPACE_BASE_DIR`-patched-to-a-tempdir
    suite for real directory creation, and a live-server suite booted with
    `AppState(None)`) plus a real end-to-end run: `$HOME` swapped to a
    temp directory so the manual smoke test couldn't touch the real
    `~/Documents/`, confirming a Korean first message produces a correctly
    named, fully scaffolded workspace;
  - fixed a real, **reproduced** race found in an independent adversarial
    pass: the check-then-create in `POST /api/chat` (`if state.workspace is
    None: ... state.workspace = create_workspace_for_first_message(...)`)
    had no lock, unlike `/api/run`'s `_RUN_LOCK`. Two near-simultaneous
    first messages (a double-clicked Send, two browser tabs against the
    same server) could both observe `None` and both create a real folder
    on disk -- confirmed with a script hitting the real server with
    concurrent threads before the fix, not a theoretical concern. Fixed
    with double-checked locking (`_WORKSPACE_CREATE_LOCK`) that
    re-checks `state.workspace` after acquiring, so a request that loses
    the race just uses the workspace the winner already created;
  - fixed a related gap the same race exposed: `create_workspace_for_first_message()`
    never inspected the `handoff_bridge.py init` subprocess's result at
    all -- a failure (bad permissions, disk full) or a timeout past 30s
    would silently continue (or crash uncaught, in the timeout case)
    with `append_chat_message()` then writing into a folder whose
    `.handoff/state.json` might not even exist. Now checked and surfaced
    as a clear `WorkspaceError`, with the half-created directory cleaned
    up rather than left behind as an orphan;
  - 7 more tests for the above (a real concurrent-request test against a
    live server, confirmed to fail without the fix) -- 175 total;
  - hardened `create_workspace_for_first_message()` further, from review:
    a "successful" (exit 0) `init` is now also verified to have actually
    produced `.handoff/state.json` *and* `.handoff/current.md` before the
    workspace is confirmed -- not just trusted on the exit code alone
    (`init_handoff()` writes both unconditionally on success, so their
    absence despite exit 0 means something drifted and shouldn't be
    silently treated as a real workspace);
  - fixed a real gap: an attachments-only first message (the composer
    allows sending with no typed text) got a meaningful folder name
    (falls back to the attachment's name) but the *task* recorded in
    `.handoff/state.json` -- which feeds every future prompt's "## Task"
    section -- still fell back to the generic "Continue the current
    handoff task." placeholder, because the two fallbacks weren't sharing
    logic. `resolve_task_for_first_message()` now reuses the same summary
    source as the folder name;
  - fixed a documentation inconsistency: `docs/design-system/roadmap.md`
    said "사전 인터뷰 8건 → DEC-04~07", reading as a 8-vs-4 mismatch --
    reworded to make clear 8 is the number of interview *questions* across
    3 rounds, consolidated into 4 *decisions* (DEC-04~07);
  - 6 more tests -- 181 total;
  - fixed two more real gaps from a fourth review round:
    `AUTO_WORKSPACE_BASE_DIR.mkdir()`/`new_workspace.mkdir()` sat outside
    `create_workspace_for_first_message()`'s `try` block -- an `OSError`
    there (the base dir existing as a *file*, permissions, a full disk)
    propagated uncaught instead of the clean `WorkspaceError` -> 400 JSON
    every other failure path here already produces; and the `task` was
    passed to `handoff_bridge.py init` without a `--` end-of-options
    separator, so a first message that happened to literally be one of
    `init`'s own flag spellings (e.g. `--no-install`) would make argparse
    consume it as that option instead of the positional task and fail
    scaffolding outright -- verified by direct reproduction on the CLI
    before and after;
  - 2 more tests -- 183 total.

- Web UI Phase 3 (multi-project history drawer, SCR-03):
  - a pre-implementation design interview resolved DEC-08~12
    (`docs/design-system/flutter-mapping.html#s1c`) before any code
    changed, fully resolving both CFL-16 (history drawer data source) and
    the remaining piece of CFL-10 (registry mechanism) -- both removed
    from the Conflict List;
  - **data source** (DEC-08): the drawer reads
    `.handoff/webui/chat/` logs, not the originally-assumed provider run
    history (`.handoff/runs/` + `state.json`'s `history[]`) -- the
    wireframe's literal user-typed text only exists in the chat log.
    `pair_messages_into_turns()` collapses a `user` message plus whatever
    `agent` message(s) followed it into one drawer item; when
    auto-fallback produced more than one `agent` reply, the *last* one's
    provider/status wins (DEC-12) -- how the turn actually ended up
    matters more than the first attempt;
  - **registry** (DEC-09/10): a small `registry.json` under
    `~/Documents/Agent Handoff Bridge/` (the location Phase 2 already
    established as app-owned, not an OS-specific app-data path) tracks up
    to 50 recently-opened workspaces, LRU-ordered, updated at every point
    `AppState.workspace` gets set -- `main()`'s startup, `POST
    /api/open-folder`, and the Phase 2 auto-create path, not just explicit
    UI actions. An entry whose folder no longer exists is silently
    skipped at render time, not surfaced as an error;
  - fixed a real bug found *during* this implementation, before it ever
    shipped: the registry's file path was first written as a module-level
    constant (`REGISTRY_PATH = AUTO_WORKSPACE_BASE_DIR / "registry.json"`)
    bound once at import time -- tests patch `AUTO_WORKSPACE_BASE_DIR` to
    a tempdir to avoid ever touching the real
    `~/Documents/Agent Handoff Bridge/`, but a constant computed at import
    wouldn't see that patch, so every registry test would have silently
    written to the real path. Caught before writing any registry tests;
    fixed by making it a function (`registry_path()`) that re-reads the
    module global on every call;
  - **drawer UX** (DEC-11): current workspace pinned first regardless of
    recency, then the rest of the registry most-recently-opened first, up
    to 5 turns per workspace. Clicking an item reuses the existing
    `switchWorkspaceTo()` (same code path as Open Folder) rather than a
    new "read-only session viewer" -- simpler, and the wireframe's literal
    "읽기 전용" wording wasn't judged worth the added complexity;
  - new `GET /api/history` endpoint; `webui/index.html`/`app.js`/`app.css`
    gained the History titlebar button, slide-in drawer, and scrim;
  - fixed a real gap found in a pre-commit self-review: unlike the other
    two ways `AppState.workspace` ever gets set
    (`resolve_startup_workspace()`, `validate_workspace_candidate()`, both
    already `.resolve()`), `create_workspace_for_first_message()` built
    the new workspace path from `AUTO_WORKSPACE_BASE_DIR` without
    resolving it. `Path.home()` doesn't itself resolve symlinks (e.g.
    `~/Documents` under iCloud Desktop & Documents sync) -- the same
    physical folder reached via auto-create vs. Open Folder/CLI startup
    later could stringify differently and duplicate in the registry
    instead of deduping to one entry. Reproduced with a real symlink in a
    test, confirmed it failed before the fix and passed after;
  - fixed two more real gaps from a follow-up review round:
    `touch_registry()`/`read_registry()` let `OSError` (base dir exists as
    a file, permissions, full disk) propagate uncaught -- since
    `touch_registry()` is called from `POST /api/open-folder` and `main()`
    *after* the real state change it's attached to already happened
    (`AppState.workspace` assigned, or the server about to finish
    starting), a registry write failure could turn a successful workspace
    switch into a client-visible 500, or stop the whole server from
    starting, over what's just an LRU convenience index. Now best-effort:
    read failures return an empty list, write failures log a warning and
    return, verified by an HTTP-level test that `/api/open-folder` still
    returns 200 with the base dir forced to fail. Separately,
    `collect_recent_turns()` paired each scanned month's messages in
    isolation, so a turn whose user message landed in one month's file and
    whose agent reply landed in the next (e.g. sent right at a UTC month
    boundary) would show up with no provider/status and silently drop the
    reply -- now pairs across the merged, chronologically-ordered messages
    from every month scanned, verified with a reproduced-and-fixed
    regression test;
  - documented `registry.json`'s schema, path-normalization contract,
    50-entry LRU cap, locking, and failure-isolation policy in
    [`docs/webui-chat-storage.md`](webui-chat-storage.md#recently-opened-registry-phase-3)
    -- the repo's existing real data-model reference doc (not a new
    fictional one), extended rather than left undocumented;
  - verified with more tests (registry CRUD including failure isolation,
    turn-pairing including the multi-agent-reply and month-boundary
    cases, month-by-month backward scanning in `collect_recent_turns()`,
    drawer assembly, path-normalization via a real symlink, and
    live-server HTTP integration tests including the registry-failure
    case) -- exact count drifts with each fix, so trust
    `python3 -m unittest discover -s tests -v` over a number pinned here
    (see the Phase 1 entry above for why). Also a real end-to-end run:
    `$HOME` swapped to a temp directory, auto-created one workspace via a
    Korean first message, opened a second via Open Folder, and confirmed
    `GET /api/history` showed both in the right order with the right
    turns via curl.

- Web UI cross-phase hardening: a comprehensive review of the whole
  `handoff_webui.py`/`webui/*` surface as it stood after Phase 0-3 merged
  together (not a single-phase diff -- three parallel agents covering
  backend/security, frontend/UX, and doc accuracy), looking specifically
  for bugs in how the phases interact now that they're all live at once:
  - fixed a real gap: nothing stopped `POST /api/open-folder` from
    reassigning `AppState.workspace` while a `POST /api/run` was still in
    flight against the *old* workspace (up to `OUTER_SUBPROCESS_TIMEOUT_SECONDS`,
    ~21 minutes) -- the run's eventual reply would still get persisted to
    the correct (old) workspace's chat log server-side, but the client,
    having already switched its visible thread to the new workspace,
    would append that stale reply into the wrong project's thread once it
    resolved. `/api/open-folder` now checks `_RUN_LOCK` and returns `409`
    while a run is in progress, matching `/api/run`'s own concurrent-call
    guard; `webui/app.js`'s `switchWorkspaceTo()` (shared by Open Folder
    and every History drawer item click) gained the same `runInFlight`
    guard client-side for immediate feedback;
  - fixed a related gap the same review surfaced: `POST /api/chat`'s
    `"user"` role had no equivalent guard, so a second browser tab (or any
    direct API caller) could post a new user message into the *same*
    workspace while a run was in flight elsewhere -- `pair_messages_into_turns()`
    (Phase 3) attaches each `agent` reply to whichever `user` message it
    saw most recently in the chat log's append order, so the in-flight
    run's eventual reply could land in the log *after* the second
    message and get misattributed to it in the history drawer. Now
    rejected with `409` too (`system`-role posts are unaffected -- they
    don't start a turn);
  - fixed a real UX gap in `sendMessage()`'s auto-create-workspace error
    path: if `POST /api/chat` failed while auto-creating a workspace, the
    code fell through and called `POST /api/run` anyway with a stale
    `hasWorkspace` flag, instead of stopping once it was clear there was
    no workspace to run against;
  - fixed a latent (currently unreachable through the shipped UI, which
    never sends `model`) argv gap: `run_provider_via_bridge()` passed
    `["--model", value]` instead of `["--model=value"]`, so a model
    string starting with `-` would make argparse misparse it as the next
    flag instead of `--model`'s value -- closed the same way the prompt
    (`--prompt-file`) and `init`'s task (`--`) argv gaps were already
    closed in earlier rounds;
  - fixed several stale documentation claims found by the doc-accuracy
    pass: `docs/design-system/components.html`'s page summary said the
    history drawer components (§11/§13) had "no code" (Phase 3 shipped
    them), `wireframes.html`'s SCR-01 tag still said "provider connection
    excluded" (Phase 1 added it) and SCR-03 (history drawer) was missing
    the "actually implemented" tag every other shipped screen has,
    `cli-reference.md`'s closing pointer still listed cross-project
    history browsing as intentionally missing (Phase 3 shipped it),
    `design-system/README.md`'s page-index table cited stale DEC/CFL
    counts and an off-by-one screen count, and CFL-14's example list
    named provider-connection and history as things that might still be
    added when both had already shipped;
  - more tests for all of the above, including HTTP-level tests proving
    `/api/open-folder` and a second `POST /api/chat` both correctly get
    `409` while a run is in flight, and that a `system`-role message
    doesn't;
  - fixed a real gap a follow-up review found in the same area:
    `sendMessage()` only stopped short of calling `POST /api/run` when
    `POST /api/chat` failed *and* the workspace had just been missing --
    for an already-existing workspace, a failed or `409`-rejected
    `/api/chat` (e.g. the new concurrent-run guard above) fell through to
    `/api/run` anyway, which would itself immediately `409` too (a
    second, more confusing error stacked on the first) or, worse, let an
    agent reply render and persist with no corresponding user turn ever
    saved to back it. Now stops unconditionally on any `/api/chat`
    failure, not just the auto-create case;
  - considered, and consciously left as documented/accepted: the
    `_RUN_LOCK.locked()` checks in `/api/open-folder` and `/api/chat`
    (above) are a plain check-then-act, not atomic against a `/api/run`
    that acquires the lock in the gap between the check and the
    subsequent state change. Closing that fully would mean either
    `/api/open-folder`/`/api/chat` blocking on the same lock `/api/run`
    holds (up to `OUTER_SUBPROCESS_TIMEOUT_SECONDS`, ~21 minutes -- this
    project has deliberately favored fail-fast `409`s over blocking
    everywhere else `_RUN_LOCK` is involved) or a heavier shared-mutex
    redesign across all three endpoints. Given this is a single-user,
    single-process local tool, the residual window (a handful of Python
    bytecode instructions, down from the *entire* run's duration before
    this round) was judged not worth either tradeoff.

## v0.1.0 — 2026-08-03

First tagged release. Downloadable as `agent-handoff-bridge-macos.zip` /
`agent-handoff-bridge-windows.zip` from GitHub Releases, or usable via
`git clone`. Verify a download with `python3 handoff_bridge.py --version`
and `python3 handoff_bridge.py check` — both run with no provider tokens
spent and no git repo required.

- Release pass:
  - added `BRIDGE_VERSION`/`--version` to `handoff_bridge.py`;
  - `scripts/package_platforms.py` now bundles the quality-gate scripts and
    `tests/` into the release zips and stamps `START_HERE_*.txt` with the
    version;
  - added `docs/release-process.md` documenting how to cut a release.

- Quality gates pass:
  - added `docs/quality-gates.md` consolidating every enforced rule;
  - added branch naming convention (`type/short-description`) and
    `scripts/check_branch_name.py`;
  - added `scripts/scan_secrets.py` and wired it into `handoff_bridge.py
    check` and `.githooks/pre-commit`;
  - fixed `handoff_bridge.py` failure classification to recognize
    `tool_failure` and stay in sync with `docs/shared-agent-contract.md`;
  - added atomic, cross-process-locked writes (`atomic_write_text`,
    `WriteLock`) for `.handoff/state.json` and `.handoff/current.md`;
  - added `tests/test_handoff_bridge.py` (stdlib `unittest`) covering the
    classification, provider fallback, and shared-write logic, run by
    `handoff_bridge.py check`;
  - added `.githooks/pre-commit`, `.githooks/pre-push`,
    `scripts/install_git_hooks.sh`, and `.github/workflows/ci.yml`.

- Platform pass:
  - added cross-platform desktop controller;
  - added macOS `.command` and install launcher;
  - added Windows `.cmd` and PowerShell launchers;
  - added macOS/Windows zip package builder;
  - added platform setup documentation.

- Documentation pass:
  - added documentation index;
  - added architecture guide;
  - added CLI reference;
  - added workflow guide;
  - added Korean operator guide;
  - added security model;
  - linked the new docs from README and validation.

## Initial

- Added Codex/Claude handoff bridge.
- Added workspace controller.
- Added mobile app remote guide.
- Added preflight setup and provider/model targeting protocol.
- Added shared contract and verification playbook.
- Added optional hook examples and custom HTTP remote scripts.
