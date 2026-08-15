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

## Provider: claude / Model: claude-sonnet-5 — 2026-08-07

- **Task**: continuing from the prior checkpoint, asked the user directly
  whether DEC-24 (no code signing) should be revisited now that its
  stated trigger condition (real users beyond the operator, or growing
  distribution scale) is being approached by the 2026-08-06 tester +
  public-repo changes.
- **What changed**: user chose to keep the decision as-is (no signing).
  Updated the decision's paper trail to record the re-review rather than
  silently leaving it looking untouched since before the public-repo
  switch:
  - `docs/security-model.md`: DEC-24 section's rationale no longer calls
    the repo "private" (stale since the 2026-08-06 public switch) —
    reworded to "userbase is still the operator plus a small number of
    known testers" as the actual justification, repo visibility called
    out as a separate axis. Added a "Re-reviewed 2026-08-07" note:
    reaffirmed, not re-decided from scratch.
  - `docs/design-system/flutter-mapping.html` (project's DEC-1~24 source
    of truth log): appended the same re-review note to the DEC-24 row.
  - `.agent/DECISIONS.md` DEC-001 (gitignored, local-only session-handoff
    copy, not committed): status/date updated to match.
- **Not changed**: no code, tests, or release process — this was a
  documentation-only decision re-confirmation, no functional change.
- **Verified**: N/A (docs-only change, no test-affecting code touched).
- **Remaining**: none from this task. Still-open items from before:
  Gemini's authenticated/success response path remains unverified
  against a real call (no credentials available in this environment).
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-07 (full-project review)

- **Task**: user asked for a full-project code review (not just the diff),
  high effort, correctness/reuse/simplification/efficiency scope. Ran the
  `/code-review` skill's finder pipeline (6 angles: correctness A/B/C,
  reuse, simplification+efficiency, altitude+conventions) across the whole
  repo, then adversarially re-verified every finding with independent
  agents before trusting any of it (several earlier session entries in
  this file already show that a first-pass "no issues" claim can miss
  real regressions — treated this pass the same way).
- **Result**: 17 findings CONFIRMED after verification (report delivered
  to the user via ReportFindings, not duplicated here in full — see chat
  transcript / `docs/retrospectives/` if a write-up gets added later). 4
  were security-critical; user chose to fix those 4 immediately, deferred
  the remaining 13 (correctness races, Gemini-provider-missing-in-4-files,
  and efficiency/reuse items) to a later task.
- **What changed (the 4 security fixes)**:
  - `.github/workflows/ci.yml`: the `branch-name` job interpolated
    `${{ github.head_ref }}` directly into a `run:` shell block — GitHub's
    template expansion happens before bash parses the line, and git ref
    names legally permit `$()`, backticks, `;`, `|` (confirmed via
    `git check-ref-format`), so a malicious fork-PR branch name could run
    arbitrary shell on the CI runner. Fixed by routing it through an
    `env:` var first (`HEAD_REF: ${{ github.head_ref }}`, then
    `"$HEAD_REF"` in the script) — GitHub's own recommended mitigation for
    this exact class; env-var values aren't re-parsed as shell syntax the
    way inline `${{ }}` substitution is.
  - `remote_handoff_server.py`: two fixes.
    1. `load_or_create_token()` reused an existing-but-empty token file
       as-is; `authorized()` only guarded `token is None`, not `token ==
       ""`, so `secrets.compare_digest("", "")` let any request with no
       Authorization header through — a corrupted/truncated token file
       silently disabled auth on a server meant for non-local/remote use.
       Fixed: an empty existing token file is now treated as "no token
       yet" and a fresh one is generated, same as the missing-file case.
    2. `run_task()` built `handoff_bridge.py` subprocess argv by appending
       user-controlled `task["task"]`/`task["prompt"]` as bare positional
       arguments with no `--` separator. A prompt of literally `"--execute"`
       would be parsed by argparse as the real `--execute` flag instead of
       content — bypassing the server's own `--allow-execute` gate (which
       only checks the JSON `execute` field, never prompt content) and
       actually invoking the provider. `handoff_webui.py` already fixed
       this identical bug class elsewhere (`--` separator / `--prompt-file`)
       but it never propagated here. Fixed: added `--` before both
       `task["task"]` (in the `init` call) and `task["prompt"]` (in the
       `run` call).
  - `scripts/scan_secrets.py`: `--staged` mode's `scan_file()` read file
    contents off the working-tree disk, not the git index — reproduced
    end-to-end that staging a secret, then overwriting the working copy
    with clean text *without re-staging*, made `--staged` (and, sharing
    the same bug, `.githooks/pre-push`'s full-tree scan on the same
    working tree) report PASS while the secret was still committed via
    the index. Fixed: added `read_staged_text()` (`git show :path`, reads
    the actual staged blob) used only in `--staged` mode; full-tree mode
    (`git ls-files`) still reads disk, which is correct there since it's
    meant to represent current tracked content and is what a fresh CI
    checkout also sees.
  - `tests/test_scan_secrets.py`: added
    `test_scan_staged_only_reads_index_not_working_tree` (the exact
    stage-then-revert-on-disk repro above).
  - `tests/test_remote_handoff_server.py` (new file — no test file existed
    for this module before): `LoadOrCreateTokenTests` (empty file not
    reused vs. non-empty file reused as-is) and `RunTaskArgvSafetyTests`
    (asserts `--` always precedes `task["task"]`/`task["prompt"]` in the
    built argv, including the literal `"--execute"`-as-prompt case).
- **Verified**: `python3 handoff_bridge.py check` → 376 tests (371 + 5 new:
  1 scan_secrets regression + 4 remote_handoff_server), OK, PASS. Every
  one of the 4 fixed findings was independently re-verified by a separate
  agent (not just the finder that first reported it) via direct code
  reading and, for the scan_secrets/CI findings, live reproduction
  (`git check-ref-format`, an actual stage-then-revert repro, an actual
  rename+edit repro) before being trusted as real.
- **Remaining**: 13 lower/medium-severity findings from the same review
  deferred by explicit user choice — not silently dropped, just not done
  in this pass. Notable ones: `remote_handoff_server.py`'s
  `TimeoutExpired` bytes-vs-str bug (can hang a remote task at "running"
  forever), `handoff_bridge.py`'s `state.json` non-atomic read-modify-
  write race under concurrent remote tasks on the same workspace,
  `classify_handoff()`'s second `ERROR_PATTERNS` loop still misclassifying
  successful runs for patterns other than `auth` (only `auth` was
  narrowed in a prior session), and Gemini being excluded from
  `handoff_control.py`/`handoff_desktop.py`/`remote_handoff_server.py`/
  `remote_handoff_submit.py` (each hardcodes its own stale provider tuple
  instead of importing `handoff_bridge.PROVIDERS`). Full list of all 17
  (with file/line/failure-scenario) was reported to the user in-session.
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-07 (remaining 13 findings)

- **Task**: user said to continue; fixed all 13 remaining findings from the
  full-project review above (all 17 are now closed). Two required design
  judgment and were done directly (not delegated): the `state.json`
  concurrency race and the `classify_handoff()` false positive. The other
  11 were delegated to 5 parallel subagents grouped by file, then
  integration-verified.
