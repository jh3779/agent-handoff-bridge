# Release Notes

*([한글 번역](release-notes.ko.md) available.)*

## Unreleased

## v0.4.8 — 2026-09-03

- **New: multi-session tabs.** Open multiple chats at once via a new tab
  bar — a fresh tab for a different workspace, or another tab pointed at
  the *same* workspace for a second independent conversation (e.g. one
  provider refactoring while another writes tests). Each tab keeps its
  own workspace, chat history, and provider/model selection. A run
  keeps going in the background while you're on a different tab (shown
  as a busy indicator on its tab, with a badge once it finishes). Open
  tabs are restored the next time the app starts. **Known limitation**:
  two tabs on the *same* workspace can't run a provider call at the
  literal same instant — whichever one arrives second is turned away
  with an error rather than queued, so send from a busy tab's sibling
  only after the first reply lands. Two tabs on *different* workspaces
  have no such limit and genuinely run at the same time.

## v0.4.7 — 2026-09-03

- **New: model selection for CLI-detected providers** (codex/claude/
  gemini). Previously only API-key-mode providers could specify a
  model -- a CLI-detected provider's row in Settings now has its own
  model field (no API key needed, saved via a new
  `POST /api/cli-model`), used as the default `--model` on every send.
  A model field next to the titlebar's provider selector lets you
  override it per message.

## v0.4.6 — 2026-09-03

- **Fix: connected AI models (codex/claude/gemini) showed as not
  detected in the macOS desktop app**, even when installed and working
  fine from Terminal. A macOS app launched from Finder/Dock/Spotlight
  inherits launchd's minimal PATH, not the user's shell PATH -- Homebrew/
  nvm/etc. install locations for these CLIs live outside that. The app
  now asks the user's login shell for its real PATH once at startup and
  merges in whatever's missing (macOS-only, no-op everywhere else).
- **New: the Settings panel now shows the running app version** (a
  read-only "버전" row in the "일반" section), backed by a new
  `"version"` field on `GET /api/info`.

## v0.4.5 — 2026-09-03

- **Fix: the native folder picker was completely broken in every release
  since it was introduced (v0.4.3).** Clicking Open Folder (or "폴더
  직접 선택…") always failed with `Command plugin:dialog|open not
  allowed by ACL`. Root cause: the app's window loads
  `http://127.0.0.1:8787/` (the Python server's content) via
  `WebviewUrl::External`, not Tauri's bundled `app://` frontend --
  Tauri v2 only auto-applies a capability to *local* content, so an
  externally-loaded webview needs its origin explicitly allow-listed
  via the capability's `remote.urls` field, or every command it
  invokes is ACL-rejected regardless of what permissions are granted.
  `src-tauri/capabilities/default.json` never had this field. Fixed by
  adding `"remote": {"urls": ["http://127.0.0.1:8787/*"]}`.
- **New: Korean/English UI language toggle.** A language selector in
  Settings (기본값 한국어, `localStorage`-persisted) now switches every
  static and dynamic string in the app chrome -- titlebar, sidebar,
  composer, history drawer, Settings panel, toasts -- via a new
  `webui/i18n.js` module. Scope is UI chrome only; chat message
  content, provider/model/file names stay untranslated.
- Renamed the "공용 Context" Settings section to "지침" to match how
  it's actually used (still backed by the same
  `.handoff/shared-context.md`, no behavior change).

## v0.4.4 — 2026-09-02

- **New: a consolidated ⚙️ Settings panel**, replacing the separate
  Diagnose and Context titlebar buttons (same content, moved inside as
  the "연결된 AI 모델" and "공용 Context" sections) plus two new items:
  - **Auto-fallback toggle** -- was always on with no way to turn it off
    from the Web UI; now controllable per-session, threaded through
    `POST /api/run`'s `auto_fallback` field into
    `webui_bridge_run.py`'s CLI-mode dispatch.
  - **Theme switch** (시스템 설정/라이트/다크) -- previously only
    followed the OS setting with no manual override; persisted in
    `localStorage`, applied before first paint to avoid a flash of the
    wrong theme.

## v0.4.3 — 2026-09-02

- **Fix: re-opening the app while it was already running (minimized or
  just closed, not quit) showed a confusing "port already in use, quit
  and try again" error.** Closing the window doesn't quit the app on
  macOS (standard convention) -- a second launch attempt now focuses the
  already-running window instead of trying to spawn a competing sidecar.
- **New: a real native folder picker in the desktop app.** Clicking Open
  Folder (or "폴더 직접 선택…" on the empty-workspace card) now opens the
  OS's own folder-choose dialog on the Tauri build, instead of falling
  through to the manual path-typing prompt (that pywebview-only
  shortcut, `window.pywebview.api.pick_folder`, never existed inside the
  Tauri webview, only the separate non-Tauri native-window mode). Added
  `dialog:allow-open` (only `allow-open`, not the broader `dialog:default`)
  plus `app.withGlobalTauri: true` so the frontend can call
  `window.__TAURI__.dialog.open(...)` directly with no npm bundler.
- **Fix: the empty-workspace card looked like a tiny box floating in a
  mostly-empty window on large screens.** A side effect of the earlier
  fullscreen-viewport-scaling fix: the outer app shell grew with the
  window, but the empty-state card stayed a fixed 320px wide. It (and
  the empty-chat-history state) now scale with the viewport via
  `clamp()`, up to a readable cap.
- Removed the "Phase 6 · 자동 업데이트 확인" titlebar badge -- a
  development-progress label with no ongoing use to a real user.

## v0.4.2 — 2026-09-02

- **Fix: the macOS desktop app's local server crashed on every launch**
  (`Failed to load Python shared library ... mapping process and mapped
  file (non-platform) have different Team IDs`, exit code 255). Root
  cause: CI builds the sidecars with `actions/setup-python`'s macOS
  Python 3.11, whose `Python.framework` is signed and notarized by the
  Python Software Foundation with a real Team ID; the PyInstaller onefile
  bootloader extracts and `dlopen()`s that framework at runtime from a
  sidecar executable that -- since v0.4.1's ad-hoc `signingIdentity` fix
  -- carries no Team ID at all, and macOS's Library Validation refuses to
  load a dylib whose Team ID doesn't match the loading process's. This
  was invisible in v0.4.1 because Gatekeeper's "damaged" bug (fixed in
  that same release) blocked the app before the sidecar ever ran far
  enough to hit it -- v0.4.1 traded one launch blocker for a deeper one.
  Fixed with a new `src-tauri/entitlements.plist` granting
  `com.apple.security.cs.disable-library-validation`, applied to every
  sidecar individually (confirmed by reading `tauri-bundler`'s own
  signing code, not assumed) via `bundle.macOS.entitlements`. Verified
  locally, not just reasoned about: reproduced the exact "different Team
  IDs" failure with a local PyInstaller build, then confirmed this
  entitlement made the identical binary load successfully; also built a
  full local `.app`/`.dmg` with real sidecars and confirmed the
  entitlement reaches the actual bundled `agent-handoff-bridge-server`
  binary and it runs without crashing.
