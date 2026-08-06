# Handoff Packet

## User Task

No active task has been initialized yet.

## Current State

- Status: installed.
- Primary provider: codex.
- Fallback provider: claude.

## Active Work Target

- Provider: Either.
- Model: app-selected default.
- Account/App: CLI bridge or mobile remote.
- Workspace: this repository.
- Instruction type: setup.

## Handoff Rules

- Follow `docs/preflight-setup-guide.md` before first remote use.
- Follow `docs/agent-targeting-protocol.md` for every task change or handoff.
- Follow `docs/shared-agent-contract.md`.
- Verify with `docs/verification-playbook.md`.
- Read this file before continuing.
- Inspect the workspace and git status before editing.
- Keep raw provider logs under `.handoff/runs/`.
- Update this file before stopping so the next CLI can continue naturally.

## Latest Summary

This is a bootstrap packet for a fresh workspace. Run
`python3 handoff_bridge.py init "<task>"` or use `python3 handoff_control.py` to
create a task-specific packet.

### 2026-08-05 — Phase 4: API-key mode (chat-only)

- **Target**: Claude Code CLI, web UI feature work (`handoff_webui.py`,
  `webui/*`), branch `feature/api-key-mode-phase-4`, PR #7
  (https://github.com/jh3779/agent-handoff-bridge/pull/7), not yet merged.
- **Changed**: Added API-key mode for providers with no local CLI detected
  — a "Diagnose" panel (`webui/index.html`/`app.js`/`app.css`) to save a
  per-provider API key (+ optional model), backed by new
  `GET /api/providers`/`POST /api/provider-key` in `handoff_webui.py`.
  Calls Anthropic's Messages API / OpenAI's Responses API directly via
  `urllib` (no new dependency) instead of shelling out, only when a
  provider's CLI is genuinely absent and a key is saved — CLI always wins
  when detected. Deliberately **chat-only** this phase (no file edits/shell
  exec via API-key mode); full agentic parity deferred to a future phase
  (CFL-17). Credentials in `~/Documents/Agent Handoff Bridge/
  credentials.json` (0600 perms), never inside a workspace. Resolves
  CFL-12 (`docs/design-system/flutter-mapping.html`, DEC-13~16). New
  research doc: `docs/research-api-key-mode.md`. Docs updated:
  `docs/design-system/roadmap.md`, `docs/design-system/components.html`,
  `docs/webui-chat-storage.md`, `docs/provider-extensibility.md`,
  `docs/cli-reference.md`, `docs/release-notes.md`, `docs/index.md`.
- **Verified**: `python3 -m unittest discover -s tests -v` — 251 tests
  passing (35 new). `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Branch name passes
  `scripts/check_branch_name.py`. Independent adversarial self-review
  (background agent) run before opening the PR; 3 real findings fixed
  (an `"auto"` literal that could leak into a persisted chat-log
  `provider` field, an uncaught `json.JSONDecodeError` on a malformed
  200 response, one docs overclaim) and re-verified.
- **Remaining (superseded, see 2026-08-05 Phase 5 entry below)**: PR #7
  merged.

### 2026-08-05 — Phase 5: Gemini CLI as a third provider + fallback target selection

- **Target**: Claude Code CLI, bridge core + web UI work
  (`handoff_bridge.py`, `handoff_webui.py`, `webui/*`), branch
  `feature/gemini-cli-phase-5`, PR #8
  (https://github.com/jh3779/agent-handoff-bridge/pull/8), **merged**.
- **Changed**: Added Gemini CLI as a third provider. `PROVIDERS` extended
  to `("codex", "claude", "gemini")`. `other_provider()`'s hardcoded
  binary toggle replaced with `next_provider()`/`next_available_provider()`
  (the latter skips uninstalled CLIs) — generalizes which provider a
  fallback hop lands on; auto-fallback itself stayed exactly one hop by
  design (unchanged token-spend-bounding decision from `docs/research.md`,
  not full N-way retry-until-exhausted chaining — the PR title/docs were
  corrected mid-review after initially overselling this as "N-way
  fallback"). `summarize_gemini()` added (Gemini's `--output-format json`
  returns one JSON object per run, not a JSONL stream). Gemini's
  `session_id` is always the literal sentinel `"latest"` (no real session
  ID exists in its output), set only when `exit_code == 0` with no
  `error` field — DEC-17 (`--resume latest`, after confirming Gemini
  sessions are scoped per workspace directory, not global). DEC-18:
  `diagnose()` does not probe Gemini's auth status (no free command
  exists; a real probe would cost a token every run) — shows "gemini
  auth: not checked". `handoff_webui.py` got `API_KEY_MODE_PROVIDERS =
  ("codex", "claude")`, deliberately separate from the now-3-wide
  `PROVIDERS` import, so Gemini doesn't silently inherit Phase 4's
  API-key mode. Resolves CFL-13. New research doc:
  `docs/research-gemini-cli.md`.
- **Verified**: `python3 -m unittest discover -s tests -v` — 290 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Three review rounds on the PR
  (one self-review before opening, two real automated reviews posted
  directly on GitHub) found and fixed real issues: a stale hardcoded
  binary guess in `handoff_webui.py`'s timeout handler the refactor
  missed; `summarize_gemini()` marking the resume sentinel without
  checking `exit_code`; a Gemini `AuthError`/exit-41 auth failure
  misclassifying as `unknown` instead of `auth`
  (`ERROR_PATTERNS` needed one addition); the single-hop fallback
  skipping past an installed, working provider when the naive
  next-in-order pick wasn't installed; a real CI failure (a test passed
  locally on a dev machine with real `codex`/`claude` installed but
  failed in CI's clean environment) fixed by pinning `shutil.which` in
  that test; a docs contradiction about whether `ERROR_PATTERNS`
  changed; the "N-way fallback" naming overclaim; and a verified-but-
  undocumented fact (bare piped stdin with no `-p` flag also triggers
  Gemini's non-interactive mode) that was found during research but
  never made it into the written doc, closing an apparent mismatch
  between documented research and the actual implementation. One
  recurring review claim (`docs/local-data-model.md`/`docs/adr/0010-*`
  etc. needing updates) was verified and rejected as false across
  multiple rounds — neither has ever existed in this repo's history
  (`git log --all` confirms), and this same claim was already raised and
  resolved once before on an earlier PR (commit `e6c74c1`).
- **Remaining (superseded, see 2026-08-05 Phase 6 entry below)**: PR #8
  merged.

### 2026-08-05 — Phase 6: automatic update check via local gh CLI auth

- **Target**: Claude Code CLI, bridge core + web UI work
  (`handoff_bridge.py`, `handoff_webui.py`, `webui/*`), branch
  `feature/auto-update-check-phase-6`, PR #9
  (https://github.com/jh3779/agent-handoff-bridge/pull/9), **merged**.
- **Changed**: Titlebar update-check badge (SCR-07/components.html §15).
  `handoff_bridge.py`: `GITHUB_REPO`, `parse_version_tuple()`,
  `check_for_update()` — shells out to local `gh` CLI (`gh release view
  --repo <repo> --json tagName,url`) rather than building new public
  infrastructure, since this repo is private (DEC-19, resolves CFL-11).
  `handoff_webui.py`: `AppState.update_info`/`update_checked`, a
  background daemon thread started in `main()` so the real network call
  never delays startup, `GET /api/update-check` reading the cache.
  Frontend: always-visible titlebar button, dot badge only when an
  update exists, popover with version + release-notes link, reuses the
  existing toast for "up to date"/"still checking" states rather than
  inventing new UI the wireframe never mocked.
- **Verified**: `python3 -m unittest discover -s tests -v` — 310 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Four review rounds (one
  self-review before opening, two real automated reviews on GitHub, one
  more self-review requested explicitly) found and fixed real issues,
  most notably a **highly-reachable race**: the frontend originally
  checked `/api/update-check` exactly once at page load with no retry,
  but the real `gh` subprocess call (network I/O) can easily still be
  running at that point, especially right after server startup — so a
  normal server start could silently and permanently miss showing the
  badge even when an update genuinely existed. Fixed with an
  `update_checked` pending/done flag and bounded frontend polling
  (1.5s × 10). A follow-up self-review then found the **reader side of
  the same race**: the HTTP handler read `update_info` before
  `update_checked` (the opposite order from how the background thread
  writes them), so the two writes landing between the handler's two
  reads could still report a stale "checked, no update" for a request
  that actually raced a real update being found — fixed by reading
  `checked` first (the safe-to-be-wrong-in direction). Also fixed in the
  same round: the frontend's polling `catch` block gave up permanently
  on any single transient fetch error instead of retrying (undermining
  the whole point of the polling fix), and an off-by-one that let 11
  fetches through a nominal 10-fetch bound. One low-severity, explicitly
  non-blocking finding (`check_for_update()` can't distinguish "genuinely
  up to date" from "couldn't check at all" — `gh` missing/unauthenticated
  cases show the same "최신 버전" toast) was recorded as a consciously
  accepted tradeoff, not fixed — **CFL-18** in
  `docs/design-system/flutter-mapping.html`.
- **Remaining**: Phase 7 (framework migration, DEC-01, Tauri/Electron) is
  the only unstarted item on the original roadmap — and it's explicitly
  the "최종 목표" (final goal), a full rewrite of the stdlib-http-server
  MVP onto a real production stack, not an incremental feature addition
  like Phases 1-6. It also has real unresolved prerequisites of its own
  (CFL-09: the whole "download a zip, no git required" release/packaging
  pipeline needs a full redesign once real installable binaries replace
  it). CFL-17 (full agentic parity for API-key mode) and CFL-18 (above)
  remain deliberately undesigned/unfixed.
- **Blocked**: none technically, but Phase 7's scope is large enough
  (and consequential/hard-to-reverse enough — migrating the actual
  shipped UI stack) that it's worth confirming with the user whether to
  actually start it now, versus treating Phases 0-6 (the full v0.2
  chat-redesign feature set) as a natural stopping point.
- **Next**: presented Phase 7's scope to the user for a go/no-go/defer
  decision rather than starting the framework migration unprompted. User
  chose to **stop here** — Phases 0-6 (the full v0.2 chat-redesign
  feature set: provider connection, auto-created workspaces, history
  drawer, API-key mode, Gemini as a third provider, auto-update check)
  are the intended resting point. Phase 7 (framework migration, DEC-01)
  and the deliberately-deferred items (CFL-17: full agentic parity for
  API-key mode; CFL-18: update-check can't-check-vs-current ambiguity)
  remain open on the roadmap for whenever this project picks back up,
  with no further action needed right now.

### 2026-08-05 — CFL-18 fix: update-check status contract (DEC-20)

- **Target**: Claude Code CLI, bridge core + web UI work
  (`handoff_bridge.py`, `handoff_webui.py`, `webui/app.js`), branch
  `fix/update-check-status-distinction`, PR #10
  (https://github.com/jh3779/agent-handoff-bridge/pull/10), **merged**.
  User asked to work through the remaining open items smallest-first
  (Phase 7 > CFL-17 > CFL-18); this was the smallest.
- **Changed**: `check_for_update()` previously returned `None` for two
  different situations a caller couldn't distinguish — "checked
  successfully, nothing newer" and "couldn't check at all" (`gh`
  missing/unauthenticated/offline/unparseable response) — so a user
  without `gh` set up saw the same "최신 버전을 사용 중입니다" toast as
  someone genuinely confirmed current. It now always returns a dict
  (never `None`, never raises) with a `status` field: `"available"`
  (new release, unchanged extra fields), `"current"` (checked, nothing
  newer), or `"unavailable"` (every unreadable-response case collapsed
  together, same fail-silent posture as `touch_registry()`).
  `GET /api/update-check` drops the old `update_available` boolean and
  exposes `status` directly. `webui/app.js` tracks a `latestUpdateStatus`
  string (`"pending"|"available"|"current"|"unavailable"` — `"pending"`
  is purely local frontend/API state, not something `check_for_update()`
  itself returns) and shows a fourth distinct toast
  ("업데이트를 확인할 수 없습니다") for the unavailable case. Resolves
  CFL-18 as **DEC-20** in `docs/design-system/flutter-mapping.html`
  (moved from the Conflict List into the Decision Log); matching updates
  in `docs/cli-reference.md`, `docs/release-notes.md`,
  `docs/design-system/components.html`, `docs/design-system/roadmap.md`.
- **Verified**: `python3 -m unittest discover -s tests -v` — 311 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. `node --check webui/app.js`
  passes. Independent adversarial self-review (background agent) before
  opening the PR found one real gap (`docs/design-system/roadmap.md`
  still described the old None-returning behavior and only three
  `/api/update-check` states) — fixed before the PR was opened. Two
  rounds of real automated review on GitHub: round 1 found one genuine
  doc/comment inaccuracy (`docs/cli-reference.md` and an `app.js`
  comment both attributed the `"pending"` state to `check_for_update()`'s
  return contract, when `"pending"` is purely local frontend/API state
  before `check_for_update()` is ever consulted) — fixed. Round 2 found
  no blocking issues and confirmed the round-1 fix, plus one more real
  catch: two code comments (`handoff_bridge.py`, `tests/
  test_handoff_bridge.py`) still pointed at
  `flutter-mapping.html#s2` (the Conflict List anchor, correct while
  CFL-18 lived there) instead of `#s1c` (the Decision Log, where DEC-20
  actually lives now that the CFL-18 row was removed) — fixed and
  pushed before merge. Declined one review suggestion (keep a legacy
  `update_available` derived field for API back-compat) — this is an
  internal-only endpoint with no external consumers, and preserving the
  ambiguous field would defeat the point of the fix. The recurring false
  claim about `docs/local-data-model.md`/`docs/adr/*` not existing
  resurfaced again as a caveat (not a real finding) in both rounds — no
  action needed, consistent with every prior instance of this claim.
- **Remaining**: Phase 7 (framework migration, DEC-01) and CFL-17 (full
  agentic parity for API-key mode) are the only items still open. Both
  remain deliberately deferred per the user's earlier explicit "stop
  here" decision after Phase 6, and both are large/undesigned enough
  (Phase 7 is a full stack rewrite and the project's stated final goal;
  CFL-17 would add a new tool-execution/sandboxing surface with no
  design yet) that they should go through the same
  interview-before-implementing discipline as every prior phase, not be
  started opportunistically.
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to pick up CFL-17 or Phase 7.

### 2026-08-05 — CFL-17: full agentic tool parity for API-key mode (DEC-21)

- **Target**: Claude Code CLI, bridge core work (`handoff_webui.py`),
  branch `feature/api-key-mode-tool-parity`, PR #11
  (https://github.com/jh3779/agent-handoff-bridge/pull/11), **merged**.
  User asked to work through the remaining open items smallest-first;
  after CFL-18 this was the next (and last) one before Phase 7.
- **Changed**: API-key mode started chat-only (Phase 4, DEC-13),
  explicitly deferring file-edit/shell-exec parity with CLI mode as
  CFL-17. This resolves it. A design interview (`AskUserQuestion`)
  resolved two real forks before any code was written: **scope** = build
  file tools (`read_file`/`write_file`/`edit_file`) *and* the shell tool
  (`run_shell`) together in one pass — the larger/riskier option, not
  the more conservative file-tools-only recommendation; **confirmation
  UX** = reuse DEC-02 as-is (confirm only the first send per session)
  for every tool call this adds, rather than a stronger per-call
  confirmation — extending CLI mode's existing trust level (real
  `codex`/`claude` subprocesses already have full local shell access) to
  API-key mode, not a new tier. `call_anthropic_messages_api()`/
  `call_openai_responses_api()` (previously single stateless calls) now
  run a full tool-use turn loop in place — same function names/contract,
  one new `workspace` parameter; a response with no tool call still
  returns after one HTTP call, so this is a strict superset of the prior
  behavior. Four tools declared once in `_TOOL_SPECS`, rendered into
  each vendor's own schema shape via `anthropic_tool_definitions()`/
  `openai_tool_definitions()` so they can't drift apart. File tools
  reuse the existing `safe_join()` primitive for workspace confinement.
  `run_shell` sets `cwd=workspace` as its starting directory — **not a
  sandbox**: an absolute path or `..` still reaches anywhere the OS user
  account can, same as a real terminal or CLI mode's own subprocess —
  with a timeout and output cap, no command allowlist, per the
  interview's chosen scope. Tool-call activity renders via the existing
  fenced-code-block convention (DEC-03), no new message schema. Both
  vendors' tool-use JSON shapes were confirmed against current official
  docs before implementing, not assumed (cited in each function's
  docstring and `research-api-key-mode.md`'s Sources list). Resolves
  CFL-17 as **DEC-21** in `docs/design-system/flutter-mapping.html`
  (moved from the Conflict List into the Decision Log); matching updates
  in `docs/cli-reference.md`, `docs/release-notes.md`,
  `docs/provider-extensibility.md`, `docs/webui-chat-storage.md`,
  `docs/design-system/{components,roadmap,wireframes}.html`.
- **Verified**: `python3 -m unittest discover -s tests -v` — 353 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Four review rounds total
  before merge, given this is a genuinely new shell-exec surface — an
  independent adversarial self-review before opening the PR, then three
  real automated GitHub reviews (each on the previous round's fix
  commit), every one finding real issues and none blocking merge by the
  end:
  - **Self-review**: `MAX_TOOL_ITERATIONS` bounded HTTP round trips, not
    actual tool executions — a single response can carry more than one
    tool call, so a model batching many into one response could execute
    well past the intended cap. The Anthropic loop also only executed
    the first `tool_use` block per response, silently dropping any
    others if the API ever didn't honor the `disable_parallel_tool_use`
    hint. Both fixed; both loops now track actual executions and handle
    every tool-call block defensively.
  - **Round 2**: a mid-turn API failure could discard the record of a
    tool that had *already* executed (real side effects, e.g.
    `write_file`/`run_shell`) — undermining DEC-21's own premise that
    post-hoc chat-log visibility substitutes for a per-call
    confirmation. Fixed by prefixing any accumulated transcript onto the
    failure message (`_error_with_transcript()`). Also found `read_file`
    bypassed the same output cap `run_shell` already respected (256KB
    vs. 4000 chars) — fixed. One more finding marked optional by the
    review itself, fixed anyway: the fenced-code-block transcript could
    break if tool output/arguments contained their own ` ``` ` —
    `_escape_fence()` neutralizes embedded backtick runs.
  - **Round 3**: the transcript's *argument* side (e.g. `write_file`'s
    `content`) had no length cap even after Round 2 capped the result
    side — `_truncate_for_transcript()` now bounds both. Also: this
    project's own wording describing `run_shell` as "cwd-confined"
    could read as claiming stronger isolation than it actually has —
    reworded everywhere (code comments, docs, PR description).
  - **Round 4**: `TOOL_EXEC_TIMEOUT_SECONDS` only guarantees killing the
    immediate subprocess, not a whole process tree a
    backgrounded/forked command might spawn — documented as a known,
    accepted gap rather than adding cross-platform process-group
    cleanup (meaningfully larger scope than this fix round), the same
    posture DEC-21 already takes toward `run_shell` having no command
    allowlist.
  New regression tests cover every fix directly.
- **Remaining**: only **Phase 7** (framework migration, DEC-01) is left
  open — CFL-17 and CFL-18 are both now resolved. Phase 7 remains
  deliberately deferred per the user's earlier explicit "stop here"
  decision after Phase 6, and it's the project's stated final goal (a
  full rewrite of the stdlib-http-server MVP onto a real production
  stack, with its own unresolved prerequisite: CFL-09, the "download a
  zip, no git required" release/packaging pipeline needing a full
  redesign once real installable binaries replace it) — large enough
  that it should go through the same interview-before-implementing
  discipline as every prior phase, not be started opportunistically.
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to start Phase 7.

### 2026-08-06 — Phase 7a: Tauri shell + Python sidecar architecture (DEC-22)

- **Target**: Claude Code CLI, first non-Python code in this repo
  (`src-tauri/`), branch `feature/phase7-tauri-shell`, PR #12
  (https://github.com/jh3779/agent-handoff-bridge/pull/12), **merged**.
  User confirmed (explicit yes/no question, given Phase 7's size/
  irreversibility) to start Phase 7 despite the earlier "stop here"
  decision.
- **Changed**: A design interview resolved four architecture forks
  before any code was written (research:
  `docs/research-phase7-framework.md`) — **Tauri over Electron**
  (official first-class Python-sidecar support), **keep
  `handoff_webui.py` as a PyInstaller sidecar** rather than a Rust
  rewrite, **keep the existing `gh`-based `check_for_update()`** rather
  than either framework's own updater (neither cleanly supports a
  private repo), **carry `webui/` over near-verbatim** this phase
  (frontend framework deferred). Recorded as **DEC-22**, resolving
  CFL-14. Phase 7 broken into sub-phases (7a done; 7b cross-platform
  build/packaging; 7c code signing; 7d frontend framework, separate
  decision) rather than one giant PR.
  `src-tauri/`: a Tauri v2 project spawning `handoff_webui.py` (built
  via PyInstaller as `agent-handoff-bridge-server`) as a sidecar, plus
  three more sidecars following the real call chain
  (`agent-handoff-bridge-cli`/`handoff_bridge.py` for `init`/`run`,
  `agent-handoff-bridge-validate`/`scripts/validate_handoff.py` for
  `check`, `agent-handoff-bridge-scan`/`scripts/scan_secrets.py` for the
  secret scan). `scripts/build_phase7a_sidecars.py` is the real build
  script. Fixed four instances of a pre-existing pattern
  (`[sys.executable, script_path, ...]`) that breaks once frozen, since
  `sys.executable` becomes the frozen binary itself — each now detects
  frozen mode and invokes a sibling sidecar directly.
  Two real bugs found only by building and launching the actual `.app`:
  a statically-declared Tauri window navigates before the sidecar is
  ready (permanently blank window, no retry) — fixed by building the
  window programmatically only once the sidecar's stdout confirms
  readiness; CPython fully-buffers stdout once piped, so that readiness
  print could sit unflushed forever — `PYTHONUNBUFFERED=1` alone did
  *not* reliably fix this when re-tested against the real binary; the
  confirmed fix is `handoff_webui.py`'s own
  `sys.stdout.reconfigure(line_buffering=True)`.
- **Verified**: `python3 -m unittest discover -s tests -v` — 365 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. `cargo build` (src-tauri)
  clean, locally and in CI. Against the actual built `.app`: sidecar
  starts, a first chat message creates a real workspace via the CLI
  sidecar chain, `agent-handoff-bridge-cli check` passes clean, macOS
  registers the app as `Foreground` with a live WebKit renderer. Direct
  screenshot confirmation wasn't achieved in this dev environment
  (Accessibility-permission limits meant automated screenshot targeting
  kept hitting the wrong window — including once misdirecting a
  keystroke into an unrelated app, disclosed to the user at the time;
  further screenshot attempts were stopped after that). In its place,
  the app's log shows the *webview itself* (not `curl`) requesting `/`,
  `/app.css`, `/app.js`, `/api/update-check`, `/api/info` in sequence
  right after window creation — the real request pattern of a browser
  engine executing the frontend, near-conclusive without a screenshot.
  Three review rounds before merge (an independent self-review before
  opening the PR, then two real automated GitHub reviews), each finding
  real issues:
  - **Self-review** (5 findings): a sidecar dying before the readiness
    marker used to leave the app running with no window and no
    diagnostic trail (logging was debug-build-only) — logging is now
    always on, `tauri-plugin-dialog` added solely for a
    fatal-startup-error path. `tests/test_validate_handoff.py` (new,
    this script had no tests before) wasn't registered in
    `validate_handoff.py`'s tracked-file lists or `handoff_bridge.py`'s
    `INSTALL_FILES` — fixed. `docs/security-model.md` had no section on
    the new architecture — added one. No committed script captured the
    four PyInstaller builds — added
    `scripts/build_phase7a_sidecars.py`.
  - **Review round 1** (2 merge-blocking findings): `docs/quality-gates.md`/
    `docs/verification-playbook.md` unconditionally documented `check`
    as running the dev test suite — the frozen CLI's `check` silently
    skips that step (no dev checkout to test against when shipped).
    Both docs now document the exception explicitly, and the frozen
    build's own `PASS` line says so too. The new Rust code had zero CI
    coverage — added a `rust-build` job (`cargo build`, not a full
    bundle). This **failed on its first real CI run**: Tauri's own
    build script validates every `bundle.externalBin` path exists even
    for a plain `cargo build` — reproduced locally before fixing with
    placeholder sidecar files in CI. Also: `capabilities/default.json`'s
    `shell:allow-execute` grant, previously documented as merely
    "inert," was **actually removed** — verified empirically (rebuilt
    and relaunched the real `.app` with it gone; sidecar still spawns,
    window still renders).
  - **Review round 2** (0 findings, 1 optional cosmetic fix applied): a
    stray `</content>` tag artifact left at the end of
    `docs/research-phase7-framework.md` from the original file-write —
    removed.
  - **Separately, after review round 1's CI fix**: the new `rust-build`
    job then hung indefinitely (13+ minutes, confirmed via per-step
    GitHub API timestamps, never even reaching `cargo build`) on an
    interactive `apt-get`/`needrestart` service-restart prompt that
    never gets answered in a non-interactive CI shell — a real,
    separate CI infrastructure bug, not a code issue. Cancelled the
    stuck run rather than let it keep burning CI time, fixed with
    `DEBIAN_FRONTEND=noninteractive`/`NEEDRESTART_MODE=a` plus a
    10-minute `timeout-minutes` safety net, verified green afterward
    (1m3s).
- **Remaining**: **Phase 7b** (cross-platform build/packaging — real
  Windows/Linux PyInstaller builds, Tauri bundle config for installers,
  replacing `scripts/package_platforms.py`'s zip model, resolves
  CFL-09) and **Phase 7c** (code signing — macOS notarization + Windows
  signing, a new recurring cost/process this project has never had) are
  the only items left open, both explicitly out of 7a's scope. Two
  deferred-not-blocking follow-ups noted for before 7b/7c: sidecar
  process cleanup on app quit, and behavior if port 8787 is already in
  use — neither verified, both flagged by review round 1 as real but
  non-blocking for 7a's actual goal (prove the architecture works).
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to start Phase 7b.

### 2026-08-06 — Phase 7b M1: cross-platform sidecar builds

- **Target**: Claude Code CLI, build/CI work (`scripts/build_sidecars.py`,
  `.github/workflows/ci.yml`), branch
  `feature/phase7b-m1-cross-platform-sidecars`, PR #13
  (https://github.com/jh3779/agent-handoff-bridge/pull/13), **merged**.
  User confirmed to start 7b at M1 (of the plan recorded in
  `docs/design-system/roadmap.md`'s "7b 계획" after Phase 7a merged).
- **Changed**: Renamed/generalized `scripts/build_phase7a_sidecars.py` →
  `scripts/build_sidecars.py` to build on macOS/Windows/Linux instead of
  just macOS (Phase 7a's script was macOS-only, built by hand):
  `--add-data`'s separator now uses `os.pathsep` (PyInstaller's documented
  rule: `;` on Windows, `:` elsewhere) instead of a hardcoded `:`; new
  `rename_for_tauri()` automates producing `<name>-<target-triple>[.exe]`
  filenames (previously `cp` by hand per binary); new `--target-triple`
  flag, auto-detected via `rustc -vV`'s `host:` line when omitted.
  `handoff_bridge.py`'s `INSTALL_FILES` and `scripts/validate_handoff.py`'s
  `REQUIRED_FILES`/`PYTHON_FILES` updated to the new filename (3 sites).
  New `sidecar-build` CI job: a `macos-latest`/`windows-latest`/
  `ubuntu-latest` matrix, each running `scripts/build_sidecars.py` with an
  explicit per-OS `--target-triple` and uploading the four sidecars as
  artifacts — this project's first real CI execution on Windows. Two known
  pitfalls pre-empted rather than waited-for: `python` not `python3`
  (`actions/setup-python` doesn't reliably alias `python3` on Windows),
  and a `timeout-minutes: 10` safety net (reusing the fail-loudly lesson
  from `rust-build`'s Phase 7a apt-get/needrestart hang, even though this
  job has no apt-get step).
- **Verified**: `python3 -m unittest discover -s tests -v` — 365 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Local macOS: full sidecar build
  via both auto-detect and explicit `--target-triple` paths, functional
  CLI `init`/`check` round-trip, `cargo build` clean. CI: all 7 checks
  green, including `sidecar-build`'s first-ever real run on
  `windows-latest`/`ubuntu-latest`. One self-review round before opening
  the PR found no blocking issues (flagged zero test coverage of
  `detect_target_triple()`/`rename_for_tauri()` as a non-blocking gap —
  not fixed, noted below). One real automated GitHub review found two
  medium-risk, both verified against the actual diff and fixed in a
  follow-up commit before merge: (1) the CI matrix hardcodes each runner's
  target triple, but `rename_for_tauri()` never checks the PyInstaller
  output's actual OS/arch against it — if a GitHub runner label's
  underlying machine ever changes, CI would stay green while producing a
  wrongly-named artifact; fixed with a step comparing `runner.os`/
  `runner.arch` against each matrix leg's expected value, failing loudly
  on mismatch. (2) the "verify files exist" step only checked filenames,
  never executed anything, so a frozen import error or bad bundling could
  still pass; fixed with a smoke-test step running each triple-suffixed
  sidecar right after building (CLI with `--version`, the other three
  with `--help`) on all three OSes — confirmed working against the real
  local macOS build before pushing. Re-review after the fix returned risk
  하 (low), 0 findings.
- **Remaining**: `scripts/build_sidecars.py`'s `detect_target_triple()`
  (the `rustc -vV` auto-detect path) and `rename_for_tauri()` have zero
  unit-test coverage — flagged by the self-review as a legitimate,
  non-blocking gap (the CI job always passes `--target-triple` explicitly
  and never exercises auto-detect at all). The review bot's optional
  (non-blocking) suggestion — archive uploaded artifacts with `tar`/`zip`
  to preserve Unix executable permission bits, since raw
  `actions/upload-artifact` doesn't — is also still open. Per the 7b plan
  in `docs/design-system/roadmap.md`, remaining milestones are M3 (extend
  `rust-build` from compile-check to a real `cargo tauri build` producing
  actual installers per OS), M4 (`docs/release-process.md` rewrite,
  CFL-09), M5 (code signing, deferred to 7c per DEC-22), M6 (verify the
  two Phase 7a-deferred follow-ups: sidecar cleanup on app quit, port 8787
  conflict handling). M2 (target-triple automation) was substantially
  folded into M1's work already.
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to continue to 7b's next milestone.