- **Design-call fixes (done directly)**:
  - `handoff_bridge.py`: added `RUN_LOCK_FILE` (`.handoff/.run.lock`,
    distinct from the existing `WRITE_LOCK_FILE` on purpose — that one is
    held only for an instant per write, this one is held across an entire
    `run_command()` invocation, which can legitimately run for minutes).
    `run_command()` now holds it for its whole load_state()-...-save_state()
    cycle; a second concurrent `run` against the same workspace fails fast
    (exit 75, clear stderr message) after `RUN_LOCK_TIMEOUT_SECONDS` (3600s)
    instead of racing and silently losing whichever save happens first.
  - `handoff_bridge.py`: `classify_handoff()`'s false-positive class (a
    successful run's own answer text quoting an error-sounding phrase like
    "command not found" got wrongly classified as a handoff) was only ever
    patched for the `auth` pattern in a prior session. First tried gating
    the whole second `ERROR_PATTERNS` loop on `exit_code != 0` — rejected,
    because `test_rate_limit_signal_in_stdout` is a real, intentional case
    of `exit_code == 0` with a genuine plain-text signal that must still be
    caught. Fixed instead by cutting `parsed["final_text"]` (the provider's
    own extracted answer) out of the text those patterns scan — a
    substring-exclusion approach that fixes every pattern generally
    without needing per-pattern phrase-narrowing or breaking the real
    exit-0-with-signal case.