- **New: a portable Agent Skill, installed automatically.**
  `.agents/skills/handoff-status/SKILL.md` teaches whichever CLI is
  running (Codex, Claude Code, or Gemini CLI all discover this directory
  natively via the open `agentskills.io` `SKILL.md` standard) to check
  `.handoff/current.md` and run the free `status`/`diagnose` commands
  before starting or continuing work. Installed by `handoff_bridge.py
  install` like every other shared file. See
  [Architecture § Portable Agent Skill](architecture.md#portable-agent-skill-agentsskills).
- **Fix: custom providers (DEC-26) were completely unreachable from the
  chat UI.** `POST /api/run` rejected any `"custom:<name>"` provider
  value with a 400 "invalid provider" before ever reaching the
  already-correct dispatch logic that routes it to API-key mode -- the
  provider-select dropdown lists custom providers using exactly that
  value, so selecting one and sending a message always failed. Found
  while explaining how provider selection works, not from a dedicated
  audit pass.
- **Follow-up from the 2026-09-02 production audit** (see
  `docs/production-audit-2026-09-02.md`), all 6 audit-numbered findings
  addressed:
  - **F2 (continuity gap)**: API-key mode now appends a record to
    `.handoff/current.md` after every turn, success or failure --
    previously it never touched this project's actual cross-provider
    continuity document at all, even though its tool loop (CFL-17/DEC-21)
    can write/edit files and run shell commands. See
    [Architecture § State Boundaries](architecture.md#state-boundaries).
  - **F1 (partial)**: `run_shell`'s timeout now kills the whole process
    group a command spawned, not just the immediate shell -- a
    backgrounded/forked descendant no longer keeps running past
    `TOOL_EXEC_TIMEOUT_SECONDS`. The per-tool-confirmation/mode-boundary
    UX half of this finding is a deliberate product decision (DEC-21
    already chose against it once) and was intentionally left
    unchanged, not silently decided here.
  - **F3**: source zips (`scripts/package_platforms.py`) now include
    `README.ko.md` and all of `docs/design-system/` -- both were linked
    from README.md/docs/index.md but missing from the packaged zip.
  - **F4**: `remote_handoff_submit.py` gained `--no-auto-fallback`; the
    flag previously had no way to be turned off.
  - **F5**: `remote_handoff_server.py`'s `--task-timeout` now defaults to
    1800s instead of 0 (unlimited), and its request body gets the same
    2 MB cap the Web UI's own handler already enforces.
  - **F6**: `handoff_control.py`'s interactive task creation no longer
    lets "auto" reach `init --primary` (which has never accepted it) --
    a new `ask_primary_provider()` restricted to real providers replaces
    `ask_provider()` there.
  - **F7**: `scripts/handoff_hook.py`'s `write_next_prompt()` now uses the
    same `atomic_write_text()` pattern `append_current()` already does,
    instead of a plain `write_text()`.
  - **F8**: `src-tauri/Cargo.toml`'s scaffold-default metadata
    (description/authors/license/repository/version) filled in; the
    crate `name` deliberately stays `"app"` (the actual shipped
    executable filename -- see the comment in that file for why renaming
    it is out of scope for a metadata-hygiene pass).

## v0.4.1 — 2026-08-28

- **Fix: macOS installer reported as "damaged" and refused to open.** The
  `.app` bundle shipped in v0.4.0's `.dmg` had an inconsistent code
  signature — the main executable carried the ad-hoc signature the Rust
  linker adds automatically, but the bundle itself was never re-signed
  as a whole, so it had no `_CodeSignature/CodeResources` seal covering
  `Info.plist`/`Resources`/the bundled sidecar binaries. Gatekeeper
  treats that mismatch as a tampered/invalid bundle and shows "\<app\> is
  damaged and can't be opened" — a stronger, non-bypassable error,
  distinct from (and not fixed by) the documented "unidentified
  developer" control+click workaround. Fixed by setting
  `bundle.macOS.signingIdentity: "-"` in `src-tauri/tauri.conf.json` so
  `cargo tauri build` ad-hoc-signs the whole assembled `.app` (not just
  the executable) before packaging it into the `.dmg`. Windows/Linux
  installers were unaffected (this is Apple's code-signing model only).
  Still unsigned at the OS level per DEC-24 — the "unidentified
  developer" Gatekeeper warning (with the control+click workaround) is
  still expected and normal; this fix only removes the false "damaged"
  error.

## v0.4.0 — 2026-08-19

- API-key mode: **custom providers** (DEC-26) — register any number of
  arbitrary OpenAI-compatible (Chat Completions -- OpenRouter, Groq,
  Together, a local Ollama/LM Studio server, etc.) or Anthropic-compatible
  HTTP endpoints under a name you choose, for buying API tokens directly
  rather than installing a vendor CLI. Same tool-use turn loop (read/write/
  edit files, run shell commands) as codex/claude/gemini's API-key mode.
  Manage them from the connection panel (Diagnose button); `POST
  /api/custom-provider` for the API contract. See
  [webui-chat-storage.md](webui-chat-storage.md#custom-providers-dec-26).
- **Shared project context** (DEC-27) — a new "Context" button opens a
  per-workspace, free-form text box (`.handoff/shared-context.md`,
  git-tracked) that's folded into every provider call regardless of mode:
  CLI (via `handoff_bridge.py`'s own prompt construction) and API-key mode
  (fixed or custom, as each provider's own system-prompt field). See
  [webui-chat-storage.md](webui-chat-storage.md#shared-project-context-dec-27).
- **Real self-update for the desktop app** (DEC-28) — the Tauri-packaged
  installer now checks for a new version once per launch and, with your
  confirmation, downloads, verifies, and installs it, then restarts.
  Source-zip/browser-tab users are unaffected and keep the existing
  update-check badge. Every update is cryptographically signed and
  verified before install; see
  [Security Model](security-model.md#tauri-shell-boundaries-phase-7a-dec-22)
  for what that does and doesn't cover, and
  [Release Process](release-process.md#signing-key-dec-28-one-time-setup--already-done)
  for how the signing key is managed.

## v0.3.0 — 2026-08-14

- `handoff_bridge.py`'s `--instruction-type` (on both `init` and `run`)
  now rejects any value outside `new-task`/`continue`/`handoff`/`review`/
  `verify` instead of silently accepting and persisting arbitrary text
  into the shared `.handoff/current.md` state.
- API-key mode: `POST /api/provider-key` now verifies a key with a real,
  minimal, tool-free call to the provider's own API before ever saving
  it — a bad key, wrong model name, or network failure returns a 400
  with nothing written, instead of silently saving an unchecked string.
  `model` is now a hard requirement whenever a non-empty key is saved
  (no built-in default exists for any provider, and the verification
  call needs one). The connection panel's success toast now shows the
  actual confirmation reply. See `docs/webui-chat-storage.md`'s
  "Credentials & API-Key Mode" section.
- API-key mode: **Gemini is now a third supported provider**, alongside
  Codex and Claude — connect it the same way, with a pasted API key and
  an explicit model name (e.g. `gemini-2.5-flash`). Gets the same full
  tool-use turn loop (`read_file`/`write_file`/`edit_file`/`run_shell`)
  as the other two. Resolves the "should Gemini get API-key mode too"
  question this project had left open since the feature first shipped.
- Fixed a real crash on Windows machines whose system locale isn't
  UTF-8-based (e.g. Korean/`cp949`): every real `execute` run would fail
  immediately with `UnicodeEncodeError`, even for the simplest possible
  message, because a subprocess call's stdin encoding silently fell back
  to the OS locale codec instead of UTF-8, and this project's own docs
  (folded into every prompt) contain characters that codec can't
  represent. Every `subprocess.run`/`Popen` call across the project now
  pins `encoding="utf-8"` explicitly.

## v0.2.0 — 2026-08-06

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

- Web UI Phase 4 (API-key mode for CLI-less users, SCR-06): resolves
  CFL-12. Chat-only scope, decided with the user after
  `docs/research-api-key-mode.md` found neither Anthropic's Messages API
  nor OpenAI's Responses API exposes session resume/file-edit/shell-exec
  behind a plain API-key call the way the CLI path does — full agentic
  parity is deliberately deferred to a future phase (new CFL-17), not
  attempted here:
  - added a per-provider connection panel (**Diagnose** titlebar button,
    `webui/index.html`/`app.js`/`app.css`, matching
    `docs/design-system/components.html` §14/wireframes.html SCR-06)
    showing CLI-detected/CLI-missing status, with a masked key (+ optional
    model) field exposed only when a provider's CLI isn't detected;
  - added `GET /api/providers` and `POST /api/provider-key` (empty key =
    remove);
  - added `~/Documents/Agent Handoff Bridge/credentials.json`
    (`0600` permissions, same base directory Phase 2/3 already established
    as "the app owns this") — `read_credentials()`/`save_credential()`
    follow the same failure-isolation pattern as `read_registry()`/
    `touch_registry()`;
  - `_run_provider_via_bridge_locked()` now only diverts to the new
    `run_provider_via_api_key()` path when a provider's CLI is genuinely
    absent (`shutil.which()`) *and* a key is saved for it — every
    previously-existing case (CLI available, or CLI absent with no key)
    is unchanged, verified by the full pre-existing test suite passing
    with no modifications;
  - `call_anthropic_messages_api()`/`call_openai_responses_api()` use only
    `urllib` (no new dependency), through a small `_http_post_json()` seam
    so tests substitute a fake transport instead of making real network
    calls — the same posture the CLI path already has via fake
    `codex`/`claude` scripts;
  - `build_api_message_history()` replays the chat log as alternating
    turns on every call (capped to the most recent 20 entries) since
    neither vendor's API is session-based — stands in for
    `codex exec resume`/`claude --resume`;
  - API-key-mode replies reuse the exact same chat-log record shape the
    CLI path produces (so `classify_run_status()`/`append_chat_message()`
    need no changes), with `session_id`/`run_dir` always `null` and
    `.handoff/state.json`/`current.md` deliberately untouched — those stay
    the CLI-handoff-specific durable state files;
  - the saved API key is never interpolated into any error message/chat-log
    text/toast — every error string is built only from the HTTP response
    body or exception text, verified by tests;
  - Neither provider ships a built-in default model (a Claude default was
    briefly considered/added, then removed in round 3 below once it was
    flagged as an internal, undated assumption rather than a citable
    source) — both return a clear error asking for one to be set via the
    connection panel instead of guessing.
  - **Round 2** (independent second-opinion review, before merge): fixed
    a real gap the first review round didn't cover —
    `build_api_message_history()` mapped the chat log to alternating
    turns 1:1 with no merging, which breaks the moment a single CLI turn
    left two consecutive `agent` entries (`--auto-fallback` chaining
    providers) — Anthropic's Messages API requires strict alternation, so
    that workspace's next API-key-mode call would 400. Now merges
    consecutive same-role entries (including against the final prompt).
    Also fixed: the same function only ever read the *current* month's
    log, silently dropping all prior context on the first message(s) of a
    new UTC month — the same class of cross-month bug Phase 3 already had
    to fix once for `collect_recent_turns()` — now scans months backward
    the same way that function does. Fixed an uncaught `ValueError`
    `_http_post_json()` could raise (uncaught anywhere up the stack) if a
    saved key contained characters `http.client` rejects in a header
    value (e.g. embedded CR/LF) — now converted to a clean error tuple,
    taking care not to forward `http.client`'s own exception text, which
    embeds the offending header *value* (the key) verbatim. Frontend: the
    connection panel's "저장" button deleted a provider's saved key if
    the key field was left blank (e.g. reopening the panel just to fix
    the model) since a saved key is never echoed back into the field —
    now a no-op instead, with a separate "연결 해제" button as the only
    way to actually remove a key; also added a request-generation guard
    on the panel's refresh so an overlapping re-render (a save's own
    refresh racing a fresh reopen) can't render a stale response's rows
    on top of a newer one's. 5 new regression tests (exact count/names via
    `python3 -m unittest discover -s tests -v`, per this file's usual
    anti-drift practice).
  - **Round 3** (two more pasted review passes, before merge): one real
    ordering bug introduced by round 2 itself, one real cross-doc
    consistency gap, one legitimate hardening request, one credential-
    write gap found while writing docs (not by either pasted review), and
    one recurring claim verified and rejected as false:
    - fixed a self-inflicted bug: round 2's `except ValueError:` guard
      around the header-injection case in `_http_post_json()` was broad
      enough to also swallow `json.JSONDecodeError` (a `ValueError`
      subclass) from a malformed-but-200 response body, mislabeling that
      case as "headers were rejected" and making the caller-side
      `except json.JSONDecodeError` handling added earlier in round 2
      unreachable dead code. Restructured so the malformed-body case is
      caught inside the success path specifically, before the broader
      header-rejection handler ever gets a chance to misclassify it;
    - added a small bounded retry (`API_KEY_MODE_MAX_RETRIES = 2`) for
      429/5xx/network-transient failures in `_http_post_json()`, honoring
      a numeric `Retry-After` header when present — `docs/research-api-
      key-mode.md` already noted the official SDKs do this and a
      hand-rolled `urllib` client doesn't get it for free; a review
      correctly flagged the gap between that research finding and what
      had actually been implemented;
    - removed the hardcoded Claude default model
      (`API_KEY_MODE_DEFAULT_MODELS` is now empty for both providers) — a
      review correctly pointed out the only justification for it was this
      session's own internal environment context, not an externally
      citable, dated source the way this project's other model/API claims
      are sourced; both providers now require an explicit model with no
      guessing;
    - found (while updating docs to describe the credential store, not by
      either pasted review) that `save_credential()`'s write failure
      wasn't caught anywhere — unlike `touch_registry()`'s deliberate
      best-effort/log-only posture, a save is the entire point of
      `POST /api/provider-key`, so its failure now surfaces as an ordinary
      `WorkspaceError` → `400` instead of an uncaught exception killing
      that request's thread with no response at all;
    - extended `docs/security-model.md` § Credential Boundaries and
      `docs/architecture.md` § State Boundaries to describe the API-key
      mode exception explicitly (plaintext-at-rest tradeoff, storage
      location, dispatch priority) — both previously only described the
      CLI-only `handoff_bridge.py` posture, which Phase 4 doesn't
      contradict but does add a real, documented exception next to;
    - **verified and rejected**: both review passes also asked for
      `docs/local-data-model.md` and `docs/adr/0010-*`/`0014-*`/`0015-*`
      to be reconciled with this change. Checked `git log --all` across
      every branch — neither has ever existed in this repository at any
      point, and this exact same claim was already raised and resolved
      once before, on an earlier PR (commit `e6c74c1`, "the review's
      suggestion assumed a convention this project doesn't use" —
      `docs/webui-chat-storage.md`'s own opening paragraph documents the
      decision not to use an ADR directory). No ADR system or
      `local-data-model.md` was invented to satisfy a convention this
      project has twice now deliberately not adopted;
    - **considered, not changed**: `.handoff/current.md`'s Phase 4 entry
      names this PR's number and says "not yet merged" — accurate as of
      when it was written, and it'll read as stale after merge the way
      any last-updated packet does until the next session updates it
      again (this repo's own `CLAUDE.md` protocol: "update before
      stopping," not "keep evergreen"). Left as-is rather than rewritten
      to avoid a specific PR number, since that would just trade one
      snapshot-in-time framing for a vaguer one with no real gain.
    - 9 more new regression tests in this round.

- Web UI Phase 5 (Gemini CLI as a third provider + generalized fallback
  **target selection**, not full N-way retry-until-exhausted chaining —
  auto-fallback is still exactly one hop, unchanged from the original
  2-provider design; see the round-2 entry below for why "N-way
  fallback" needed this clarification): resolves CFL-13.
  `docs/research-gemini-cli.md` written first
  (same discipline as `docs/research.md` for Codex/Claude) — found Gemini
  fits the existing subprocess architecture but has no free auth-status
  command, no session ID in its JSON output, and returns one JSON object
  per run instead of a JSONL stream. Two of those needed a real
  pre-implementation decision, not just mechanical extension:
  - `handoff_bridge.py`: `PROVIDERS` extended to `("codex", "claude",
    "gemini")`. `other_provider()`'s hardcoded binary toggle replaced
    with `next_provider(current, tried)`, which walks `PROVIDERS` in
    order and wraps around — all three call sites
    (`init_handoff()`/`choose_auto_provider()`/`run_provider()`'s
    auto-fallback) now use it; auto-fallback is still exactly one hop,
    only *which* provider it lands on generalized;
  - `provider_command()` gained a `gemini` branch (prompt via stdin like
    the other two, `--resume latest` once a prior clean run is recorded
    in this workspace) and `summarize_gemini()` was added, parsing a
    single end-of-run JSON object directly rather than going through
    `parse_jsonl()`;
  - `session_id` for Gemini is always the literal sentinel `"latest"`,
    never a real ID — Gemini's JSON response has none — set only when a
    run completes cleanly with no `error` field, which is exactly when
    `provider_command()`'s next call is safe to add `--resume latest` to
    (DEC-17: chosen after confirming Gemini sessions are scoped per
    workspace directory, not global, which substantially narrows the
    "could resume the wrong conversation" risk that made this a real
    decision rather than a given);
  - `diagnose()` gained a `gemini` row via the existing `PROVIDERS` loop
    for free, plus an explicit "gemini auth: not checked" line (DEC-18)
    — Gemini has no free auth-status subcommand, and a real probe would
    cost a token on every `diagnose` run, so this deliberately doesn't
    check rather than making `diagnose` sometimes-free;
  - `handoff_webui.py`: `API_KEY_MODE_PROVIDERS = ("codex", "claude")`
    added as its own tuple, deliberately not derived from the
    now-3-wide `PROVIDERS` import — Phase 4's API-key mode scope (DEC-15)
    was never revisited to include Gemini, so it must not silently
    inherit a new entry just because the shared CLI-dispatch tuple grew.
    `/api/run` now accepts `gemini`; `/api/providers` shows Gemini's real
    CLI-detection badge (finally resolving what SCR-06 originally shipped
    as a "미확인" placeholder) via a new `api_key_mode_supported` field
    that stays `false` for it, so the connection panel shows status
    without offering a key field;
  - `webui/index.html`/`app.js`: `gemini` added to the provider selector;
    the connection panel checks `api_key_mode_supported` before rendering
    the key/model inputs;
  - `docs/provider-extensibility.md`'s "The Current Code Assumes Exactly
    Two Providers" section rewritten from a plan into a record of what
    actually changed (`classify_handoff()` itself needed no changes, as
    predicted; `ERROR_PATTERNS` needed one small addition, corrected in
    round 2 below);
  - 17 new/updated tests in `tests/test_handoff_bridge.py` (`next_provider()`
    ordering/wraparound/skip-tried, `provider_command()`'s gemini branch,
    `summarize_gemini()` success/error/malformed input, a real-subprocess
    integration test with a fake `gemini` binary) plus updates to
    `tests/test_handoff_webui.py` for the `API_KEY_MODE_PROVIDERS`
    separation. Exact count via
    `python3 -m unittest discover -s tests -v`.
  - **Round 2** (independent adversarial review, before merge): found one
    real bug the N-way refactor missed and one real gap in the resume
    sentinel, both fixed:
    - `handoff_webui.py`'s outer-subprocess-timeout handler had its own,
      separate hardcoded `"claude" if ... == "codex" else "codex"`
      binary guess for which provider was still running when the whole
      thing got killed (`other_provider()`'s replacement in
      `handoff_bridge.py` never touched this webui-local copy of the
      same pattern) — wrong whenever the original provider was
      `"claude"` (a claude run needing handoff recurses into `gemini`,
      not `"codex"`), which would have misattributed the timed-out
      provider in the persisted chat log. Fixed by reusing
      `next_provider()` directly instead of reimplementing the guess;
    - `summarize_gemini()` only checked the JSON response body's
      `error` field before marking the `"latest"` resume sentinel, never
      `exit_code` — since Gemini's own docs don't fully document
      exit-code/JSON-body correlation on failure, a nonzero exit with a
      superficially clean body could have marked a failed run resumable.
      Now requires `exit_code == 0` too.
  - **Round 2** (real automated review posted on the PR itself, not a
    pasted transcript this time): 2 real bugs and 1 real doc
    contradiction found, all fixed:
    - a Gemini `AuthError`/exit-41 auth failure classified as `unknown`
      instead of `auth` — `ERROR_PATTERNS`'s auth regex only matched
      Codex/Claude's own error vocabulary
      (`not logged in`/`authentication_failed`/`unauthorized`/`forbidden`),
      none of which Gemini's literal, verified error strings contain.
      Added `AuthError`/`FatalAuthenticationError` to the pattern —
      exact, sourced strings from `docs/research-gemini-cli.md`, not a
      guess at unverified text;
    - the single-hop auto-fallback could skip past an installed,
      working provider entirely: `next_provider()` (and
      `choose_auto_provider()`'s handoff-needed branch) picked the next
      provider in `PROVIDERS` order with no regard for whether it was
      actually installed — with exactly two providers this never
      mattered (no third option to skip past), but with three, a codex
      failure landing on an uninstalled claude meant gemini was never
      reached, even though it was right there and working. Added
      `next_available_provider()`, a `shutil.which()`-aware wrapper used
      everywhere a fallback target is actually *picked* (not the purely
      informational message in `init_handoff()`); `handoff_webui.py`'s
      timeout-guess (fixed in round 1, above) now calls it too, to keep
      guessing the same thing the real subprocess actually does;
    - `docs/provider-extensibility.md`'s intro still said "nothing
      described here is implemented yet" directly above a section titled
      "...(Resolved In Phase 5)" — reworded to describe both the
      still-forward-looking parts (a hypothetical fourth provider,
      API-key-mode extension) and the now-historical Gemini record in
      the same doc.
    - 7 more new tests in this round (plus 3 pre-existing
      `ChooseAutoProviderTests` pinned to a mocked `shutil.which`, since
      `next_available_provider()` made their outcome depend on what's
      actually installed on whatever machine runs the suite), including
      a real-subprocess integration test reproducing the exact
      skip-uninstalled-provider scenario with fake `codex`/`gemini`
      binaries and no `claude` on `PATH` at all.
  - **Round 3** (same PR, follow-up automated review confirming round 2's
    fixes landed, then raising two more points):
    - a real CI failure this round's own testing didn't catch locally:
      `tests/test_handoff_webui.py`'s outer-timeout guess test never
      mocked `shutil.which`, so it passed on a dev machine with real
      `codex`/`claude` installed but failed in CI's clean environment
      (nothing installed) — `next_available_provider()`'s guess correctly
      fell through to `"codex"` there, since the test's "claude timed
      out" premise implicitly requires claude to have been launchable at
      all. Pinned `shutil.which` in that test to make the premise
      concrete instead of depending on the host's real installed set;
    - **naming precision, not a behavior change**: pointed out that
      "N-way fallback" oversold what actually shipped — auto-fallback
      remained exactly one hop throughout, by design (unchanged from the
      original 2-provider system, to bound token spend if a fallback
      also fails late). What Phase 5 actually generalized was *which*
      provider that one hop can land on. Reworded the headline framing
      above and in `docs/research-gemini-cli.md`'s implementation plan
      to say "fallback target selection," not "N-way fallback," since
      the prose already explaining the one-hop constraint wasn't
      preventing the section *title* from implying more than it delivered;
    - this file's own Phase 5 entry said `classify_handoff()`/
      `ERROR_PATTERNS` "needed no changes" one paragraph above the round-2
      entry documenting that `ERROR_PATTERNS` *did* need one addition —
      corrected to say what's actually true of each (`classify_handoff()`
      itself: no changes; `ERROR_PATTERNS`: one addition).

- Web UI Phase 6 (automatic update check, SCR-07): resolves CFL-11. This
  repo is private, so an anonymous request can't list its GitHub
  Releases — resolved (DEC-19) by shelling out to the user's own local
  `gh` CLI auth rather than standing up new public infrastructure, the
  same tool `docs/release-process.md` already assumes for cutting
  releases:
  - `handoff_bridge.py`: `GITHUB_REPO` constant, `parse_version_tuple()`
    (`"v0.2.0"` → `(0, 2, 0)`, `None` on anything unparseable),
    `check_for_update()` — runs `gh release view --repo <repo> --json
    tagName,url` via the existing `short_run()` helper (which already
    turns a missing/timed-out `gh` into a clean exit code rather than an
    exception), compares against `BRIDGE_VERSION`, returns
    `{latest_version, current_version, url}` only when a genuinely newer
    release exists — never raises, same fail-silent posture as
    `touch_registry()` elsewhere in this project, since this is a
    background convenience check nobody explicitly asked to run;
  - `handoff_webui.py`: `AppState.update_info` (a plain attribute, not
    behind a lock — write-once-then-read-many from a single background
    thread, not a contended read-modify-write like credentials/registry
    are). `main()` starts `_check_for_update_in_background()` as a daemon
    thread right after constructing `AppState`, so the real `gh`
    subprocess call (network I/O, can take a few seconds) never delays
    server startup or the browser/native window opening. `GET
    /api/update-check` only ever reads the cached result, so it's always
    fast regardless of network conditions;
  - `webui/index.html`/`app.css`/`app.js`: a titlebar "업데이트" button
    (always visible, matching components.html §15's "평소엔 아이콘만")
    with a small dot badge that only appears when an update is
    available, opening a popover with the version and a
    "릴리즈 노트 보기" link on click. The wireframe only mocked the
    "update available" state — clicking the button when already current
    (or when the check failed/never ran) reuses the existing toast
    mechanism ("최신 버전을 사용 중입니다") instead of inventing a second
    popover layout for a state nothing designed;
  - 17 new tests: `parse_version_tuple()` (v-prefix, unparseable input,
    differing-length version comparison), `check_for_update()` (newer
    release detected, same/older version not reported as an update, `gh`
    missing/erroring/returning malformed JSON all resolve to `None`, the
    call pins `--repo` rather than relying on `cwd`), the background
    check populating `AppState.update_info`, and `GET /api/update-check`
    reflecting both the empty and populated cache states over a real HTTP
    server. Exact count via `python3 -m unittest discover -s tests -v`.
  - **Round 2** (real automated review, one genuine correctness bug
    found): `state.update_info is None` used to mean both "the
    background check hasn't finished yet" and "it finished and found
    nothing newer" — collapsed into the same
    `{"update_available": false}` response. The real `gh` subprocess
    call is network I/O and can easily still be running when the page's
    first `GET /api/update-check` arrives, especially right after server
    startup, and `webui/app.js` only asked once at boot with no retry —
    so a normal server start could silently and permanently miss
    showing the badge even when an update genuinely existed, not a rare
    edge case. Added `AppState.update_checked` to tell "pending" apart
    from "checked, nothing found," and the frontend now polls (1.5s
    interval, up to 10 times — comfortably past `short_run()`'s 10s
    default timeout) while `checked` is false instead of asking exactly
    once. New and updated tests cover the pending-vs-checked distinction
    specifically — exact count via `python3 -m unittest discover -s tests -v`,
    per this file's usual anti-drift practice (an exact figure here was
    corrected once already after a review found it didn't match the real
    diff).
  - **Round 3** (follow-up review confirming round 2's fix, then one
    more low-severity, non-blocking finding): clicking the update button
    during the ~15s polling window (round 2's fix) showed the same
    "최신 버전을 사용 중입니다" toast as a genuinely confirmed up-to-date
    result, even though nothing had actually been confirmed yet — a
    minor false reassurance if an update actually existed and just
    hadn't been polled-in yet. Added a frontend `updateCheckPending`
    flag (starts `true`, flips to `false` only once a response actually
    reports `checked: true`) so the button shows "업데이트 확인
    중입니다…" during that window instead.
  - **Round 4** (independent self-review, requested explicitly before
    merge rather than waiting further on external review): traced the
    actual bytecode-level read/write ordering rather than trusting the
    round-2 fix's own comments, and found a real (if narrow) reader-side
    counterpart to the exact race round 2 fixed:
    - `GET /api/update-check`'s handler read `update_info` *before*
      `update_checked` — the opposite order from how the background
      thread writes them (`update_info` then `update_checked`, so a
      reader checking `update_checked` second always sees it True only
      after `update_info` is really populated). If the two writes landed
      in the gap between the handler's own two reads, it could observe a
      stale (pre-write) `info` alongside a fresh `checked = True`,
      reporting "checked, no update" for a request that actually raced a
      real update being found — silently reproducing round 2's bug from
      the other side. Fixed by reading `checked` first: the only
      possible stale read becomes `checked = False`, which just makes
      the polling client ask again (safe direction to be wrong in) —
      the value in `update_info` is no longer even consulted unless
      `checked` was already observed `True`;
    - `webui/app.js`'s polling `catch` block gave up permanently on any
      single fetch exception, not just after retries were exhausted —
      undermining the round-2 fix's entire premise (retrying) if even
      one transient blip happened (e.g. the server not yet accepting
      connections in the instant right after startup). Now retried the
      same bounded way as an unfinished check;
    - an off-by-one let 11 fetches through a "`UPDATE_CHECK_MAX_POLLS =
      10`" bound (`attempt < 10` with a 0-indexed `attempt` allows
      attempts 0 through 10); fixed to `attempt + 1 < 10`;
    - added a live-server test exercising the actual pending → checked
      *transition* (a genuine background thread gated by a
      `threading.Event`, not just the two static end-states the earlier
      tests asserted directly via `state.*` assignment) — the gap for
      this existed even though the test class already had the
      `ThreadingHTTPServer` machinery to close it cheaply;
    - corrected an inflated test count from round 2's own entry above.
  - **Round 5** (CFL-18 fix, DEC-20 — the one finding round 3/4 explicitly
    left as "low-severity, not merge-blocking"): `check_for_update()`
    returned `None` for two genuinely different situations — "checked
    successfully, nothing newer" and "couldn't check at all" (`gh`
    missing/unauthenticated/offline/unparseable response) — and callers
    couldn't tell them apart, so a user whose `gh` wasn't set up saw the
    same "최신 버전을 사용 중입니다" toast as someone who'd actually been
    confirmed current. `check_for_update()` now always returns a dict
    (never `None`) with a `status` field: `"available"` (unchanged shape
    plus the field), `"current"`, or `"unavailable"` — the last one
    collapsing every unreadable-response case, matching this project's
    existing fail-silent posture for a background convenience check, just
    now distinguishable from "current" instead of indistinguishable from
    it. `GET /api/update-check` drops the old `update_available` boolean
    and exposes `status` directly. `webui/app.js` tracks
    `latestUpdateStatus` (`"pending"|"available"|"current"|"unavailable"`)
    instead of the old pending-only boolean, and the update button now
    shows a fourth distinct toast — "업데이트를 확인할 수 없습니다." — for
    the unavailable case instead of silently borrowing the "current"
    wording. Tests updated across all three `CheckForUpdate*` classes for
    the new contract, plus a new case covering `unavailable` end-to-end
    over the real live server. Exact count via
    `python3 -m unittest discover -s tests -v`.

- CFL-17 follow-up (full agentic parity for API-key mode, resolved as
  DEC-21): API-key mode started chat-only (DEC-13, Phase 4); this adds
  the file-edit/shell-exec parity with CLI mode that Phase 4 explicitly
  deferred. A design interview resolved two open forks: build file tools
  and the shell tool together in one pass (the larger, riskier option —
  not the more conservative file-tools-only recommendation), and reuse
  DEC-02 (confirm only the first send per session) for every tool call
  this adds rather than requiring a stronger per-call confirmation.
  - `handoff_webui.py`: four tools (`read_file`, `write_file`,
    `edit_file`, `run_shell`), declared once in `_TOOL_SPECS` and
    rendered into each vendor's own schema shape
    (`anthropic_tool_definitions()`/`openai_tool_definitions()`) so the
    two can't silently drift apart. `execute_tool_call()` dispatches to
    the matching executor and never raises — an unknown tool name or a
    bug inside an executor degrades to an error string the model can see,
    not a crash mid-conversation. File tools reuse the existing
    `safe_join()`/`read_file_preview()` primitives for workspace
    confinement and the existing size cap; `run_shell` runs
    `subprocess.run(..., shell=True, cwd=workspace)` with a timeout
    (`TOOL_EXEC_TIMEOUT_SECONDS`, reusing `API_KEY_MODE_TIMEOUT_SECONDS`'
    value) and an output-length cap (`TOOL_OUTPUT_MAX_CHARS`, truncated
    with an explicit note, never silently) — no command allowlist, by
    the interview's own choice, on the reasoning that a bridge-controlled
    shell tool with the workspace as its starting `cwd` (not a sandbox —
    an absolute path or `..` still reaches anywhere the OS user account
    can, exactly as a real terminal or CLI mode's own `codex`/`claude`
    subprocess would) isn't a new tier of trust beyond what CLI mode
    already has when it actually runs.
  - `call_anthropic_messages_api()`/`call_openai_responses_api()` grew
    the actual turn loop in place, rather than introducing new sibling
    functions — a response with no tool-call block still returns on the
    first HTTP call, the exact behavior these two functions had before
    this change, so a plain chat turn is unaffected. Anthropic's loop
    sets `tool_choice.disable_parallel_tool_use: true` (one tool call
    per turn, simpler to log and reason about); OpenAI's Responses API
    has no documented equivalent, so a response containing more than one
    `function_call` item executes and returns results for all of them.
    Both bound a single turn to `MAX_TOOL_ITERATIONS = 15` tool calls,
    returning whatever text exists plus a note if hit, so a confused
    model can't loop indefinitely burning API cost. Tool-call activity
    (tool name, arguments, result) is folded into `final_text` as a
    fenced code block — DEC-03's existing code-block rendering, not a
    new message schema or frontend change — so what ran is visible in
    the persisted chat log even though DEC-02's single confirm gate means
    nothing interrupts the turn to ask per call.
  - Both vendors' tool-use JSON shapes (Anthropic's `tool_use`/
    `tool_result` content blocks, OpenAI's `function_call`/
    `function_call_output` items) were confirmed against each vendor's
    current official documentation before implementing, not assumed from
    older or general knowledge — cited in each function's docstring.
  - New tests: tool schema consistency between the two vendor shapes,
    each tool executor directly (path-escape rejection via `safe_join()`,
    `edit_file`'s exact-one-match requirement, `run_shell`'s timeout and
    output-truncation handling, an executor exception being caught not
    propagated), and the turn loop itself with `execute_tool_call()`
    mocked out (a tool-call round trip, the `MAX_TOOL_ITERATIONS` bound,
    the `tool_use_id`/`call_id` correctly threaded back, OpenAI's
    multiple-function-calls-in-one-output case, malformed
    `arguments` JSON not crashing the loop). Existing tests covering the
    pre-tool-loop single-call behavior needed only a `workspace` argument
    added — a response with no tool-call block is exactly their fixture
    shape, so the loop's zero-iteration case reproduces the old behavior
    verbatim. Exact count via `python3 -m unittest discover -s tests -v`.
  - **Round 2** (independent self-review before opening the PR, then a
    real automated review on GitHub, both genuinely useful — 3 real
    findings between them, no stale/false claims this round): the
    self-review found `MAX_TOOL_ITERATIONS` was bounding HTTP round
    trips, not actual tool executions -- since a single response
    (either vendor) can legitimately carry more than one tool call, a
    model batching many into one response could execute well past the
    intended cap; both loops now track a running executed-count instead.
    Same review found the Anthropic loop only ever executed
    `tool_use_blocks[0]`, silently dropping any others if the API ever
    didn't honor `disable_parallel_tool_use` (a hint, not a guarantee) --
    it now executes every block, matching the OpenAI loop's existing
    defensive handling of a multi-call response. The GitHub review then
    found two more, both fixed: a mid-turn API failure (network error,
    non-200) discarded the transcript of any tool that had *already*
    executed on an earlier iteration -- if `write_file`/`edit_file`/
    `run_shell` already had a real effect before the *next* call failed,
    that record vanished along with the error, undermining DEC-21's own
    premise that skipping per-tool-call confirmation is safe because
    activity stays visible after the fact (`_error_with_transcript()`
    now prefixes any accumulated transcript onto the failure message);
    and `read_file` returned `read_file_preview()`'s content directly,
    bounded only by `MAX_FILE_BYTES` (~256KB, what's read off disk) and
    not by `TOOL_OUTPUT_MAX_CHARS` (4000 chars, what's fed into the
    *next* API call) the way `run_shell`'s output already was -- a
    handful of large-file reads in one turn could still blow past the
    context/cost budget that constant exists to bound. One review
    suggestion (also address the fenced-code-block audit trail
    potentially breaking if tool output/arguments contain their own
    ` ``` `) was addressed too even though the review itself marked it
    optional, not merge-blocking -- it directly supports the same
    post-hoc-visibility guarantee the other two fixes protect
    (`_escape_fence()`, applied to both tool names/arguments and
    results before they're folded into the transcript). New regression
    tests cover all of this directly.
  - **Round 3** (fresh automated review on the Round 2 fix commit,
    "no merge-blocking items" this time — 2 more real but genuinely
    low-severity findings, both addressed anyway): the transcript's
    *argument* side had no length cap even after Round 2 capped the
    result side — `write_file`'s `content`/`edit_file`'s `new_string`
    could still be arbitrarily long and land in the transcript verbatim
    via `json.dumps(tool_input)`, inflating every subsequent call's
    context for a completely normal large file write (the file itself
    stays on disk in full either way — the transcript only needs to
    show the write happened). `_truncate_for_transcript()` now applies
    the same `TOOL_OUTPUT_MAX_CHARS` bound to arguments too. Separately,
    this project's own comments/docs describing `run_shell` as
    "cwd-confined" or "고정" could read as claiming stronger isolation
    than `cwd=workspace` actually provides — it sets the *starting*
    directory only, not a sandbox; an absolute path or `..` still
    reaches anywhere the OS user account can, same as a real terminal or
    CLI mode's own `codex`/`claude` subprocess. Reworded everywhere this
    project describes `run_shell`'s isolation
    (`handoff_webui.py`, this file, `webui-chat-storage.md`,
    `flutter-mapping.html`'s DEC-21) to say so explicitly rather than
    implying more than DEC-21 actually decided.
  - **Round 4** (fresh review on the Round 3 fix commit — "no
    merge-blocking items" again, two more low-severity optional items):
    `subprocess.run(..., timeout=...)`'s `TimeoutExpired` handling only
    guarantees killing the immediate subprocess, not a whole process
    tree a backgrounded/forked command might spawn — real, but
    cross-platform process-group cleanup (`os.killpg` on POSIX, a job
    object on Windows) is meaningfully more code than this round's
    scope, so it's documented as a known, accepted gap
    (`handoff_webui.py`, `webui-chat-storage.md`) rather than
    implemented, the same posture DEC-21 already takes toward
    `run_shell` having no command allowlist. The PR description itself
    still said "cwd-confined" even after Round 3 fixed every persisted
    doc/comment — corrected there too.

- Phase 7a (framework migration kickoff, DEC-22 — CFL-14 resolved): the
  first real, non-Python code in this repo. A design interview resolved
  four architecture forks (Tauri over Electron, keep the Python backend
  as a PyInstaller sidecar rather than a Rust rewrite, keep the existing
  `gh`-based update check rather than either framework's own updater,
  carry `webui/` over near-verbatim this phase) — see DEC-22. This adds
  the smallest sub-phase (7a): prove the sidecar architecture actually
  works end to end on one OS, no packaging/signing/cross-platform build
  yet (7b/7c).
  - `src-tauri/`: a Tauri v2 project (`cargo tauri init`, vanilla JS
    template). `tauri.conf.json`'s `app.windows` is deliberately empty —
    a statically-declared window navigates to its URL the instant it's
    created, which races a PyInstaller onefile binary's real startup
    cost (self-extraction + a full Python import), found by actually
    launching the built `.app` and getting a permanently blank window.
    `src-tauri/src/lib.rs` instead spawns the `agent-handoff-bridge-server`
    sidecar and only builds the window once its stdout contains the
    readiness line `handoff_webui.py`'s `main()` already prints right
    after `ThreadingHTTPServer(...)` binds.
  - Two buffering bugs stacked on top of that first one, both found only
    by testing the real built `.app`, not by unit tests: a piped
    (non-tty) stdout switches CPython to fully-buffered, so the
    readiness print above could sit in Python's own buffer indefinitely
    instead of ever reaching Rust's `CommandEvent::Stdout`, hanging
    window creation forever even with the fix above in place. First
    tried `PYTHONUNBUFFERED=1` on the sidecar spawn; testing against the
    actual PyInstaller onefile binary showed this alone did **not**
    reliably fix it (its bootloader's own environment/re-exec handling
    doesn't guarantee the variable reaches the embedded interpreter).
    The real fix: `handoff_webui.py`'s `main()` now calls
    `sys.stdout.reconfigure(line_buffering=True)` directly, confirmed by
    redirecting the raw binary's stdout to a file and seeing the
    readiness line appear immediately. `PYTHONUNBUFFERED=1` stays on the
    spawn anyway as a harmless extra.
  - Four PyInstaller `--onefile` sidecars, following the real call
    chain: `agent-handoff-bridge-server` (`handoff_webui.py`),
    `agent-handoff-bridge-cli` (`handoff_bridge.py`, invoked by the
    server for `init`/`run`), `agent-handoff-bridge-validate`
    (`scripts/validate_handoff.py`, invoked by the CLI's `check`),
    `agent-handoff-bridge-scan` (`scripts/scan_secrets.py`, invoked by
    validate's secret-scan step) — all four declared in
    `tauri.conf.json`'s `bundle.externalBin` (missing any of the last
    three works in ad hoc local testing but silently isn't bundled into
    the real packaged `.app`). `scripts/build_sidecars.py` is
    the actual, runnable build script for all four (added in a review
    round after the four PyInstaller invocations first existed only as
    interactive shell history) — it imports `handoff_bridge.INSTALL_FILES`
    directly for the CLI sidecar's `--add-data` flags rather than
    keeping a second, driftable copy of that file list.
  - Fixed four real instances of a pre-existing pattern this project
    already had (subprocess-invoking a sibling script via
    `[sys.executable, script_path, ...]`) that breaks under freezing --
    frozen, `sys.executable` is the frozen binary itself, not a Python
    interpreter. `handoff_webui.py` gained `bridge_command_prefix()`;
    `handoff_bridge.py`'s `check()` and
    `scripts/validate_handoff.py`'s `check_secrets()` got the same
    `getattr(sys, "frozen", False)` branch, invoking a sibling sidecar
    binary directly instead. `check_tests()` (re-running this project's
    own dev unit test suite) couldn't be fixed the same way -- that
    suite's own integration tests spawn fresh `sys.executable`
    subprocesses, the exact assumption being worked around, so re-running
    it from inside an already-frozen interpreter hits the same problem
    recursively. It skips cleanly when frozen instead, since a shipped
    app has no dev checkout to test against anyway.
  - The ~50 files `handoff_bridge.py`'s `install`/`init` copy into a new
    workspace (`INSTALL_FILES`) had to be bundled into the CLI sidecar
    via `--add-data` -- PyInstaller onefile doesn't include non-Python
    data files by default, so the frozen `init` would otherwise silently
    produce an incomplete workspace. Dynamically-`unittest.discover()`-ed
    test modules' stdlib imports (`unittest.mock`, `http.server`, etc.)
    needed explicit `--hidden-import` flags for the same reason (not
    visible to PyInstaller's static analysis of the entry-point script
    alone).
  - New tests: `BridgeCommandPrefixTests` (`handoff_webui.py`),
    `CheckCommandTests` (`handoff_bridge.py`), and a new
    `tests/test_validate_handoff.py` (this script had no unit tests
    before) -- all covering both the frozen and unfrozen branches, plus
    the Windows `.exe` suffix. Exact count via
    `python3 -m unittest discover -s tests -v`.
  - Verified against the actual built `.app`, not just unit tests: the
    sidecar starts, a first chat message through the real HTTP API
    creates a real workspace (`.handoff/current.md`/`state.json`) via
    the CLI sidecar, and `agent-handoff-bridge-cli check` passes clean.
    macOS registers the app as `type="Foreground"` with the correct
    bundle ID and a live WebKit renderer process. Direct visual
    (screenshot) confirmation of the rendered window wasn't possible in
    this dev environment (Accessibility-permission limits meant screen
    automation kept targeting the wrong window entirely -- once
    misdirecting a keystroke into an unrelated application, after which
    further screenshot attempts were stopped rather than risking it
    again). In its place: `tauri-plugin-log`'s persisted log file
    (always-on, not just in debug builds -- see the review round below)
    shows the *webview itself*, not `curl`, requesting `GET /`,
    `GET /app.css`, `GET /app.js`, `GET /api/update-check`, and
    `GET /api/info` in sequence right after the window was created --
    exactly the request pattern of a real browser engine parsing the
    HTML and executing the actual frontend, not something achievable by
    a bare HTTP client. Near-conclusive without a screenshot; visually
    confirming firsthand is still recommended.
  - **Review round** (independent self-review before opening the PR, 5
    findings, all addressed): a sidecar that dies or errors before ever
    printing the readiness marker (bad build, port conflict, import
    error) used to leave the app running with no window, no dialog, and
    -- because logging was gated to debug builds only -- no diagnostic
    trail at all in a release build. Fixed: logging is now always on
    (writes to `tauri-plugin-log`'s normal per-platform log file
    regardless of build type), and `tauri-plugin-dialog` was added
    solely for a fatal-startup-error path -- a blocking native dialog
    plus a clean exit if the sidecar terminates or errors before a
    window ever exists, rather than sitting invisibly forever. Also
    found: the new `tests/test_validate_handoff.py` wasn't registered in
    `scripts/validate_handoff.py`'s own `REQUIRED_FILES`/`PYTHON_FILES`
    (every other `tests/test_*.py` was) or in `handoff_bridge.py`'s
    `INSTALL_FILES` (so a normal, unfrozen `install`/`check` would have
    silently never installed or tracked its own new test file) --
    registered in all three. `docs/security-model.md` had no section on
    the new Tauri/sidecar architecture at all; added one, including the
    finding that `capabilities/default.json`'s `shell:allow-execute`
    grant and `tauri.conf.json`'s `"csp": null` are both currently inert
    (the window only ever loads the sidecar's real external
    `http://127.0.0.1:8787/` URL, and this project's Rust code calls the
    shell plugin directly rather than through IPC a capability would
    gate) -- documented so neither is mistaken for load-bearing, or
    loosened further under the assumption it's already doing real work,
    if a future sub-phase adds an actual native command surface
    reachable from the frontend. And: no committed script captured the
    four PyInstaller build invocations, addressed by
    `scripts/build_sidecars.py` above.

- Phase 7b (cross-platform builds, real installers, sidecar lifecycle
  fixes — PRs #13-#16): takes the 7a proof-of-concept from "runs on one
  dev machine" to "actually builds and ships on macOS/Windows/Linux."
  - **Cross-platform sidecar builds**: `scripts/build_sidecars.py`
    (renamed/generalized from the 7a-only `build_phase7a_sidecars.py`)
    now builds on any of macOS/Windows/Linux -- the `--add-data`
    separator uses `os.pathsep` (Windows' `;` vs. everyone else's `:`,
    exactly matching PyInstaller's own rule) instead of a hardcoded `:`,
    and `rename_for_tauri()` automates producing Tauri's
    `<name>-<target-triple>[.exe]` sidecar filenames (previously done by
    hand, once per binary). New CI `sidecar-build` job: a
    `macos-latest`/`windows-latest`/`ubuntu-latest` matrix that actually
    runs this script per OS and smoke-tests each built sidecar
    (`--version`/`--help`) rather than just checking filenames exist --
    this project's first real CI execution on Windows.
  - **Real per-OS installers**: new `installer-build` CI job producing
    genuine, unsigned installers (`.dmg`/`.app` macOS, `.msi`+nsis `.exe`
    Windows, `.deb`/`.AppImage`/`.rpm` Linux) via `cargo tauri build`,
    using the real sidecars above rather than the placeholder files the
    existing compile-check job uses. Deliberately gated to manual
    trigger (`workflow_dispatch`) only, not every PR/push like the rest
    of CI -- GitHub bills private-repo Actions minutes at 10x for macOS
    runners and 2x for Windows, and a real bundle build is comparatively
    expensive. Still unsigned (code signing stays a separate "Phase 7c"
    decision gate, DEC-22/DEC-23 -- new cost, Apple Developer Program
    $99/year+ -- explicitly declined to start for now).
  - **`docs/release-process.md` rewritten** for two parallel packaging
    tracks instead of one: the original git-free source zip
    (`scripts/package_platforms.py`, unchanged, for terminal/CLI-only
    use) stays alongside the new installers (for desktop GUI use) rather
    than being replaced by them -- confirmed explicitly with the repo
    owner rather than assumed, recorded as **DEC-23** (resolves the
    long-open **CFL-09**, which had assumed the zip model would end
    entirely once a framework migration happened).
  - **Sidecar lifecycle, verified by actually building and quitting the
    real `.app`, not just reading code**: two Phase 7a-deferred
    questions turned out to need real fixes, not just verification.
    Sidecar cleanup on app quit was genuinely broken -- discovered via a
    real leftover orphaned process (parented to `launchd`, still holding
    port 8787 hours after its app had exited) -- caused by the spawned
    sidecar's `CommandChild` being discarded with no cleanup hook.
    Fixed: the child is now kept in Tauri managed state and killed on
    `RunEvent::Exit` (not `ExitRequested`, which never fires on a real
    quit here -- confirmed by logging every event a real quit actually
    produces). Getting a *clean* kill took two more rounds: a
    single-PID `kill()` only reached the outer PyInstaller bootloader,
    orphaning its re-exec'd inner process all over again, and a
    single-hop `pkill -P` only reached the first process generation --
    an in-flight provider run's real tree is 3-4 generations deep (a
    second sidecar spawned mid-run, its own re-exec'd interpreter, then
    the real `codex`/`claude`/`gemini` subprocess). Fixed by walking the
    whole descendant tree before killing anything. Then: hard-killing
    that whole tree unconditionally was itself a real risk if a provider
    was mid-write -- now signals `SIGTERM`/a non-forced `taskkill`
    first, waits briefly, and force-kills only whatever's still alive.
    Port 8787 conflict handling was already non-hanging (an existing
    error dialog already caught it) but showed a generic message;
    `handoff_webui.py` now prints a stable marker
    (`AHB_PORT_CONFLICT`) before re-raising the bind failure, and the
    Rust side matches on that instead of guessing at OS/locale-specific
    `OSError` text.
  - **Known, accepted gaps, left open on purpose rather than guessed
    at**: the deeper tree-kill and graceful-terminate logic above was
    verified via compile checks only, not a repeated real
    build-launch-quit cycle (a deliberate tradeoff after a local
    system-resource-load concern came up during testing). Windows'
    version of the same graceful-then-force kill still has a known,
    unfixed edge case (re-targeting an already-dead root PID can miss
    surviving descendants) -- left as-is because Windows-specific code
    in this codebase has no verification path at all in this dev
    environment, not even a CI compile check (`rust-build` only runs on
    Linux). Real Windows/Linux testing of this whole feature remains
    outstanding.

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
