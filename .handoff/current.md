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

### 2026-08-06 — Phase 7b M3: real per-OS installer builds via cargo tauri build

- **Target**: Claude Code CLI, CI work (`.github/workflows/ci.yml`),
  branch `feature/phase7b-m3-installer-build`, PR #14
  (https://github.com/jh3779/agent-handoff-bridge/pull/14), **merged**.
  User asked to proceed with "다음 작업" (the next task) after M1 merged;
  M2 was already folded into M1, so this is M3 of the 7b plan
  (`docs/design-system/roadmap.md`'s "7b 계획").
- **Changed**: New `installer-build` CI job producing real, unsigned
  installers (`.dmg`+`.app` macOS, `.msi`+nsis `.exe` Windows,
  `.deb`+`.AppImage`+`.rpm` Linux) via `cargo tauri build`, using genuine
  sidecars from `scripts/build_sidecars.py` — unlike `rust-build`, which
  only compile-checks against placeholder (`touch`ed) sidecar files.
  Reuses `sidecar-build`'s 3-way matrix and runner OS/arch guard, then
  installs `tauri-cli` (pinned to `2.11.4`) and runs the real bundle,
  verifying per-OS output files exist (not just filenames) before
  uploading as artifacts. **Gated to `workflow_dispatch` (manual trigger)
  only** — confirmed with the user via `AskUserQuestion` before
  implementing, since GitHub bills private-repo Actions minutes at 10x
  for macOS runners and 2x for Windows, and a real bundle build is
  comparatively expensive; running it on every PR/push (like every other
  job in this file) was explicitly rejected. `validate`/`rust-build`/
  `sidecar-build` all got `if: github.event_name != 'workflow_dispatch'`
  so a manual trigger runs only `installer-build`, not the whole file.
  Unsigned only — code signing stays deferred to 7c per DEC-22.
- **Verified**: `python3 -m unittest discover -s tests -v` — 365 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean. Local macOS: ran the exact
  sequence CI runs (real sidecar build → `cargo tauri build`) and got a
  genuine `.app`/`.dmg`; the job's verification `find` commands were run
  against that real output and matched. CI: all existing jobs stayed
  green on push, and `installer-build` correctly showed "skipping" on
  both push and PR triggers, confirming the manual-only gate works.
  Two review rounds, both with real findings, both fixed and
  re-verified:
  - **Self-review** before opening the PR: an incorrectly-justified `rpm`
    apt package (Tauri's rpm bundler is pure Rust, needs no system
    `rpmbuild` — verified by reading Tauri's actual bundler source)
    replaced with the packages Tauri's own official CI example for real
    bundling actually installs (`patchelf`, `xdg-utils`); added
    `libfuse2` as a preemptive mitigation for a currently-open upstream
    `linuxdeploy`/AppImage bug on `ubuntu-latest`
    (tauri-apps/tauri#14796); pinned `tauri-cli` to the exact version
    used in local verification instead of floating on `^2`.
  - **External review round 1** (risk 중/medium): `libfuse2` had no
    fallback — if `ubuntu-latest` moves to a 24.04-based image, Ubuntu's
    time_t transition may rename it to `libfuse2t64`, failing the whole
    apt-get step before reaching packages that matter; split into its
    own step with a non-fatal `libfuse2t64` fallback. Also:
    `workflow_dispatch` was added workflow-wide but `validate`/
    `rust-build`/`sidecar-build` had no exclusion, so a manual run meant
    only to test `installer-build` would also re-run all three,
    undercutting the explicit cost-consciousness that motivated gating
    `installer-build` to manual-only — added the exclusion guard.
  - **External review round 2**: risk 하 (low), 0 findings.
- **Remaining**: the Windows leg (NSIS/WiX toolchain auto-download) and
  the Linux leg (AppImage/rpm bundling, including whether the
  `libfuse2`/`libfuse2t64` fallback actually resolves the known upstream
  issue) have never been run for real anywhere — neither can be tested
  on this dev machine (macOS only). First real test is whenever someone
  actually triggers `installer-build` via `workflow_dispatch`. Per the
  7b plan, remaining milestones: M4 (`docs/release-process.md` rewrite,
  CFL-09), M5 (code signing, deferred to 7c per DEC-22), M6 (verify the
  two Phase 7a-deferred follow-ups: sidecar cleanup on app quit, port
  8787 conflict handling).
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to continue to 7b's next milestone, or to actually
  trigger `installer-build` for its first real run.

### 2026-08-06 — Phase 7b M4: rewrite release-process.md for two packaging tracks (DEC-23)

- **Target**: Claude Code CLI, docs-only work (`docs/release-process.md`
  and cross-references), branch `feature/phase7b-m4-release-process`,
  PR #15 (https://github.com/jh3779/agent-handoff-bridge/pull/15),
  **merged**. User said "계속 진행해줘" (continue) after M3 merged — M4
  is the next item in the 7b plan (`docs/design-system/roadmap.md`'s
  "7b 계획").
- **Changed**: Before writing anything, asked the user (via
  `AskUserQuestion`) whether the new Tauri installer track should fully
  replace the old "zip, git-free" source distribution
  (`scripts/package_platforms.py`) or both should stay — user chose
  **keep both** (recommended). Recorded as **DEC-23**, resolving the
  previously-open **CFL-09**, in
  `docs/design-system/flutter-mapping.html`'s Decision Log (moved out of
  the Conflict List). `docs/release-process.md` fully rewritten:
  documents both tracks (source zip for terminal/CLI/scriptable use,
  unchanged; Tauri installers for desktop GUI use, built by manually
  triggering CI's `installer-build` job via `gh workflow run`/`gh run
  watch`/`gh run download`), a new step to keep
  `src-tauri/tauri.conf.json`'s version field manually in sync with
  `BRIDGE_VERSION`, and a `gh release create`/`gh release upload` step
  attaching one representative installer format per OS. Cross-reference
  updates in `docs/index.md`, `docs/cli-reference.md`, `docs/
  platform-setup.md` (pointers to the new track) and
  `docs/security-model.md` (new note: installers are currently unsigned,
  Gatekeeper/SmartScreen warnings expected, signing deferred to 7c).
  Several stale CFL-09-as-unresolved references elsewhere (a Phase 7
  kickoff section, a summary table row, `docs/
  research-phase7-framework.md`'s title) corrected to reflect the actual
  "keep both" outcome rather than the original assumption that the zip
  model would end entirely.
- **Verified**: `python3 -m unittest discover -s tests -v` — 365 tests
  passing. `python3 handoff_bridge.py check` passes.
  `python3 scripts/scan_secrets.py` clean.
  `docs/design-system/flutter-mapping.html`'s HTML tags verified balanced
  after the Decision Log/Conflict List edits. Three review rounds, all
  with real findings, all fixed and re-verified:
  - **Self-review**: caught a real ordering bug in the runbook — the
    first draft built desktop installers (`workflow_dispatch` against
    `main`) *before* committing/tagging/pushing the version bump, which
    would have silently shipped installers built from the *previous*
    version. Fixed by reordering (commit/tag/push first, then trigger
    against the newly-pushed tag) and correcting the release-asset paths
    to match `actions/upload-artifact@v4`'s real subdirectory-preserving
    behavior (the first draft assumed a flat artifact directory).
  - **Review round 1** (risk 중/medium): the run-lookup loop used
    `--limit 1` with no bound — if any unrelated manual
    `workflow_dispatch` run landed more recently, the target run would
    never appear in that 1-row window and the loop would hang forever.
    Widened to `--limit 20` sorted by `createdAt`, bounded to 30 attempts
    with a loud failure instead of an infinite wait. Also: the macOS
    installer description implied general support, but the real CI
    matrix (verified against `.github/workflows/ci.yml`) only has an
    `aarch64-apple-darwin` leg — no Intel Mac support at all, a gap left
    open since 7b M1's planning and never resolved — documented
    explicitly rather than implied. Low-severity fix applied too:
    `docs/design-system/README.md`'s "remaining unresolved" CFL list was
    stale beyond CFL-09 (still listed CFL-11/12/13/14 as open, though
    each was already resolved by DEC-13/17-19/22) — corrected against
    the real Conflict List table.
  - **Review round 2**: risk 하 (low), 0 findings. Applied one optional
    suggestion anyway: noted the Apple-Silicon-only limitation in
    `cli-reference.md`'s summary too, not just the fuller explanation in
    `release-process.md`.
- **Remaining**: the installer-track runbook (steps 5-7 of
  `docs/release-process.md`) has never been run end-to-end — no tagged
  release has ever shipped installer assets yet (`gh release list` is
  empty). This is flagged directly in the doc itself. The macOS
  Apple-Silicon-only gap (no `x86_64-apple-darwin` CI leg) also remains
  open — a real product-scope decision (build Intel too? universal
  binary? drop Intel deliberately?) that wasn't part of M4's docs-only
  scope. Per the 7b plan, remaining milestones: M5 (code signing,
  deferred to 7c per DEC-22), M6 (verify the two Phase 7a-deferred
  follow-ups: sidecar cleanup on app quit, port 8787 conflict handling).
- **Blocked**: none. No further action pending — awaiting user direction
  on whether/when to continue to 7b's next milestone (M5/M6), decide the
  Intel Mac question, or actually cut a first real release to exercise
  this runbook.

### 2026-08-06 — Phase 7b M6: sidecar lifecycle fixes

- **Target**: Claude Code CLI, `src-tauri/src/lib.rs` (Rust) +
  `handoff_webui.py` + docs. Branch
  `feature/phase7b-m6-sidecar-lifecycle`, PR #16
  (https://github.com/jh3779/agent-handoff-bridge/pull/16), **merged**.
  User said "다음으로 진행해줘" after M4 merged; M5 (code signing) is
  explicitly deferred to Phase 7c per DEC-22, so M6 (verify the two
  Phase 7a-deferred follow-ups: sidecar cleanup on quit, port 8787
  conflict handling) was next. Work paused once mid-flow at the user's
  request ("일단 여기까지 작업해주고 기록해줘") after a system-load
  concern came up during empirical testing (see the 2026-08-06 "resource
  load / other apps closing" investigation earlier in this log if
  present, or the conversation itself) — resumed and taken to merge
  across two further "진행해줘"-style instructions, working through
  review findings in explicit priority order each time per the user's
  "중요도 높은것을 기준으로 분할해서 작업 진행해줘" instruction.
- **Changed**: Both Phase 7a-deferred questions were investigated by
  actually building and running the real `.app`, not just reading code.
  - **Sidecar cleanup on app quit — found genuinely broken, fixed.**
    Discovered by accident: while preparing a port-conflict test,
    `lsof -i :8787` found a process already squatting on the port whose
    parent was `launchd` (PID 1) — proof a Tauri app from earlier
    testing had quit hours ago but its sidecar never died, just got
    orphaned. Root cause: `sidecar.spawn()`'s returned `CommandChild` was
    discarded (`_child`) — `tauri-plugin-shell`'s `CommandChild` has no
    Drop-triggered cleanup. Fixed: the child is now kept in Tauri managed
    state and killed on app exit — required two rounds of empirical
    correction to get the hook right: `RunEvent::ExitRequested` never
    fires on a real quit here (confirmed by logging every `RunEvent`
    during a real quit via `osascript -e 'tell application ... to quit'`,
    chosen because Apple Events don't need the Accessibility permissions
    this dev environment lacks) — moved to `RunEvent::Exit`. Then
    `CommandChild::kill()` (SIGKILL) alone only killed the outer PID,
    orphaning PyInstaller's re-exec'd inner process all over again.
  - **Extended after a self-review**: the real process tree during an
    in-flight provider run is 3-4 generations deep (sidecar bootloader →
    its re-exec'd interpreter → a *second* PyInstaller sidecar
    `agent-handoff-bridge-cli`, spawned mid-run → its own re-exec'd
    interpreter → the real `codex`/`claude`/`gemini` subprocess), not
    the 2-generation idle case that was directly tested — a single-hop
    `pkill -P` only reached the first generation. Fixed with
    `descendant_pids_unix()`, walking the whole tree via repeated
    `pgrep -P` before killing anything (a dead process's children can no
    longer be found by ppid).
  - **Extended again after external review**: hard-killing the whole
    tree unconditionally (including a live provider CLI mid-write) was
    flagged as a real data-safety risk. Fixed with graceful-then-force:
    `SIGTERM` (Unix) / non-forced `taskkill /T` (Windows) first, a 1.5s
    grace period, then `SIGKILL`/`-F` only for whatever's still alive.
  - **Port 8787 conflict — was already non-hanging, message improved,
    then properly structured.** `handoff_webui.py`'s
    `ThreadingHTTPServer(...)` has no try/except around the bind call, so
    a taken port produces an unhandled `OSError` traceback — reproduced
    for real by pre-binding the port and launching a second instance.
    The existing `fatal_startup_error()` dialog already caught this, just
    with a generic message. First fix matched raw OSError text
    (`"Address already in use"`) — external review pointed out this is
    POSIX-only and fragile across OS/locale. Properly fixed by having
    `handoff_webui.py` itself catch the bind `OSError` and print a stable
    marker (`PORT_CONFLICT_MARKER = "AHB_PORT_CONFLICT"`) before
    re-raising; Rust matches on that marker as the primary signal now
    (old free-text checks kept only as a defensive fallback). Verified
    directly against the unfrozen Python module (pre-bind port, run
    `handoff_webui.py`, confirm the marker prints) — no Tauri
    build/app-launch needed for this part.
  - `docs/security-model.md` updated twice to keep pace with the
    implementation (was still describing the earlier single-hop
    `pkill -P` after the code had already moved past it — caught by
    external review).
- **Verified**: `python3 -m unittest discover -s tests -v` — 365 tests
  passing throughout. `python3 handoff_bridge.py check`,
  `python3 scripts/scan_secrets.py` clean throughout. `cargo build
  --manifest-path src-tauri/Cargo.toml` (placeholder sidecars, matching
  CI's `rust-build` job) clean on every round, including CI's own
  `rust-build` job (Linux). CI: all checks green across all 3 commits;
  `installer-build` correctly stayed skipped on push/PR throughout.
  **Empirically verified on macOS** (real `.app` build → launch → quit,
  repeated, `ps`/`lsof` checked): the idle-server case for sidecar
  cleanup — no orphans, port freed; the port-conflict dialog showing the
  correct specific message, directly observed by the repo owner in a
  real native dialog. **NOT empirically re-verified**: the deeper
  tree-kill and graceful-then-force timing were added *after* the
  empirical GUI-testing rounds and verified only via compile checks, not
  a real build+launch+quit cycle — a deliberate tradeoff after a
  system-resource-load concern came up, agreed with the user, to rely on
  careful code review instead of repeated local Tauri rebuilds/app
  launches for this round.
  Two rounds of external review on the PR, both with real findings, both
  addressed:
  - **Round 1** (risk 중/medium): the two gaps above (deeper tree-kill,
    hard-kill-only data-safety risk) plus the stale `security-model.md`
    description — all fixed as described.
  - **Round 2** (risk 하/low, **0 required fixes**): one remaining
    optional finding, **left unfixed, accepted as known debt** — see
    below.
- **Remaining — known, accepted gap (non-blocking, explicitly flagged in
  the PR and here)**: Windows' graceful-then-force kill re-targets the
  same root PID for both the graceful attempt and the forced retry; if
  the graceful `taskkill /T` kills the root but leaves descendants alive,
  the second forced call may fail to find them by a now-dead root PID —
  the same "parent dies, ppid-based lookup breaks" problem the Unix path
  already solves via upfront `pgrep -P` discovery, just not yet ported to
  Windows. Deliberately not fixed this round: Windows-specific code in
  this codebase has **zero verification path** in this environment — no
  Windows machine, and CI's `rust-build` job is Linux-only, so
  `#[cfg(windows)]` code has never even been *compiled* anywhere, let
  alone run. A blind fix here risks introducing new, equally-unverified
  Windows bugs with no way to catch them. Also still open: the deeper
  tree-kill and graceful-timing logic (both platforms) has never been
  empirically re-verified live since the resource-load-driven pause: only
  compile-checked, not build-launch-quit tested. Windows/Linux runtime
  behavior remains completely untested anywhere for this whole feature.
- **Blocked**: none. No further action pending. **Natural next steps**:
  whenever real Windows access becomes available, port the upfront-PID-
  discovery pattern to the Windows kill path and empirically verify the
  whole sidecar-lifecycle fix (both platforms) with a real
  build→launch→quit cycle, including the in-flight-provider-run case
  this round's fixes specifically target but never got to test live.
  Otherwise, the remaining Phase 7 items are M5 (code signing, deferred
  to 7c per DEC-22) and 7c itself.

**2026-08-06, follow-up**: user explicitly said "코드 서명 일단은
제외해줘" (exclude code signing for now) — reaffirms DEC-22's existing
deferral, not a new decision. With M1/M3/M4/M6 merged and M2 folded into
M1, **Phase 7b is now substantively complete**; 7c (code signing:
macOS notarization + Windows Authenticode, new recurring cost — Apple
Developer Program $99/year+) stays explicitly parked, out of scope until
separately greenlit. No code/doc changes needed beyond this note — DEC-22
and the roadmap's Phase 7 summary table already correctly describe this
as a separate, ungated decision. Do not start 7c work without an explicit
go-ahead.

**2026-08-06, wrap-up**: user asked to "7b마무리 작업 진행해줘" (finish
up Phase 7b). Direct commit to `main` (docs-only, no PR — matches how
every other cross-cutting `.handoff`/roadmap bookkeeping commit in this
project's history has been handled). Found and fixed one real gap while
doing this: `docs/release-notes.md`'s `## Unreleased` section had a
detailed Phase 7a entry but **nothing at all** for Phase 7b's substantial
work (M1/M3/M4/M6) — added a matching entry. Also added inline "✅
완료" markers to all 6 items of the 7b plan in
`docs/design-system/roadmap.md`, a wrap-up summary, and updated the
sub-phase breakdown + Phase 7 status table (was still "🚧 진행 중", now
"✅ 7a·7b 완료 · 7c는 명시적으로 제외"). **Phase 7b is now formally
closed out** — nothing further pending on it. Three items explicitly
carried forward as known-open (not fixed, not forgotten — written down
in both `roadmap.md` and `release-notes.md`): (1) macOS Intel
(`x86_64-apple-darwin`) support was never actually decided, only flagged
as a gap; (2) the installer-build release track has never been exercised
by a real tagged release; (3) M6's deeper tree-kill/graceful-timing fixes
remain empirically unverified on real Windows/Linux. None are blocking
anything — they're the natural entry points whenever this project picks
Phase 7 back up (or starts 7c, if that gets a separate go-ahead).

**2026-08-06, Phase 7c finalized as "no" (DEC-24)**: user asked to check
code signing's real costs and, if any existed, follow the direction a
sibling project (`file-converter`, `/Users/jihun/Developer/file-converter`,
separate repo, same operator) took at the same fork. Confirmed real costs
(macOS Apple Developer Program $99/year for notarization; Windows OV/EV
cert, EV needing hardware-backed key storage post-June-2023) and directly
inspected `file-converter`'s own decision log and CI config — it hit the
identical fork and chose (its DEC-029) to ship both platforms unsigned,
investing in clear Gatekeeper/SmartScreen bypass docs instead; confirmed
this is actually implemented there, not just written down (zero
signing-related steps anywhere in its `.github/workflows/build.yml`).
Converted this project's Phase 7c from DEC-22's open-ended "separate
decision gate" into **DEC-24: a final "no"** for the current scale
(private repo, real userbase is the operator) -- not a postponement.
`docs/security-model.md` gained the same concrete bypass instructions
(macOS: control+click → Open; Windows: click "More info" → "Run
anyway"). `docs/design-system/roadmap.md`'s Phase 7c bullet and status
table, and `flutter-mapping.html`'s Decision Log, all updated to match.
**Phase 7 (the entire framework-migration effort, DEC-01) is now fully
closed out** — 7a/7b done, 7c decided against. Revisit only if the
userbase premise changes, not on a fixed timeline; no code changes, no
cost incurred.

### 2026-08-06 — v0.2.0: first real tagged release, cut end-to-end

- **Target**: Claude Code CLI, direct commits to `main` (no PR — matches
  how release-cutting has always worked per `docs/release-process.md`,
  not a feature branch). User said "이제 다운로드 링크 릴리스 진행해줘"
  (proceed with the download-link release now). **Codex explicitly not
  involved in this piece of work** (user's own words), so no handoff
  coordination needed here.
- **Changed**: This is the **first tagged release this repo has ever
  actually published on GitHub** — `git tag`/`gh release list` were both
  completely empty beforehand, even though `docs/release-notes.md`
  already had a historical "v0.1.0" section (written but apparently
  never actually cut as a real GitHub Release/tag on this remote).
  `BRIDGE_VERSION` bumped `0.1.0` → `0.2.0` (`handoff_bridge.py`,
  `src-tauri/tauri.conf.json`) — matches this project's own long-standing
  internal name for the entire chat-redesign + framework-migration body
  of work sitting in `## Unreleased` (Phases 1-7, all of it, referred to
  as "v0.2" throughout this project's history). Followed
  `docs/release-process.md` step by step for the actual execution:
  - **Step 3 (validation) caught a real bug immediately**: 
    `tests/test_handoff_bridge.py::test_a_newer_release_is_reported` had
    hardcoded a mock "newer" release tag as the literal string
    `"v0.2.0"` — which silently collided the instant `BRIDGE_VERSION`
    was actually bumped to that exact value, since the test then had no
    version left that was legitimately "newer." Fixed by deriving the
    mock tag relative to `BRIDGE_VERSION` (major + 1) instead of a
    hardcoded literal, so this can never recur regardless of what the
    real current version happens to be.
  - **Step 4 (build+sanity-check the zip) caught two more real gaps**,
    exactly the failure mode the doc's own step 4 anticipated ("a file
    used by `check` is missing from `COMMON_FILES`"): extracting the
    real built zip and running `check`/`install` standalone (no git
    repo, exactly as a real downloader would) found
    `scripts/build_sidecars.py` and `tests/test_validate_handoff.py`
    missing from `scripts/package_platforms.py`'s `COMMON_FILES` (both
    already required by `handoff_bridge.py`'s `INSTALL_FILES` /
    `validate_handoff.py`'s `REQUIRED_FILES`, added in earlier Phase 7
    work but never backfilled into the zip packaging list) — the first
    caused an outright `check` failure; the second wouldn't fail `check`
    but was still missing from what ships. Also found, by cross-checking
    `INSTALL_FILES` against `COMMON_FILES` programmatically rather than
    waiting to hit each gap one at a time: `.githooks/pre-commit`,
    `.githooks/pre-push`, and `scripts/install_git_hooks.sh` were in
    `INSTALL_FILES` (so `install` tries to copy them into a target
    workspace) but never bundled into the zip itself -- a real
    downloader running `install` from the extracted zip would have hit
    `FileNotFoundError`. All four added to `COMMON_FILES`; re-verified
    with a full extract → `check` → `install` cycle, including
    confirming the git hooks' executable permissions survive the
    zip round-trip.
  - **Steps 5-8 (commit/tag/push, trigger `installer-build`, publish)
    run for real for the first time**: tagged and pushed `v0.2.0`,
    triggered `installer-build` via `gh workflow run ci.yml --ref
    v0.2.0`. All three OS legs actually produced real installers;
    downloaded and attached all 8 assets to the GitHub Release (2 source
    zips, Windows `.exe`+`.msi`, macOS `.dmg`, Linux
    `.AppImage`+`.deb`+`.rpm`).
    [https://github.com/jh3779/agent-handoff-bridge/releases/tag/v0.2.0](https://github.com/jh3779/agent-handoff-bridge/releases/tag/v0.2.0)
    — first real, live download links this project has ever had.
  - **Real, first-time-only finding**: the Windows `installer-build` leg
    hit its 30-minute job timeout, but *after* the real build,
    verification, and artifact upload had already succeeded — the
    timeout landed during a post-job step (`Swatinem/rust-cache`'s cache
    save), so the job's `conclusion` reported `cancelled` even though
    the artifact was genuinely fine and fully downloadable. Verified via
    the GitHub API directly (artifact existed, correct size, not
    expired) before trusting it. Documented in
    `docs/release-process.md`'s Notes section so a future release
    doesn't mistake this for an actual build failure. Also noted: `gh
    workflow run` printed the new run's URL directly on the `gh` version
    used this time, making step 6's documented polling loop unnecessary
    in practice (kept in the doc anyway since this isn't guaranteed
    across `gh` versions).
  - **README.md's Download section**, previously deliberately left
    zip-only (no real release existed yet to link), now has real,
    live links to all v0.2.0 assets — styled after a sibling project's
    (`file-converter`) table + blockquote-warning format: a platform
    table for the desktop installers, explicit step-by-step
    Gatekeeper/SmartScreen bypass instructions (matching the same
    wording `docs/security-model.md` already uses per DEC-24), and the
    existing source-zip instructions kept as a second, clearly-labeled
    track underneath.
  - **README.md separately updated to acknowledge Gemini** as a full
    third provider (title, intro, "Current Local Status" section) — it
    had been added as a real provider back in Phase 5 but README's
    title/body text still only ever said "Codex/Claude," never
    mentioning Gemini at all. Fixed with accurate, non-overclaiming
    language about Gemini's real limitations (no free auth-status
    check, not yet in API-key mode).
  - **Korean translations added as separate files** (matching the
    existing `docs/ko-operator-guide.md` pattern — English stays the
    source of truth, Korean lives alongside it, not a replacement, per
    the user's explicit choice when asked): `README.ko.md`,
    `docs/release-notes.ko.md` (translated in full, both the huge
    v0.2.0 entry and the shorter v0.1.0/Initial sections -- the user
    initially scoped this to "just v0.2.0 in detail" given the sheer
    size of the full changelog, then explicitly expanded it to
    "전체 내용" (the whole thing) once the v0.2.0 section was done), and
    `docs/release-process.ko.md`. Cross-linked from README.md,
    `docs/index.md`, and a one-line pointer at the top of each English
    original.
- **Verified**: `python3 -m unittest discover -s tests -v` (365 tests,
  passing after the version-bump-collision fix), `python3
  handoff_bridge.py check`, `python3 scripts/scan_secrets.py` all clean
  at every step. The actual release itself is about as end-to-end
  verified as it gets short of a real user downloading it: real zip
  extracted and round-tripped through `check`+`install` standalone, real
  installers built by real CI runners on all three OSes, real GitHub
  Release with real attached assets, real download link format
  confirmed against `gh release view`.
- **Remaining**: none of this touched code (only version numbers,
  a test fixture, `COMMON_FILES`, and docs) beyond what release-cutting
  itself requires — no new feature work. The known, already-recorded
  gaps from Phase 7b/7c (Intel Mac unsupported, Windows sidecar-kill
  edge case, Windows/Linux runtime never tested in this dev environment)
  are unchanged by this release and still open. Whoever cuts the next
  release should read the new "real experience" notes added to
  `docs/release-process.md` first.
- **Blocked**: none. v0.2.0 is live with working download links. No
  further action pending.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-06

- **Task**: user installed a real `gemini` CLI locally and asked for
  real verification of this project's Gemini support — every prior
  Gemini test throughout Phase 5 (and the v0.2.0 release) used fake
  mock shell scripts, never an actual binary.
- **What changed**:
  - Ran the real, locally installed `gemini` binary (npm
    `@google/gemini-cli`, v0.54.0, unauthenticated) directly, and read
    its own bundled JS source
    (`/opt/homebrew/lib/node_modules/@google/gemini-cli/bundle/`) to
    trace exactly where JSON output is written. Found two real gaps
    between `handoff_bridge.py`'s assumptions and actual CLI behavior
    — both now fixed and covered by new tests, see
    `docs/research-gemini-cli.md`'s new "Real CLI Verification"
    section for the full writeup:
    1. A fatal-error JSON body (auth failure, cancellation,
       max-turns-exceeded, fatal tool error) is written to **stderr**,
       not stdout — `summarize_gemini()` only ever looked at stdout, so
       it silently missed every real fatal error's structured body
       (`classify_handoff()`'s generic `exit_code != 0` fallback still
       caught these as a handoff, just with a less specific reason
       string). Fixed: `summarize_gemini()` now takes `stderr` too and
       falls back to it only when stdout has nothing parseable.
    2. A real auth failure's `error.type` comes back as the generic
       `"Error"`, not `"AuthError"`/`"FatalAuthenticationError"` as
       `classify_handoff()`'s `auth` pattern assumed — the type-name
       match doesn't fire for this real, likely-common case (an
       unauthenticated `gemini`). Fixed: the `auth` pattern now also
       matches the real message text's `auth method` phrase.
  - `handoff_bridge.py`: `summarize_gemini()` signature and body
    updated (`stdout, stderr="", exit_code=0`, stdout tried first,
    stderr only as fallback); its one call site in `run_provider()`
    updated to pass both streams; `ERROR_PATTERNS`' `auth` regex
    extended.
  - `tests/test_handoff_bridge.py`: updated the existing
    `test_gemini_autherror_is_classified_as_auth_not_unknown` to route
    through stderr; added
    `test_gemini_real_cli_autherror_shape_is_classified_as_auth`
    (exact real captured JSON shape),
    `test_falls_back_to_stderr_when_stdout_has_nothing_parseable`,
    `test_prefers_stdout_over_stderr_when_both_are_present`, and a full
    end-to-end `GeminiIntegrationTests.test_unauthenticated_gemini_run_end_to_end`
    (fake binary shaped exactly like the real one: empty stdout, JSON
    error on stderr, exit 41).
  - `docs/research-gemini-cli.md`: new "Real CLI Verification
    (2026-08-06)" section at the top documenting both findings, the
    source-tracing method used, and what was and wasn't re-verified
    (the successful/authenticated response path was **not** exercised
    — no real Gemini credentials available in this environment; only
    `--help` output and source-reading confirmed it's still consistent).
- **Verified**: real unauthenticated `gemini` call before the fix
  reported `tool_failure: provider exited with code 41` in
  `.handoff/current.md`/state; the exact same real call after the fix
  correctly reports `auth: provider emitted a machine-readable error
  event` — confirmed live via
  `python3 handoff_bridge.py run gemini --execute ...` against the real
  binary in a scratch workspace (not just the unit/integration tests).
  Full suite: `python3 handoff_bridge.py check` → 369 tests, OK, PASS.
- **Remaining**: the successful/authenticated Gemini response path is
  still unverified against a real call (no credentials available here)
  — if Gemini API/OAuth credentials ever become available in this
  environment, worth a quick real run to confirm the `response`/`stats`
  shape and `--resume latest` actually round-trip as documented. (A
  real bug in this session's own "no other gaps found" claim was later
  found in review — see the next entry — so treat this kind of claim
  with appropriate skepticism until an independent review pass confirms
  it.)
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-06

- **Task**: user asked to switch the GitHub repo to public (a tester was
  blocked waiting for access), then asked for a full review of
  everything done in the session so far.
- **What changed (public-repo switch)**:
  - Audited for anything unsafe to expose: `scripts/scan_secrets.py`
    clean on the current tree; a full `git log --all -p` secret-pattern
    sweep across all 112 commits found only test fixtures (fake AWS-key
    strings used by the secret-scanner's own tests), no real secrets;
    no `.env`/credentials file ever committed.
  - `README.md`, `README.ko.md`, `docs/release-process.md`,
    `docs/release-process.ko.md`: removed/updated the "this repo is
    private, you need GitHub account access" wording (both languages).
  - Ran `gh repo edit jh3779/agent-handoff-bridge --visibility public
    --accept-visibility-change-consequences` — confirmed via
    `gh repo view` (`isPrivate: false`) and an anonymous `curl` against a
    real release asset URL (302 → 200, no auth wall).
  - No LICENSE file added, by explicit user choice — matches the
    sibling `file-converter` project's same no-LICENSE precedent.
  - Verified both Windows installer assets' integrity end-to-end: SHA256
    of the freshly-downloaded public release assets matches the original
    CI-build artifact byte-for-byte (`cmp` confirms identical, not just
    same hash); `.exe` parses as a structurally valid PE32 NSIS
    self-extracting installer (manually walked the PE/section-table
    headers, all offsets within file bounds); `.msi` parses as a valid
    OLE Compound File (WiX-built MSI, correct metadata). Confirmed the
    CI run that built them (`31079734450`) actually built from the exact
    commit `v0.2.0` tags. Could not test actual install/run behavior —
    no Windows environment available here.
- **What changed (review pass, on the prior Gemini-fix entry)**: a
  fresh review (via a sub-agent with no context from writing the
  original fix, then independently re-verified by direct reproduction)
  found one real, reproducible regression the original fix introduced,
  plus a few lower-severity gaps. All fixed in this same commit range:
  - **(High, confirmed via direct repro) False-positive auth
    misclassification**: the `auth method` phrase added to
    `ERROR_PATTERNS`'s `auth` pattern is also checked, unconditionally,
    against a *successful* run's raw combined stdout+stderr
    (`classify_handoff()`'s second, ungated loop) — a genuinely
    successful Gemini response that merely discusses "the auth method"
    in prose got wrongly classified as `auth: matched auth signal`,
    which would discard a good answer and (with `--auto-fallback`)
    burn tokens re-running the same prompt on a different provider.
    Fixed by matching the fuller, distinctive imperative phrase "set an
    auth method" (Gemini's own real wording) instead of the bare
    "auth method" -- confirmed the false-positive case now passes and
    the real auth-failure case still correctly classifies as `auth`.
    Added `test_successful_response_merely_mentioning_auth_method_is_not_misclassified`.
  - **(Low, confirmed via direct repro) `summarize_gemini()`'s
    stdout→stderr fallback gap**: only triggered on a stdout
    `JSONDecodeError`, not on "stdout parsed fine but wasn't a dict"
    (e.g. `"null"`, a bare array) — in that case it returned the empty
    summary immediately without ever trying stderr, contradicting the
    function's own documented fallback contract. Low real-world
    likelihood given the real CLI's confirmed mutually-exclusive
    stdout/stderr behavior, but fixed for correctness: rewrote as a
    loop trying `(stdout, stderr)` in order, keeping the first
    dict-shaped parse. Added
    `test_falls_back_to_stderr_when_stdout_parses_but_is_not_a_dict`.
  - **(Medium, doc consistency) Missed a second stale "private repo"
    line**: `docs/release-process.md`/`.ko.md` step 6's rationale for
    gating `installer-build` to manual trigger cited "GitHub bills
    private-repo Actions minutes at 10x/2x" — no longer true now that
    the repo is public. Reworded to note the original reason was
    billing (now moot) but the manual-trigger gate itself stays for
    now (build wall-clock time, not cost). Left the actual CI trigger
    behavior unchanged — that's a separate design call, not something
    this pass should decide unprompted.
  - **(Low, doc consistency) Stale rationale comments left as-is by
    the first pass**: `handoff_bridge.py`'s `GITHUB_REPO` comment and
    `docs/cli-reference.md`'s update-check description both justified
    using `gh`-CLI-based (rather than anonymous API) release checks by
    "this repo is private" — reworded to past tense ("written while
    this repo was still private") rather than silently changing the
    actual implementation, since switching to an anonymous API call is
    a real behavior change out of scope for a docs-consistency pass.
  - **Not changed, flagged only**: `docs/security-model.md`'s DEC-24
    (unsigned-installer decision) rationale explicitly cites "a private
    repo where the real userbase is the operator" as part of its
    cost/benefit call — a tester is a first step beyond that premise.
    DEC-24 already documents clear Gatekeeper/SmartScreen bypass
    instructions, so this isn't blocking, but revisiting DEC-24 itself
    is a decision for the user, not something to silently rewrite in a
    review pass.
- **Verified**: `python3 handoff_bridge.py check` → 371 tests (369 + 2
  new regression tests from this review), OK, PASS. Both the
  false-positive fix and the stderr-fallback fix were independently
  reproduced by hand (not just trusted from the review agent's report)
  before and after the fix to confirm the bug was real and the fix
  actually closes it.
- **Remaining**: DEC-24's premise re-check (above) is a user decision,
  not done here. Everything else raised by the review is fixed.
- **Blocked**: none.