- **Delegated fixes (5 parallel subagents, then integration-verified)**:
  Gemini-provider propagation to `handoff_control.py`/`handoff_desktop.py`/
  `remote_handoff_submit.py` (+ `remote_handoff_server.py` done directly,
  see below) plus the fragile-argparse-positional fix (`--prompt-file`/
  `--model=value`, matching `handoff_webui.py`'s existing pattern) in the
  first two; `scan_secrets.py`'s generic-regex gaps (unquoted values,
  underscore-adjacent labels) and renamed-file staged-scan gap
  (`--diff-filter=ACMR`); `scripts/handoff_hook.py`'s unlocked-append race
  (now uses `handoff_bridge.WriteLock`/`atomic_write_text`, same lock file
  as `handoff_bridge.py`'s own `append_current()`); the `build_prompt()`
  double-call waste on auto-fallback (reordered so the auto-fallback branch
  never builds/writes a prompt that's about to be superseded); and in
  `handoff_webui.py`/`webui/app.js` — `read_credentials()` now only reads
  when a CLI is actually unavailable, `read_month_messages()` now tolerates
  a `FileNotFoundError` TOCTOU race against `archive_old_months()`, and
  `switchWorkspaceTo()`'s three independent fetches now run via
  `Promise.all` instead of sequentially (`boot()` was deliberately left
  untouched — its fetches are NOT independent).
  - `remote_handoff_server.py` (done directly, not delegated, since I'd
    just finished editing it for the earlier security fixes): imports
    `PROVIDERS`/derives `PRIMARY_PROVIDERS` from `handoff_bridge.PROVIDERS`
    now (closing its own Gemini gap); `TimeoutExpired.stdout`/`.stderr` now
    routed through `handoff_bridge.decode_timeout_output()` (was crashing
    `write_json()`'s `json.dumps()` on raw bytes, silently wedging a timed-
    out remote task at "running" forever); `write_json()`/`read_json()` now
    reuse `handoff_bridge.py`'s atomic-write + lock + corrupt-JSON-safe-read
    helpers instead of a bare `write_text()`/`read_text()`.
- **Problem encountered and resolved**: running 5 subagents in parallel
  against the same live working tree (no worktree isolation) meant they
  observed each other's concurrent file writes and — per this repo's own
  shared-workspace framing in CLAUDE.md — misinterpreted them as "another
  session's in-flight work," leading a couple of them to `git stash`
  defensively mid-task. This caused real churn (one agent's edits briefly
  vanished from the working tree, `HEAD` even advanced by one commit
  unexpectedly) but no actual data loss — every agent that hit this
  recovered its own content and verified it byte-identical before
  finishing. After all 5 returned, did a full independent audit rather
  than trusting their individual "tests pass" claims: `git stash list`
  (empty, confirmed clean), full `python3 -m unittest discover -s tests`
  (found and fixed one real bug in a new test's own mock — it didn't
  account for `build_prompt()`'s internal `git status --short` subprocess
  call — not a production bug), then `python3 handoff_bridge.py check`
  (found and fixed one real false positive: the new unquoted-secret regex
  matched `remote_handoff_server.py`'s own `token = self.server.token`
  line; the intended `.`-exclusion fix an agent described in its report
  had not actually landed in the file, only in its own retelling — fixed
  directly and locked in with a regression test). Lesson for next time:
  don't fan out multiple file-mutating subagents against a live working
  tree without `isolation: "worktree"` unless their file sets are known to
  be fully disjoint; here they overlapped by proximity (same repo, same
  moment) even though their assigned *files* didn't overlap.
- **Verified**: `python3 -m unittest discover -s tests` → 414 tests, OK.
  `python3 handoff_bridge.py check` → 414 tests + secret scan + doc-
  consistency check, PASS. Re-ran both from a clean `git status --short`
  (nothing stashed, nothing uncommitted left over) as the final gate, not
  trusting any individual subagent's self-reported result.
- **Remaining**: none from the original 17-finding review — all fixed.
  Gemini's authenticated/success response path (from an earlier session)
  remains unverified against a real call (no credentials available here).
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-12

- **Target**: Claude Code CLI on a real Windows machine (first time this
  project's dev test suite has ever been run there), branch
  `fix/instruction-type-validation`, no PR yet. User first asked to
  prepare a Windows test environment, then (in manual testing of the
  running app) reported general CLI-input concern: "아무 키나 혹은 값을
  입력하였을 때 그냥 저장하는 경우가 존재함" (there are cases where any
  key/value entered just gets silently saved), then asked to verify
  further and do supplementary work.
- **Environment note (important for whoever reads this next)**: this
  session's working tree started as a genuinely fresh `git clone` (its
  own `git reflog` has exactly one entry: the clone itself) — an earlier
  part of the *same conversation* had already investigated a Windows-prep
  scope, gotten user answers via AskUserQuestion, and (per the
  conversation's own compacted history) apparently made real edits
  (`scripts/dev_shell.ps1`, `tests/fake_provider.py`, WriteLock/POSIX-path/
  run_shell fixes, an instruction-type fix) — none of which exist in this
  actual clone. That earlier local state was not this repository's real,
  committed history; it appears to have been some other/stale workspace.
  Re-verified everything from scratch against the real repo before
  redoing anything, rather than trusting the earlier conversation's
  self-report. One specific earlier claim was **wrong and is retracted
  here**: a claimed Gemini/`PROVIDERS` gap in `handoff_control.py`/
  `handoff_desktop.py` does not exist in this repo -- both already
  correctly derive `PROVIDERS` from `handoff_bridge.PROVIDERS` (includes
  gemini). `scripts/dev_shell.ps1`/`tests/fake_provider.py` and the
  broader "make all 35 POSIX-shell-skipped tests run on Windows" effort
  described in that earlier conversation turn were **not** redone here —
  no reliable diff survived to redo them from, and this session scoped
  itself to what could be freshly, directly verified.
- **Changed (real, verified bug)**: `handoff_bridge.py`'s
  `--instruction-type` (on both `init` and `run`) had no `choices=`
  restriction at all -- unlike its sibling `--primary`/`provider`
  arguments, which already validate correctly. Reproduced directly:
  `python handoff_bridge.py init "task" --instruction-type
  totally-bogus-value` exited 0 and wrote the garbage string straight
  into `.handoff/current.md`'s "Instruction type:" line, the shared
  source-of-truth file both providers read, no warning.
  `handoff_desktop.py`'s GUI already restricts the same field to a fixed
  5-value set via a readonly Combobox -- the CLI was the one unvalidated
  path. Fix: new `INSTRUCTION_TYPES = ("new-task", "continue", "handoff",
  "review", "verify")` constant in `handoff_bridge.py`; both
  `--instruction-type` arguments now use `choices=INSTRUCTION_TYPES`;
  `handoff_desktop.py` now imports this constant directly from
  `handoff_bridge` instead of keeping its own separate literal (matches
  the existing `PROVIDERS`-import pattern already used there for the same
  no-drift reason). `docs/cli-reference.md` documents the valid set.
  Audited but confirmed **not** in scope: `--model`/`--target-model`
  (intentionally free text), `--primary`/`provider` (already validated),
  `handoff_control.py`'s `ask_provider()`/`ask_model()` (already correct),
  `handoff_webui.py`'s `/api/run` and `/api/chat` (both already validate
  `provider`/`role` against fixed sets server-side; `instruction_type` is
  always a hardcoded literal there, never user-controlled).
- **Changed (found during the Windows-verification pass itself, not from
  the CLI-input report)**: running the real dev suite on Windows for the
  first time surfaced further real, Windows-specific bugs, all fixed:
  1. `handoff_webui.py`'s `bridge_command_prefix()`, `handoff_bridge.py`'s
     `check()`, and `scripts/validate_handoff.py`'s `check_secrets()` all
     built a frozen sibling-sidecar path via `Path(sys.executable).resolve()`
     -- host-native `pathlib.Path`, not tied to the `sys.platform` these
     functions were already branching on for the `.exe` suffix. In
     production this never actually diverges (a real frozen build's
     `sys.executable` always matches the real host OS), but it made the
     unit tests for the *other* platform's frozen behavior fail whenever
     the suite ran on a real Windows host (the darwin-simulation tests
     used a POSIX-style mocked path that a native `WindowsPath` parses
     differently). Fixed by switching all three to explicit
     `PureWindowsPath`/`PurePosixPath` (selected by `sys.platform`,
     dropping the now-unnecessary `.resolve()`) -- genuinely more correct
     and host-independent, not just a test workaround. Updated the 3
     corresponding `test_frozen_on_windows_uses_the_exe_suffix` tests
     (`test_handoff_bridge.py`, `test_handoff_webui.py`,
     `test_validate_handoff.py`) to build their expected value the same
     way instead of a hand-typed forward-slash literal, and the 2
     `test_unfrozen_shells_out_to_sys_executable_and_the_script` tests'
     `.endswith(...)` checks to use an OS-native separator.
  2. `tests/test_handoff_webui.py::LiveServerTests`'s fixture files were
     written with plain `write_text(...)` (default newline translation),
     so on Windows they landed on disk as CRLF even though the test
     asserted plain `\n` -- `read_file_preview()` itself is correct
     (deliberately binary-mode, preserving real on-disk bytes for a file
     browser); the fixture write needed `newline=""` to pin exact bytes
     across hosts, not the production code.
  3. `tests/test_handoff_bridge.py::RunProviderAutoFallbackBuildPromptCountTests.setUp()`
     registered `addCleanup(os.chdir, orig)` *before*
     `addCleanup(tmp.cleanup)` -- LIFO order made `tmp.cleanup()` run
     first, deleting a directory that was still the process's cwd.
     Allowed on POSIX (invisible until now), a hard `PermissionError` on
     Windows. Fixed by swapping the registration order.
- **Verified**: reproduced the instruction-type bug directly before the
  fix (bad value: exit 0, garbage written) and the fix after (bad value:
  exit 2, clear error, nothing written; all 5 valid values: exit 0). New
  `tests/test_handoff_bridge.py::InstructionTypeArgparseTests` (3 tests).
  Full suite run directly on Windows via the real local Python 3.12.10
  install (`C:\Users\Admin\AppData\Local\Programs\Python\Python312`) and
  real Git (`C:\Program Files\Git`) -- neither is on this machine's
  default PATH; `python`/`git` resolve correctly through Git Bash, used
  for every command this session. `python -m unittest discover -s
  tests`: 417 tests, 0 failures, 0 errors, 35 skipped (all legitimately
  platform-gated: POSIX-shell-only fake-provider integration tests,
  symlink tests, POSIX-permission tests -- none newly skipped by this
  session's changes). Repeated 5x back-to-back with no flakiness (the
  fresh-clone anomaly note above means any earlier "the concurrency test
  is flaky" observation from this conversation's history is unverified
  against this real repo and should not be trusted either way).
  `python handoff_bridge.py check` -- PASS (tests + secret scan +
  failure-classification-sync all green). `python scripts/scan_secrets.py`
  -- clean. `python -m py_compile` clean on all 8 changed files.
- **Remaining**: this was a targeted CLI-input audit plus whatever the
  Windows-verification pass itself surfaced, not an exhaustive
  input-validation or Windows-portability sweep. Specifically NOT done
  this session (would need a fresh, explicit ask, given the earlier
  conversation's self-report about this work turned out to be
  unreliable): a `scripts/dev_shell.ps1`-style PATH-setup convenience
  script for this machine; making the 35 currently-skipped POSIX-shell
  tests runnable on Windows (would need a real cross-platform fake-
  provider-script harness, a nontrivial addition); `docs/platform-setup.md`
  updates for the Windows dev-test path. Still on branch
  `fix/instruction-type-validation`, uncommitted -- no commit/PR
  requested this session.
- **Blocked**: none.

**2026-08-12, same session, follow-up**: user asked (still in Korean,
after the above): "cli키 저장시에 자동 검증 해줘 키를 읽는것이 아닌 그
키값으로 관련된 에이전트를 호출하여 최소한의 확인 답변을 받을 수 있도록
해줘" (auto-validate on CLI-key save — not by reading the key, but by
calling the related agent/API with that key to get at least a minimal
confirmation reply). Scoped this to the API-key-mode connection panel
(`webui/index.html`'s Diagnose panel, `POST /api/provider-key`,
`handoff_webui.py`), the only "save a CLI key" surface in this project.
- **Changed**: `POST /api/provider-key` previously wrote any non-empty
  `key` string to `credentials.json` unconditionally, trusting its shape
  alone — a typo'd or revoked key was only discovered the next time the
  user actually tried to chat. New `validate_provider_api_key(provider,
  api_key, model)` in `handoff_webui.py`: one real, minimal, tool-free
  HTTP call to the provider's own API (Anthropic Messages / OpenAI
  Responses, deliberately no `tools`/`tool_choice` in the request body at
  all, small `API_KEY_VALIDATION_MAX_TOKENS = 16`) asking for a one-word
  reply — same `{"ok": True, "text": ...}` / `{"ok": False, "message":
  ...}` contract as `call_anthropic_messages_api()`/
  `call_openai_responses_api()` (message never contains the key, same
  invariant) but skips their tool-use turn loop entirely: no workspace,
  no reason to grant tool access just to check a key. The endpoint now
  calls this before `save_credential()` ever runs for a non-empty key; a
  failure (bad key, wrong model, network error) returns 400 with nothing
  written. On success the response gains `verified: true` and
  `confirmation: "<actual reply text>"`. This required making `model`
  a hard requirement whenever a non-empty key is saved (400 otherwise) —
  `API_KEY_MODE_DEFAULT_MODELS` is deliberately empty for both providers
  (DEC-13), so there was never a model to validate *or* actually chat
  with without one anyway; this closes that gap at save time instead of
  leaving it to surface later as a chat-log error. Key removal (empty
  `key`) skips validation entirely, unchanged. `webui/app.js`'s save
  handler now shows the real confirmation text in its success toast
  instead of an unconditional "저장되었습니다." Docs updated:
  `docs/webui-chat-storage.md`'s "Credentials & API-Key Mode" section
  (new paragraph + `model` field note), `docs/provider-extensibility.md`
  (new changelog bullet), `docs/release-notes.md`'s `## Unreleased`.
- **Verified**: new `ValidateProviderApiKeyTests` (5 tests: Claude
  success, Codex success, invalid-key error message never echoes the
  key, network error doesn't raise, empty reply still counts as ok; two
  of these also assert no `tools`/`tool_choice` is ever sent). Updated
  `ProviderApiLiveServerTests` (real `ThreadingHTTPServer`, `_http_post_json`
  mocked at the same seam `CallProviderApiTests` already uses) — fixed 3
  tests broken by the new validation call, added 2 new ones (key without
  model → 400 + not saved; key that fails validation → 400 + not saved).
  Full suite: `python -m unittest discover -s tests` → 424 tests (417 +
  7 new), 0 failures, 0 errors, skipped=35 (unchanged). `python
  handoff_bridge.py check` → PASS. `python scripts/scan_secrets.py` →
  clean. `python -m py_compile` clean on both changed `.py` files.
  `node --check webui/app.js` could not be run (no `node` on this
  machine) — reviewed the diff by hand instead; kept small and
  template-literal-only.
- **Remaining**: not independently verified against a real Anthropic/
  OpenAI account (no real API key available in this environment) — every
  test here mocks `_http_post_json`, the same seam this project's
  existing API-key-mode tests already rely on for the same reason (no
  real credentials in CI or this dev environment either). Whoever next
  has a real key should do one real save/verify round-trip through the
  actual running app before treating this as fully proven end-to-end.
- **Blocked**: none. Still on branch `fix/instruction-type-validation`,
  uncommitted — no commit/PR requested this session.

**2026-08-12, same session, follow-up 2**: user asked "업데이트 확인도
추가 확인해줘" (also additionally verify/check the update-check feature)
-- Phase 6's `check_for_update()`/`/api/update-check` badge. This
project's entire prior verification history for this feature is
macOS-based (per this file's Phase 6/CFL-18 entries); this machine
confirmed earlier this session has no `gh` CLI installed at all, which
made this a genuine, never-before-exercised real-environment case rather
than a re-check of already-proven ground.
- **What was verified (no code changes to the feature's own logic —
  extensive existing review/tests already cover it, see Phase 6/CFL-18
  entries above)**:
  1. Direct call: `python -c "import handoff_bridge as hb;
     print(hb.check_for_update())"` on this real, `gh`-less machine
     returned `{'status': 'unavailable', 'current_version': '0.2.0'}` in
     0.003s -- instant, no hang, no exception. Confirms `short_run()`'s
     `FileNotFoundError` → exit-127 handling actually fires for a truly
     absent binary, not just a mocked one.
  2. Full end-to-end smoke test: started the real `handoff_webui.py`
     server (`--no-browser`) against a scratch workspace, polled `GET
     /api/update-check` twice a few seconds apart. Both real HTTP
     responses: `{"checked": true, "status": "unavailable",
     "current_version": "0.2.0"}` -- the background thread completed and
     the read-order-safe handler served the real result, matching the
     documented contract exactly. Server log had zero errors/warnings.
  3. Read `webui/app.js`'s polling logic (`checkForUpdate()`/
     `scheduleUpdateCheckRetry()`) end-to-end and manually traced the
     `UPDATE_CHECK_MAX_POLLS` bound (attempt 0..9 = exactly 10 fetches,
     confirming the earlier documented off-by-one fix still holds) --
     no new issue found, matches the extensively-reviewed Phase 6 design.
  4. Reviewed existing test coverage
     (`tests/test_handoff_bridge.py::CheckForUpdateTests`,
     `tests/test_handoff_webui.py::CheckForUpdateInBackgroundTests`/
     `UpdateCheckLiveServerTests`, including a real-thread race test using
     a `threading.Event`) -- thorough, no gaps in the status-classification
     or race-condition logic itself.
- **Changed (the one real, small gap this pass found)**: `short_run()`'s
  own `FileNotFoundError` → exit-127 translation -- the exact mechanism
  that makes "gh not installed" degrade gracefully -- had no direct unit
  test; every existing test mocked `short_run` itself rather than
  exercising this specific branch with a genuinely nonexistent command.
  Added `ShortRunTimeoutTests::test_binary_not_found_returns_127_not_a_raised_exception`
  (calls `short_run(["definitely-not-a-real-binary-xyz"])` for real, no
  mocking) so this behavior — which this session just confirmed by hand
  on a real `gh`-less machine — has a permanent regression test that
  doesn't require a `gh`-less machine to re-verify in the future.
- **Verified**: `python -m unittest discover -s tests` → 425 tests (424 +
  1 new), 0 failures, skipped=35 (unchanged). `python handoff_bridge.py
  check` → PASS. `python scripts/scan_secrets.py` → clean.
- **Remaining**: none — this was a verification pass, not a feature
  change; the one gap found (missing direct `short_run()` test) is fixed.
  The "genuinely available update" (`status: "available"`) response path
  is well-covered by mocked tests but was not re-confirmed against a real
  newer GitHub release in this pass (would require `gh` installed and
  authenticated, plus a real newer tag existing — neither available in
  this environment).
- **Blocked**: none. Still on branch `fix/instruction-type-validation`,
  uncommitted — no commit/PR requested this session.

**2026-08-14, follow-up (after the commit above)**: user pasted a real
crash traceback from running the frozen Windows `.exe` build
(`[PYI-18892:ERROR]`), triggered by typing a plain test message
("테스트로 테스트테스트") and hitting execute:
`UnicodeEncodeError: 'cp949' codec can't encode character '\u2014' in
position 5985: illegal multibyte sequence`, raised inside
`subprocess.run`'s stdin write in `run_provider()`.
- **Root cause, confirmed empirically on this real machine**: this
  Windows machine's locale is Korean, `locale.getpreferredencoding(False)`
  is `cp949` (confirmed via `python -c "import locale;
  print(locale.getpreferredencoding(False))"`). Every `subprocess.run(...,
  text=True, ...)` call in this codebase omitted an explicit `encoding=`,
  so Python fell back to that locale codec for both directions (encoding
  `input=` for stdin, decoding stdout/stderr) instead of UTF-8. `prompt`
  (what actually gets written to the provider's stdin) folds in this
  project's own docs (`docs/shared-agent-contract.md`,
  `docs/verification-playbook.md`), which contain literal em dashes
  (U+2014) -- cp949 cannot represent that character, so **any** execute
  call crashes immediately, regardless of what the user actually typed;
  the user's own simple test message was never the trigger. Reproduced
  directly and minimally first (`subprocess.run(['cmd','/c','more'],
  input='hello — world', text=True, capture_output=True)` raises the
  identical `UnicodeEncodeError` on this machine), then reproduced against
  the real `run_provider()` code path itself (mocked provider command,
  same crash, then confirmed clean after the fix).
- **Changed**: audited every `subprocess.run`/`Popen` call across all
  production (non-test) `.py` files for the same gap and fixed all of
  them with explicit `encoding="utf-8"` (plus `errors="replace"` on
  capture-output calls, matching `decode_timeout_output()`'s existing
  never-crash-on-decode posture in this codebase -- kept strict on
  `scan_secrets.py`'s `read_staged_text()` specifically, since its
  `except UnicodeDecodeError` guard relies on strict decoding to detect
  "this file isn't UTF-8 text" and skip it, its actual intended purpose):
  `handoff_bridge.py` (`short_run()`, `run_provider()`'s main provider
  call -- the confirmed crash site), `handoff_webui.py` (the `init`
  subprocess in `create_workspace_for_first_message()`, the `run_shell`
  tool executor, the outer bridge subprocess in
  `_run_provider_via_bridge_locked()`), `remote_handoff_server.py`
  (`run_command()`), `scripts/validate_handoff.py` (`check_secrets()`),
  `scripts/scan_secrets.py` (`list_files()`, `read_staged_text()`),
  `scripts/check_branch_name.py` (`current_branch()`),
  `scripts/handoff_hook.py` (`repo_root()`), `handoff_desktop.py`
  (`run_command()`'s worker), `scripts/build_sidecars.py`
  (`detect_target_triple()`, low-risk/ASCII-only but fixed for
  consistency). Every fix carries a comment explaining the locale-default
  hazard, not just the `encoding=` addition, so a future edit doesn't
  quietly drop it.
- **Not changed**: `handoff_desktop.py`'s fix has no automated regression
  test -- `tests/test_handoff_desktop.py` deliberately never instantiates
  a real Tk widget tree (documented in its own module docstring; the
  fixed code lives inside a `worker()` closure launched via
  `threading.Thread`/`self.after()`, genuinely impractical to unit test
  headlessly), so this one relies on matching the same
  reviewed-everywhere-else pattern rather than its own test.
  `scripts/build_sidecars.py` similarly has no existing test file and
  none was added (dev/CI-only build script; `rustc -vV` output is
  effectively always pure ASCII, lowest-risk fix in the batch).
- **Verified**: 12 new regression tests across 7 test files, each
  asserting `encoding="utf-8"` (and `errors="replace"` where applicable)
  is actually passed to the mocked `subprocess.run` call -- plus one that
  runs a real (unmocked) subprocess with a real em dash in the input and
  asserts no exception propagates. Full suite: `python -m unittest
  discover -s tests` -> 437 tests (425 + 12), 0 failures, skipped=35
  (unchanged). `python handoff_bridge.py check` -> PASS. `python
  scripts/scan_secrets.py` -> clean. `python -m py_compile` clean on
  every changed file. The exact user-reported crash was reproduced
  end-to-end against the real `run_provider()` function (not just the
  isolated minimal repro) before the fix, and confirmed crash-free after,
  both on this real cp949-locale Windows machine.
- **Remaining**: the user's crash came from running a **frozen** `.exe`
  (PyInstaller bootloader, `[PYI-18892:ERROR]`) -- almost certainly the
  packaged v0.2.0 Windows installer. This fix is only in source on this
  branch; it does **not** retroactively fix any already-built `.exe`. The
  user needs either: run from source in the meantime (`python
  handoff_bridge.py ...`, this environment's real Python 3.12.10 at
  `C:\Users\Admin\AppData\Local\Programs\Python\Python312`), or a new
  sidecar/installer build once this fix is merged and released. Not done
  this pass: no new release was cut, no sidecar rebuild was triggered --
  out of scope unless asked.
- **Blocked**: none. Still on branch `fix/instruction-type-validation`,
  uncommitted -- no commit/PR requested yet for this follow-up.

**2026-08-14, follow-up 2 (before committing the above)**: user asked
(before committing the encoding fix) whether settings should be added
for "someone who just uses AI by entering an API key" -- clarified via
AskUserQuestion into a concrete ask: **extend API-key mode to support
Gemini too** (DEC-15 had left this as an explicitly open, separate
question when API-key mode first shipped in Phase 4 -- codex/claude
only).
- **Changed**: Researched Gemini's real `generateContent` REST API
  against Google's own current official docs before implementing (same
  discipline this project already applied to Anthropic/OpenAI) --
  `docs/research-api-key-mode.md`'s new "Gemini: generateContent API"
  section has the full findings and sources. New `call_gemini_api()`
  (`handoff_webui.py`) matches `call_anthropic_messages_api()`/
  `call_openai_responses_api()`'s exact contract (`{"ok"/"text"}` /
  `{"ok"/"message"}`) and full tool-use turn loop
  (`read_file`/`write_file`/`edit_file`/`run_shell`, same
  `MAX_TOOL_ITERATIONS` bound, same defensive-every-call-block posture),
  but genuinely translates rather than reuses the wire format: Gemini's
  `Content` objects are `{"role", "parts": [...]}`, not the shared
  `{"role", "content": "..."}` shape `build_api_message_history()`
  builds (new `_gemini_contents_from_messages()` helper; `"model"`, not
  `"assistant"`, is Gemini's role for a prior turn), and its function
  calling uses `functionCall`/`functionResponse` parts (result sent back
  with `role: "user"`, wrapping the shared `execute_tool_call()`'s
  plain-text return as `{"result": <text>}` since Gemini's schema
  requires an object there, unlike Anthropic's/OpenAI's bare-string tool
  results). New `gemini_tool_definitions()` renders the same
  `_TOOL_SPECS` list Anthropic/OpenAI already share into Gemini's
  `functionDeclarations` shape (one Tool object holding all four, not
  one Tool per function). Auth via the `x-goog-api-key` header, not the
  `?key=` query-string alternative the same docs also mention -- keeps
  the key out of any URL. `API_KEY_MODE_PROVIDERS` grew to `("codex",
  "claude", "gemini")` (kept as its own tuple, not an alias for
  `PROVIDERS`, so a future CLI provider still needs its own explicit
  decision). `validate_provider_api_key()` got a third branch (Gemini's
  error shape uses `error.status`, e.g. `"INVALID_ARGUMENT"`, not
  `error.type` the way Anthropic/OpenAI's do). `webui/app.js`'s
  `PROVIDER_LABEL["gemini"]` changed from `"Gemini CLI"` to `"Gemini"`
  (no longer CLI-only, so the old label read oddly in the connection
  panel's save/delete toasts) plus a stale comment fix. No frontend
  *logic* change was needed beyond that -- `renderProviderRow()` already
  read `api_key_mode_supported` generically from the backend.
- **Recorded as DEC-25** (`docs/design-system/flutter-mapping.html`'s
  Decision Log, with a forward-reference added to DEC-15's own row) --
  resolves the question DEC-15 explicitly left open. Docs updated:
  `docs/webui-chat-storage.md` ("Credentials & API-Key Mode" + "Tool
  loop" sections), `docs/provider-extensibility.md` (new "Gemini added
  as a third API-key-mode provider" bullet), `docs/research-api-key-mode.md`
  (new Gemini section + Sources subsection), `docs/design-system/roadmap.md`
  and `components.html` (both had stale "API-key mode is still
  codex/claude only" notes from Phase 5, corrected with a forward
  pointer to DEC-25 rather than silently rewritten), `docs/release-notes.md`'s
  `## Unreleased` (also backfilled a missing entry for the
  instruction-type-validation fix from earlier this session, found
  missing while touching this file).
- **Verified**: new tests across `CallProviderApiTests` (4: success,
  error-never-echoes-key, network-error, blocked-prompt),
  `AgenticLoopTests` (5: executes-then-returns-final-text,
  defensively-executes-every-call, max-iterations-bound,
  no-function-call-returns-first-call, no-id-doesn't-fabricate-one),
  `ValidateProviderApiKeyTests` (2), `RunProviderViaApiKeyTests` (2),
  `ToolDefinitionTests` (1 new + the "two vendor schemas" test widened
  to three), `ProviderApiLiveServerTests` (the old
  `test_gemini_is_rejected_here...` test -- now factually wrong --
  replaced with `test_gemini_key_can_be_saved_and_verified_too` +
  a validation-failure counterpart). Two pre-existing tests that
  asserted the *old* "gemini is rejected/unsupported" behavior as their
  premise were fixed to use a genuinely-unsupported provider name
  instead (`CredentialsTests::test_read_credentials_filters_unknown_provider`,
  `ProviderApiLiveServerTests::test_providers_list_reflects_cli_detection_and_key_state`'s
  `api_key_mode_supported` assertion flipped true). Full suite: `python
  -m unittest discover -s tests` -> 452 tests (437 + 15 new), 0
  failures, skipped=35 (unchanged). `python handoff_bridge.py check` ->
  PASS. `python scripts/scan_secrets.py` -> clean. `python -m py_compile`
  clean. HTML tag balance in `flutter-mapping.html` re-verified
  programmatically after the DEC-25 row edit (same check this project's
  history already used for that file).
- **Not verified against a real Gemini account**: same caveat as the
  original API-key-mode-verification feature earlier this session -- no
  real Gemini API key available in this environment; every test mocks
  `_http_post_json`. The request/response shapes themselves were
  confirmed against Google's current official docs (not assumed), but a
  real end-to-end round trip (save a real Gemini key, chat, have it call
  a tool) has not been exercised. Whoever next has a real Gemini API key
  should do one.
- **Blocked**: none. Still on branch `fix/instruction-type-validation`,
  uncommitted -- no commit/PR requested yet.

**2026-08-14, follow-up 3**: user asked for a code review of the whole
project, documented into `docs/`. Clarified via AskUserQuestion into "a
new document reviewing the entire current codebase for quality/structure/
risk" (not a session retrospective, not an architecture explainer).
- **Changed**: new `docs/codebase-review.md`. Wrote the sections covering
  `handoff_bridge.py`/`handoff_webui.py`/tests/quality-gates/docs-system
  directly (deep first-hand knowledge from this session's own work);
  delegated two parallel background Explore agents for the parts examined
  less closely this session (desktop/CLI controllers, remote HTTP
  client/server, build/packaging scripts; and separately, the full
  `webui/app.js` frontend + `src-tauri/` Rust shell), then independently
  verified their two most concrete new-bug claims by reading the exact
  cited lines myself before writing them into the doc as confirmed (not
  just relayed) findings:
  1. `remote_handoff_submit.py:93` -- `--auto-fallback` is declared
     `action="store_true", default=True`, so it's unconditionally `True`
     regardless of whether the flag is passed, with no
     `--no-auto-fallback` counterpart -- confirmed by reading the exact
     line; the server-side `normalize_task()` fully supports
     `auto_fallback: false` via the JSON API directly, so only this CLI
     client can never send it.
  2. `handoff_control.py:45-51,81-88` -- `initialize_task()`'s
     primary-provider prompt reuses `ask_provider()`, which validates
     against the full `PROVIDERS` tuple (includes `"auto"`), then passes
     the answer straight through as `--primary` -- confirmed
     `handoff_bridge.py init --primary`'s own `choices=` (line 1229) is
     `PROVIDERS` from `handoff_bridge.py` itself, which has no `"auto"`
     entry, so typing `auto` there produces a raw, confusing argparse
     error from the child subprocess. `run_once()` (same file) and
     `handoff_desktop.py` both already avoid this correctly.
  Neither bug was fixed in this pass -- this was a review/documentation
  task, not a fix task; both are listed in the doc's "Consolidated
  Findings" table as concrete, actionable, still-open items. Several
  other findings (Tauri sidecar-spawn `.expect()`/panic risk,
  `package_platforms.py`'s `COMMON_FILES` possibly omitting some test
  files, `docs/architecture.md` not mentioning the Web UI/Tauri shell at
  all, a stale "pip install pywebview" instruction shown even inside the
  Tauri app that can never act on it) came from the agents' reports and
  are recorded as "open, needs confirmation" rather than asserted as
  fully proven -- not independently re-verified line-by-line the way the
  two bugs above were, per this session's own standing practice of not
  trusting a single unverified pass. Linked from `docs/index.md`'s
  Operator Docs section, next to Architecture.
- **Verified**: `python scripts/scan_secrets.py` clean on the new file
  (pure prose, no real risk expected, checked anyway).
  `python handoff_bridge.py check` -- PASS, 452 tests unchanged (this was
  a docs-only addition, no test-suite changes). No code was changed in
  this follow-up.
- **Remaining**: the two independently-confirmed bugs (#2, #3 in the
  review doc's findings table) and the four "needs confirmation" items
  are all real candidates for a future fix pass, not yet actioned.
- **Blocked**: none. Still on branch `fix/instruction-type-validation`,
  uncommitted -- no commit/PR requested yet.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-14 (v0.3.0 release finished + PR #18→#19)

- **Task**: user asked to check the uploaded PR and prepare to work on it.
  Found PR #18 (a small README doc fix) plus, via `git fetch`, that a
  prior session (this repo's dual-CLI handoff, likely Codex) had already
  cut most of a v0.3.0 release directly on `main` (merged PR #17 first)
  and paused mid-flow per the PR #18 body's own handoff record.
- **PR #18 -> #19 (accidental-close incident)**: PR #18's branch
  (`docs/v0.3.0-release-followup`) failed the `branch-name` CI check --
  dots aren't allowed in the kebab-case description part
  (`scripts/check_branch_name.py`'s `BRANCH_PATTERN`). Fixed via GitHub's
  branch-rename API (`POST .../branches/<old>/rename`), expecting it to
  transparently retarget the open PR's head ref (that's the documented
  behavior). It did NOT: the rename silently auto-closed PR #18 instead
  (`gh pr reopen` then failed with "Could not open the pull request" --
  the old head ref genuinely no longer existed for GitHub to reopen
  against). No data was lost (the commit survived on the renamed branch),
  but recovery required opening a fresh PR (**#19**, same commit
  `44f5b688`, branch `docs/v0-3-0-release-followup`) and leaving a
  pointer comment on the closed #18. **Lesson for next time**: don't use
  the branch-rename API on a branch with an open PR as a way to fix a
  failing branch-name check -- delete-and-recreate under the right name
  (or open a fresh PR directly) is safer than trusting the rename to
  carry the PR forward.
- **v0.3.0 release, finished per `docs/release-process.md`'s runbook**
  (steps 1-5 were already done by the prior session; this session did
  6-8 plus the follow-up README update the runbook doesn't number):
  - Step 6: confirmed the tag's `installer-build` CI run (`31778457384`)
    had finished -- all three OS legs `completed`/`success` (was still
    `in_progress` when the prior session paused). Downloaded all three
    artifacts (`gh run download`, ~460MB total, ran long enough to need
    backgrounding + polling via `TaskOutput`).
  - Step 4 redux: the prior session's `dist/*.zip` files were stale
    (built Aug 6, for v0.2.0) -- rebuilt via
    `scripts/package_platforms.py` and re-ran the standalone sanity check
    (extract outside the repo, no git present, `--version` + `check` both
    pass -- 418 tests, since the zip includes `tests/`).
  - Step 7: published `v0.3.0` via `gh release create` with the 3
    "one-per-OS" installers (dmg/nsis-exe/AppImage) + both source zips,
    title `v0.3.0`, notes extracted from `docs/release-notes.md` via the
    documented `sed` range (verified the extraction looked right before
    trusting it) plus an added unsigned-installer note (short, pointing
    at `docs/security-model.md` -- matches what the README already says
    near its own download table, so no need to duplicate the full
    Gatekeeper/SmartScreen explanation here).
  - Found v0.2.0's actual release had all 8 installer formats attached
    (dmg/exe/msi/AppImage/deb/rpm/.app is not a separate asset), not just
    the runbook's documented "3 one-per-OS" minimum -- uploaded the
    remaining msi/deb/rpm via `gh release upload` to match precedent and
    keep `README.md`/`README.ko.md`'s existing links (which reference all
    of them) valid.
  - Step 8: verified via `gh release view` (all 8 assets listed) and a
    real anonymous `curl -sL -o /dev/null -w "%{http_code}"` against
    every one of the 8 download URLs -- all returned 200.
  - Follow-up (not in the runbook's numbered steps, but the prior
    session's PR #18/#19 body flagged it as still open):
    `README.md`/`README.ko.md`'s desktop-installer download table
    updated from hardcoded `v0.2.0` asset URLs to the real `v0.3.0` ones,
    re-verified all three (msi/deb/rpm) with the same curl check.
- **Verified**: `python3 handoff_bridge.py check` on `main` after the
  fast-forward pull from the prior session's work -> 452 tests, PASS
  (the 418 count above is the standalone extracted-zip run, a smaller
  but still passing count -- not independently root-caused which test
  file(s) `scripts/package_platforms.py`'s `COMMON_FILES` excludes vs.
  the full repo checkout, but both runs pass, which is what step 4
  actually requires). `python3 scripts/scan_secrets.py` clean on the
  README changes. All 8 release asset URLs return HTTP 200 anonymously
  (repo is public, no auth needed).
- **Remaining**: PR #19 (the README Gemini API-key-mode doc fix) is open,
  CI running as of this entry (branch-name already passed) -- not yet
  merged, left for the user to merge when ready. PR #18 stays closed with
  a pointer comment, not deleted (keeps the accidental-close incident
  visible in the repo's history rather than erasing it).
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-15 (structure audit + remediation)

- **Task**: user asked for a structure audit (`/structure-audit` skill --
  relationship/layering quality, not a bug hunt) of the whole repo, then
  asked to remediate the findings.
- **Audit result** (reported in-session, not duplicated in full here):
  well-separated points confirmed -- handoff_bridge.py has zero reverse
  imports from any consumer, handoff_webui.py's ~2000 lines of business
  logic never reference HTTP-handler primitives (HTTPStatus/self.send_*/
  wfile/rfile, grep-confirmed 0 hits), provider-symmetric functions
  (provider_command/summarize_X/call_X_api) exist for codex/claude/gemini
  everywhere providers are handled. Five violations found, all fixed
  (see the `refactor:` commit's own message for the full list): workspace
  path normalization duplicated in 4 files, a subprocess.run() wrapper
  duplicated in 4 files (missing short_run()'s FileNotFoundError->127
  normalization in 3 of them), load_state() missing the JSON-corruption
  handling its two peripheral counterparts already had, do_GET/do_POST's
  route-not-found fallback breaking the _send_json() contract every other
  endpoint follows, and remote_handoff_server.py's provider-list error
  messages having drifted stale relative to an earlier session's own fix.
  Two "판단 유보" (deferred, not code defects) items were reported but
  intentionally left alone -- handoff_webui.py's 2621-line single-file
  size (logical sections are real, physical module split is a real
  tradeoff against the project's file-centered/no-build-step philosophy,
  not something to force without a separate decision), and
  handoff_webui.py importing business logic from handoff_bridge.py while
  5 other consumers import only constants (may be a deliberate
  "webui is a more-trusted in-process consumer" design, can't tell from
  code alone).
- **What changed**: `handoff_bridge.py` (added `normalize_path()`,
  `default_state()`, extended `short_run()` with `cwd`/`timeout: float |
  None`), `handoff_control.py`, `handoff_desktop.py`, `handoff_webui.py`,
  `remote_handoff_server.py` (all migrated to the shared helpers where it
  didn't lose behavior; two subprocess call sites deliberately NOT
  migrated -- run_provider()'s stdin-`input=` call and run_shell's
  `shell=True` call -- documented in short_run()'s own docstring for why).
  `remote_handoff_server.py`'s `run_command()` shrank by roughly half.
- **Verified**: `python3 -m unittest discover -s tests` -> 462 tests, OK
  (many new regression tests added per fix; one of them, a new
  `normalize_task()` test, initially failed on macOS's `/tmp` ->
  `/private/tmp` symlink -- the test's own `allow_roots` fixture wasn't
  resolved the same way `normalize_task()` resolves the workspace path,
  fixed in the test, not the production code). `python3
  handoff_bridge.py check` -> PASS. Pushed as commit `3b01ea9`.
- **Remaining**: none from the audit's violations list. The two deferred
  items above are explicitly left for the user's own judgment, not
  forgotten.
- **Blocked**: none.

## Provider: claude / Model: claude-sonnet-5 — 2026-08-16 (webui module split, the two deferred items)

- **Task**: user asked to act on the two "판단 유보" (deferred-judgment)
  items from the structure audit above. Asked which direction for each;
  user chose the larger option both times: physically split
  `handoff_webui.py` into modules, AND make it import only constants from
  `handoff_bridge` (matching the other 5 consumers) rather than business
  logic too.
- **Import-consistency part** (done first, smaller): added 3 new
  `handoff_bridge.py` CLI subcommands (`check-update`, `next-provider
  <current>`, `resolve-auto-provider`) wrapping `check_for_update()`/
  `next_available_provider()`/`choose_auto_provider()`; `handoff_webui.py`
  now calls these via `short_run(bridge_command_prefix() + [...])`
  instead of importing and calling the functions in-process. Named real
  cost in the commit: trades a cheap in-process call for a subprocess
  spawn, and made one existing test newly sensitive to the real
  test-runner machine's installed CLIs (fixed by mocking the new wrapper
  function directly instead of the subprocess internals).
- **Module split** (the big one): extracted `handoff_webui.py` (2663
  lines before) into `webui_common.py` (subprocess boundary + shared
  utils), `webui_workspace.py` (file tree/preview, workspace validation,
  auto-workspace creation), `webui_chat_storage.py` (chat history +
  registry + history drawer), `webui_credentials.py` (API-key storage),
  `webui_api_key_mode.py` (CLI-less provider HTTP clients + tool loop,
  the biggest at ~950 lines), `webui_bridge_run.py` (dispatching a real
  run, CLI or API-key mode). `handoff_webui.py` itself shrank to 649
  lines -- HTTP routing layer, `AppState`/`Api`, process entry point.
  - Mechanical approach: precise `sed`/Python line-range extraction from
    the original file (not manual retyping) to avoid transcription
    errors, verified each new file's syntax immediately after.
  - **Real bugs the extraction itself introduced, all caught before
    commit**: `webui_bridge_run.py` missing `import threading` (NameError
    on module load); `webui_chat_storage.py` missing `from datetime
    import datetime` and `import sys` (the latter only surfaced on the
    error-handling path, `touch_registry()`'s failure branch); the
    `_TOOL_EXECUTORS`-adjacent `_tool_read_file` missing a `read_file_
    preview` import from `webui_workspace.py`; `webui_bridge_run.py`
    missing `_api_key_mode_error_record` from `webui_api_key_mode.py`.
    Found via `pip install pyflakes` + running it across all 7 files --
    faster and more reliable than chasing each one through test
    tracebacks individually. Ran pyflakes again after fixes: clean.
  - **The one subtle cross-module bug worth remembering**: `AUTO_WORKSPACE_
    BASE_DIR` moved to `webui_common.py`, and the three consumers
    (`webui_workspace.py`/`webui_chat_storage.py`/`webui_credentials.py`)
    initially did `from webui_common import AUTO_WORKSPACE_BASE_DIR` --
    a value-copy import, immune to any later `mock.patch` on ANY module's
    copy of the name, including the canonical one. This silently broke
    ~23 tests' isolation (they patch this to redirect file I/O away from
    the user's real `~/Documents/Agent Handoff Bridge/`) and, worse,
    actually **wrote real test-fixture directories and a fake
    credentials.json to that real path** before the fix -- caught by
    checking that folder directly after a test run, not by any test
    failure (the writes "succeeded"). Fixed by switching those three
    modules to qualified access (`import webui_common; webui_common.
    AUTO_WORKSPACE_BASE_DIR`) so a single patch on the canonical module
    affects every consumer, matching the single-file version's implicit
    "one patch affects everything" semantics the tests were written
    against. Cleaned up the real-folder pollution each time it recurred
    during iteration (`rm` the test-fixture-shaped entries + fake
    credentials/registry files, never touched anything with an older
    timestamp).
  - **Mock-target retargeting in tests/test_handoff_webui.py** (~370
    direct-call sites + ~150 mock.patch targets): the general rule that
    actually mattered -- a regular function/constant's patch target must
    be the module where the ACTUAL CALLING CODE does its lookup (often
    the importer, not the definer), while a *stdlib module* patch
    (`subprocess.run`, `shutil.which`, `urllib.request.urlopen`) can
    target ANY module that did a plain `import` of it, since stdlib
    modules are singletons shared process-wide -- mutating the attribute
    via any reference affects all of them. Used the second fact to
    resolve `subprocess`/`shutil`/`urllib` mocks by picking whichever new
    module was semantically closest to what each test class actually
    exercises, without needing to trace exact call chains for those.
  - **Known pre-existing (not newly introduced) test-isolation gap**: a
    `registry.json` (and occasionally a `credentials.json`) kept
    reappearing in the real `~/Documents/Agent Handoff Bridge/` after full
    -suite runs even after the `AUTO_WORKSPACE_BASE_DIR` fix above, from
    some test/thread interaction not fully root-caused (every
    individually-inspected test class's `AUTO_WORKSPACE_BASE_DIR`
    patching looked correct). Cleaned up after every run this session;
    left as a known flake for a future session to actually chase, not
    silently ignored.
  - **Packaging manifests updated to match** (would otherwise ship a
    broken `handoff_webui.py` with missing sibling modules): added all 6
    new files to `handoff_bridge.py`'s `INSTALL_FILES` and `scripts/
    package_platforms.py`'s `COMMON_FILES`. Verified for real, not just
    by inspection: rebuilt the source zip, extracted it standalone (no
    git repo), confirmed `import handoff_webui` and `handoff_bridge.py
    check` (429 tests, the zip's own bundled subset) both pass. PyInstaller
    bundling (`agent-handoff-bridge-server` sidecar) was NOT verified end-
    to-end this session (no PyInstaller toolchain run) -- static analysis
    should auto-bundle plain top-level `import webui_X` statements the
    same way it already does for every other local import in this
    project, but this is asserted from the existing pattern, not
    freshly confirmed against a real frozen build.
  - Docs updated for the new file layout: `docs/architecture.md` (added a
    File Roles entry for the 6 new modules, fixed 2 stale function
    citations), `docs/quality-gates.md` and `docs/webui-chat-storage.md`
    (fixed stale `handoff_webui.py` citations for functions/constants that
    moved), `docs/cli-reference.md` (2 stale citations fixed).
- **Verified**: `python3 -m pyflakes` clean across all 7 touched .py
  files. `python3 -m unittest discover -s tests` -> 466 tests, OK.
  `python3 handoff_bridge.py check` -> PASS (secret scan + doc
  consistency too). Standalone extracted-zip check also passes (429
  tests, the zip's smaller bundled subset).
- **Remaining**: the pre-existing test-isolation flake noted above
  (occasional stray `~/Documents/Agent Handoff Bridge/registry.json`
  after a full suite run) is not root-caused, just cleaned up each time
  it appeared. Real PyInstaller sidecar build not exercised this session
  (would need the full Tauri/PyInstaller toolchain, not available here)
  -- worth a real `scripts/build_sidecars.py` run before the next release
  cut, to confirm the new local-module imports bundle cleanly into the
  frozen `agent-handoff-bridge-server` binary the same way the existing
  ones already do.
- **Blocked**: none.
