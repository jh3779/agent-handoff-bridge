# Codebase Review (2026-08-14)

A point-in-time code review of the whole project — architecture, code
quality, notable strengths, and concrete findings — written after a working
session that touched the core bridge CLI, the Web UI's API-key mode, and
this project's first real Windows verification pass. This is a snapshot,
not a living doc; treat file:line references as accurate as of this date
and re-verify before relying on them later (see `docs/index.md`'s own
staleness caveat for the docs system generally).

Scope: `handoff_bridge.py`, `handoff_webui.py`, `handoff_desktop.py`,
`handoff_control.py`, `remote_handoff_server.py`/`remote_handoff_submit.py`,
`scripts/`, `webui/`, `src-tauri/`, `tests/`, and the documentation system
itself. Not covered in depth: `.github/workflows/ci.yml` internals,
`examples/`.

## Bottom Line

This is an unusually well-tested, well-documented project for its size —
5,631 lines across the five main Python modules against 5,923 lines of test
code (452 tests, 0 failures at review time), and a documentation system
that tracks *decisions* (`docs/design-system/flutter-mapping.html`'s
Decision Log), not just current state, so the reasoning behind non-obvious
choices survives past the session that made them. The main risks are not
"undertested" or "undocumented" — they're the ones inherent to the
project's own stated design: `run_shell` in API-key mode has no sandbox by
design (DEC-21), and the whole project has had exactly one operator/tester
userbase for most of its life, so some code paths (Windows/Linux runtime
behavior, non-UTF-8 locales, a genuinely malicious model response) have had
much less real-world exposure than the macOS-developer-day-to-day path.

## Architecture Summary

```text
User / phone / CLI
        |
        v
handoff_control.py / handoff_desktop.py  ->  handoff_bridge.py
        |                                          |
        |                                          +--> codex exec --json
        |                                          |
        |                                          +--> claude -p --output-format stream-json
        |                                          |
        |                                          +--> gemini --output-format json
        |
        v
.handoff/current.md + shared docs + git workspace
```

A second, largely independent surface exists alongside the CLI bridge:
`handoff_webui.py`, a stdlib-only local HTTP server (no Flask/FastAPI — a
deliberate no-new-dependency choice, consistent with the rest of the
project) serving a vanilla-JS chat UI (`webui/`), optionally wrapped in a
native window by a Tauri shell (`src-tauri/`, Phase 7). The Web UI can
either shell out to a real provider CLI (same subprocess path as
`handoff_bridge.py run`) or, when no CLI is installed, talk to a provider's
raw HTTP API directly (API-key mode, `call_anthropic_messages_api()`/
`call_openai_responses_api()`/`call_gemini_api()`) — its own small
reimplementation of a tool-use turn loop, since none of the three vendors'
plain chat APIs edit files or run shell commands themselves.

`docs/architecture.md`'s own diagram (reproduced above, unchanged) predates
the Web UI/Tauri work — it still describes the project as if
`handoff_control.py`/`handoff_desktop.py` → `handoff_bridge.py` is the
whole system. **Finding (docs drift, low severity):** the Web UI is a
real, substantial second architecture branch (`handoff_webui.py` alone is
larger than `handoff_bridge.py`) with no mention in `architecture.md` at
all — someone reading only that file would not know it exists.
`docs/webui-chat-storage.md` and `docs/security-model.md`'s "Tauri Shell
Boundaries" section do cover it well, just not the top-level architecture
doc a newcomer would read first.

## `handoff_bridge.py` (1,266 lines) — the core CLI bridge

**What it does**: installs shared handoff files into a workspace, builds
provider-agnostic prompts (folding in the shared contract docs + git
status/diff), runs `codex`/`claude`/`gemini` as subprocesses, classifies
whether the run needs a handoff to the other provider, and records
everything into `.handoff/current.md`/`state.json`.

**Strengths**:
- `classify_handoff()` is a single, well-tested, provider-agnostic
  classification function — new providers plug into the existing
  `ERROR_PATTERNS` list rather than needing their own bespoke logic (with
  one documented exception: Gemini's real auth-error shape needed one
  pattern addition, found via testing against the real binary, not
  assumed — see `docs/research-gemini-cli.md`'s "Real CLI Verification"
  section).
- `WriteLock`/`atomic_write_text` are the one, shared, correctly-used
  primitive for every write to `.handoff/current.md`/`state.json` — no
  ad-hoc `open("a")` anywhere in the codebase that bypasses them
  (verified: `remote_handoff_server.py` and `scripts/handoff_hook.py`
  both import and reuse the same lock rather than reimplementing).
- `RUN_LOCK_FILE` (a second, coarser lock than `WRITE_LOCK_FILE`) exists
  specifically because `remote_handoff_server.py` can spawn overlapping
  `handoff_bridge.py run` subprocesses against the same workspace — a
  real concurrency scenario this project actually has, not a
  defend-against-nothing abstraction.
- `next_provider()`/`next_available_provider()` (replacing an original
  hardcoded two-way ternary) generalize cleanly to Gemini as a third
  provider, and the auto-fallback path is deliberately still exactly one
  hop (a documented, conscious token-spend-bounding decision, not an
  oversight — `docs/research.md`).

**Findings from this session's work** (all fixed, listed for the record
since a "code review" should include what was actually found, not just
current-state praise):
- `--instruction-type` (both `init` and `run` subcommands) had no
  `choices=` restriction at all, unlike the sibling `--primary`/`provider`
  arguments — an arbitrary string was silently accepted and written into
  the shared `.handoff/current.md` state. Fixed with a new
  `INSTRUCTION_TYPES` constant + `choices=`.
- Every `subprocess.run(..., text=True, ...)` call in this file (and
  across the whole project — see "Cross-cutting findings" below) omitted
  an explicit `encoding=`, defaulting to the OS locale codec instead of
  UTF-8 — a real, reproducible crash on any non-UTF-8-locale machine
  (confirmed: Korean/`cp949` Windows), since this project's own docs
  (folded into every prompt via `build_prompt()`) contain characters that
  codec can't encode. Fixed everywhere with `encoding="utf-8"`.
- `check()`/`bridge_command_prefix()`-equivalent frozen-sidecar path
  logic used the host-native `pathlib.Path` instead of
  `PureWindowsPath`/`PurePosixPath` — never a live production bug (a real
  frozen build's `sys.executable` always matches its real host OS), but
  it broke unit tests for the "other" platform's frozen behavior whenever
  the suite ran on a genuinely different host OS than whoever wrote the
  test used. Fixed to be host-independent, which is also more testable.

**Design choices worth flagging, not necessarily fixing**: `run_shell`
inside API-key mode's tool loop (in `handoff_webui.py`, but the same trust
model as this file's own CLI subprocess calls) has no command allowlist and
`cwd=workspace` is a starting directory, not a sandbox — `..` or an
absolute path reaches anywhere the OS user account can. This is DEC-21's
explicit, interview-confirmed choice ("same trust level CLI mode's own
subprocesses already have, not a new tier"), not an oversight, but it's
worth restating in a review: this project has no sandboxing layer at all,
by design, for any provider's shell access.

## `handoff_webui.py` (2,621 lines) — the largest single file

**What it does**: a stdlib `http.server`-based local server, serving the
chat UI, workspace file browsing, chat history persistence
(`.handoff/webui/chat/*.jsonl`, monthly, gzip-archived), and two ways to
actually run a provider — real CLI subprocess (reusing the same
`handoff_bridge.py run` path) or direct HTTP API calls when no CLI is
installed (API-key mode).

**Strengths**:
- The API-key-mode tool loop
  (`call_anthropic_messages_api()`/`call_openai_responses_api()`/
  `call_gemini_api()`) is a genuine, non-trivial reimplementation of
  "an agent that edits files and runs shell commands" against three
  different vendors' raw APIs, and each one's request/response shape was
  confirmed against that vendor's current official docs before
  implementation (cited directly in each function's docstring and in
  `docs/research-api-key-mode.md`) rather than assumed — a real, repeated
  discipline across three separate implementations, not a one-off.
- `_TOOL_SPECS` is declared once and rendered into each vendor's schema
  shape (`anthropic_tool_definitions()`/`openai_tool_definitions()`/
  `gemini_tool_definitions()`), so the three can't silently drift out of
  sync with each other — a real structural guard, not just a convention.
- Every tool-loop function defensively executes *every* tool-call block a
  response contains, not just the first — each vendor's API is trusted
  as little as reasonably possible (a documented review finding: an
  earlier version of the Anthropic loop only executed
  `tool_use_blocks[0]` and silently dropped the rest if a response ever
  carried more than one, despite `disable_parallel_tool_use` being only a
  hint, not a guarantee).
- The update-check race (`AppState.update_checked`/`update_info`, read in
  the opposite order from how the background thread writes them) is a
  genuinely subtle concurrency bug that was found and fixed correctly —
  worth calling out because it's exactly the kind of bug a less careful
  project would ship and never notice (see `.handoff/current.md`'s Phase
  6 history for the full account).

**Findings from this session's work**:
- `POST /api/provider-key` previously wrote any non-empty key string to
  `credentials.json` unconditionally — no live check that the key
  actually works. Fixed with `validate_provider_api_key()`, a real
  minimal API call before any write.
- Several `subprocess.run` calls in this file shared the same
  locale-encoding gap as `handoff_bridge.py` (see above) — fixed
  identically.
- Gemini was excluded from API-key mode by an explicit, previously-open
  design decision (DEC-15) rather than an oversight; this session
  resolved it (DEC-25) by adding `call_gemini_api()`.

**A structural note, not a defect**: at 2,621 lines, this is a large
single file for a stdlib-only project with no module-splitting
convention established elsewhere in the codebase (the other Python files
are all single-purpose and much smaller). It works today because the
functions inside it are each independently well-tested and the file is
organized into clear, consistently-labeled sections (credential storage,
tool-use loop, HTTP handler, update-check, etc.) — but it's the one file
in this codebase where "just read the whole file" stops being a realistic
onboarding strategy. Not a recommendation to split it now (that's a real
refactor with its own risk, not something to do opportunistically), just
worth naming as the file most likely to need one eventually.

## Test Suite & Quality Gates

452 tests, 0 failures at review time (`python -m unittest discover -s
tests`), 35 legitimately skipped (POSIX-shell-only integration tests,
symlink tests, POSIX-permission tests — all correctly gated on the
platform feature they need, confirmed by running the suite for real on
both a Unix-like environment's history and, this session, a genuine
Windows machine for the first time). Test code (5,923 lines) outweighs
the five main production modules combined (5,631 lines) — an unusually
high ratio for a project this size, and it shows in practice: this
session found and fixed several real bugs specifically *because* new
tests were added and actually run, not from code reading alone (the
`addCleanup` LIFO-ordering bug that only manifests on Windows is a good
example — invisible in code review, immediate in a real test run).

**Coverage gaps found**: `scripts/build_sidecars.py` and
`scripts/package_platforms.py` are the only two production scripts in
the repo with no corresponding test file at all
(`tests/test_build_sidecars.py`/`test_package_platforms.py` don't
exist). Lower severity than untested application logic — both are
build/packaging scripts that mostly shell out and produce artifacts
rather than branch on business logic — but worth naming as a real,
concrete gap rather than assuming "everything is tested" from the
otherwise-strong ratio above. `docs/quality-gates.md`'s own "Core Logic
Has Unit Tests" rule already scopes itself to the highest-risk
pure-logic surfaces (`classify_handoff`, `choose_auto_provider`,
`scan_secrets.py`'s pattern matching, `check_branch_name.py`'s parsing) —
these two scripts were never claimed to be in scope, so this isn't a
violation of the project's own stated rule, just a gap the rule doesn't
currently cover.

**Quality gates** (`docs/quality-gates.md`) are enforced at up to three
real layers (doc, local git hook, CI), each with a script backing it —
notably, this doc itself grew out of a full-repo review that found
several rules were "written down but not checked by anything," and
converted them into actually-enforced checks. That self-correcting
history (a rule about rules, enforced) is a good sign for the project's
overall discipline, not just this specific ruleset's current state.

**A repeated, valuable pattern**: multiple `tests/test_*.py` docstrings
explicitly say *why* a test exists — "regression test for X, found via
review on date Y" — rather than just what it checks. This makes the test
suite double as a partial project history, which is unusually useful for
anyone (human or agent) picking the project back up cold.

## Documentation System

The `docs/design-system/flutter-mapping.html` Decision Log (now 25
entries, DEC-01 through DEC-25) is the standout piece of this project's
documentation practice: every non-obvious design choice is recorded with
*why*, not just *what*, and — more unusually — open questions are
explicitly left open (e.g. DEC-15's "Gemini API-key support is a separate,
not-yet-made decision") rather than silently decided by whoever touches
the code next. This session's own work is a direct example of that system
working as intended: DEC-25 exists specifically because DEC-15 flagged the
question in writing years earlier (in project time) instead of leaving it
implicit.

`docs/quality-gates.md`, `docs/webui-chat-storage.md`, and
`docs/provider-extensibility.md` all follow the same pattern of
distinguishing "the original forward-looking plan" from "what actually
happened" once implementation diverged from the plan — a real discipline,
not just thorough writing, since it means a reader can trust the doc
describes reality rather than aspiration.

**Drift found**: `docs/architecture.md` (noted above) predates the Web
UI/Tauri work and doesn't mention either. Two smaller, now-fixed instances
found and corrected during this session: `docs/design-system/roadmap.md`
and `components.html` both still said "API-key mode is Codex/Claude only"
after DEC-25 changed that. Neither drift was severe (both were corrected
same-session, not left to compound), but the pattern — a `## Unreleased`
release-notes section that's easy to forget, and a Decision Log entry that
doesn't always get a matching update everywhere else it's referenced — is
worth watching for.

## Cross-Cutting Finding: Locale-Dependent `subprocess` Encoding

Worth calling out as its own item since it touched almost every file in
the project, not just the two above: **every** `subprocess.run`/`Popen`
call across `handoff_bridge.py`, `handoff_webui.py`,
`remote_handoff_server.py`, `handoff_desktop.py`,
`scripts/validate_handoff.py`, `scripts/scan_secrets.py`,
`scripts/check_branch_name.py`, `scripts/handoff_hook.py`, and
`scripts/build_sidecars.py` had this same gap. It's the kind of bug class
that's easy to miss entirely on a single-OS, single-locale development
environment (this project's history is heavily macOS-based) and only
surfaces the first time the code actually runs somewhere different — which
is exactly what happened here. All confirmed call sites are now fixed with
explicit `encoding="utf-8"` (plus `errors="replace"` on
output-capturing calls, matching `decode_timeout_output()`'s existing
never-crash-on-decode posture, kept strict only where a decode failure is
itself a meaningful signal — `scan_secrets.py`'s "this file isn't UTF-8
text, skip it" check).

## `handoff_desktop.py` / `handoff_control.py` — the two non-GUI-vs-GUI controllers

Both are thin wrappers around `handoff_bridge.py` (macOS/Windows Tkinter
GUI and a terminal menu, respectively — `handoff_control.py` is also the
fallback when `tkinter` isn't available). Both are well-organized, and
both carry the same kind of "here's the bug this fixed" inline comments
seen elsewhere in the project (prompt-via-`--prompt-file`, `--model=value`
not `--model value`, correct background-thread/`self.after()` marshaling
in the Tk case).

**Finding (real, confirmed by direct read — `handoff_control.py:45-51`,
`81-88`): `initialize_task()`'s primary-provider prompt accepts `"auto"`
as a valid answer, but `handoff_bridge.py init --primary` does not.**
`ask_provider()` validates against the full `PROVIDERS` tuple (`("auto",)
+ BRIDGE_PROVIDERS`, line 20) — appropriate for the *run* prompt, where
`"auto"` is meaningful, but `initialize_task()` reuses the same function
for the **primary**-provider prompt and passes whatever the user typed
straight through as `--primary` (line 88). `handoff_bridge.py`'s own
`init --primary` argparse `choices=` is `PROVIDERS` from
*`handoff_bridge.py`* (confirmed: `handoff_bridge.py:1229`), which has no
`"auto"` entry. A user who types `auto` at that specific prompt (its
default is `"codex"`, but nothing stops typing something else) gets a
confusing raw argparse error from the child subprocess instead of a clean
message. `run_once()` in the same file (its one-shot CLI-flag path, not
the interactive menu) already avoids this correctly via its own
`choices=BRIDGE_PROVIDERS` on `--primary` — and `handoff_desktop.py`
avoids it the same way, with a separate `PRIMARY_PROVIDERS` combobox
(lines 42, 110) that only offers `codex`/`claude`/`gemini`. This looks
like a straightforward fix: give `initialize_task()` its own
`ask_provider`-equivalent restricted to `BRIDGE_PROVIDERS`, the same
pattern its two siblings already use.

Not fixed as part of this review pass (this document records findings; it
doesn't apply them) — flagged here as a concrete, actionable item.

## `remote_handoff_server.py` / `remote_handoff_submit.py` — the optional HTTP remote path

**Server**: auth uses `secrets.compare_digest` (timing-safe, correct);
`load_or_create_token()` correctly treats a blank existing token file as
"no token yet" rather than reusing it (a documented fix for a real
auth-bypass this project already caught); task/state writes go through
the same shared `WriteLock`/`atomic_write_text` as everything else;
`run_task()` inserts `"--"` before user-controlled task/prompt text
specifically so a prompt of literally `"--execute"` can't be parsed as a
flag by the child's argparse. The `/tasks/<id>` GET handler's path
construction (`task_id = parsed.path.rsplit("/", 1)[-1]`, then `TASKS_DIR
/ f"{task_id}.json"`) was checked directly for traversal — since only the
last `/`-segment is taken and no percent-decoding happens on it, an id
like `".."` produces the literal filename `"...json"`, not an escape from
`TASKS_DIR`. No traversal found. `main()` refuses `--no-auth` on a
non-loopback host, matching `docs/security-model.md`. This is a
carefully-written module for what it is: a "trusted local automation
only" server, and its own docs are honest about that scope (full
stdout/stderr, up to 8000 chars, is returned to any valid bearer-token
holder with no per-task ACL — an explicit trust boundary, not an
oversight).

**Client — finding (real, confirmed by direct read,
`remote_handoff_submit.py:93`): `--auto-fallback` can never be turned
off from this CLI.**

```python
parser.add_argument("--auto-fallback", action="store_true", default=True)
```

`action="store_true"` normally defaults to `False` and flips to `True`
only when the flag is passed. Setting `default=True` on top of it means
`args.auto_fallback` is `True` unconditionally — whether or not
`--auto-fallback` appears on the command line — and there is no
`--no-auto-fallback` counterpart. Compare with `--no-create-workspace`
three lines below it (line 94), which correctly uses `dest=... ,
action="store_false"` to provide a real opt-out. The server-side
`normalize_task()` (`remote_handoff_server.py:239`) fully supports
`auto_fallback: false` via the JSON API directly — only this specific CLI
client has no way to ever send it. A real, user-visible gap in the
client's argument surface, not just a style nit; also unfixed here,
flagged for a future pass.

## Build & Packaging Scripts (`scripts/build_sidecars.py`, `scripts/package_platforms.py`)

Both are well-commented, fail-fast (missing expected output/input file
raises rather than silently producing an incomplete artifact), and
`build_sidecars.py` deliberately reuses `handoff_bridge.INSTALL_FILES` as
its one source of truth for bundled data files instead of a second
hardcoded list. Neither has a test file (`tests/test_build_sidecars.py`/
`tests/test_package_platforms.py` don't exist) — reasonable given both
need PyInstaller/rustc and produce real build artifacts, but it does mean
`detect_target_triple()`'s parsing and `package_platforms.py`'s file-list
translation are unverified by automation.

**Finding (needs confirmation, not independently verified against a real
extracted zip in this pass): `package_platforms.py`'s `COMMON_FILES` list
may be missing several test files.** Only `tests/test_handoff_bridge.py`,
`tests/test_scan_secrets.py`, `tests/test_check_branch_name.py`,
`tests/test_handoff_webui.py`, and `tests/test_validate_handoff.py` are
listed — `tests/test_handoff_control.py`, `tests/test_handoff_desktop.py`,
`tests/test_remote_handoff_server.py`,
`tests/test_remote_handoff_submit.py`, and `tests/test_handoff_hook.py`
are not. If `handoff_bridge.py check`'s `check_tests()` step is meant to
discover and run the *complete* test suite from an extracted release zip
the same way it does from a git checkout, this would mean the zipped
release silently runs a smaller test suite than the source checkout — not
a failure, just quieter coverage. This may be an intentional "core
enough to ship" subset rather than an oversight; worth a direct
confirmation with whoever last touched this list before treating it as a
bug to fix.

**Smaller finding**: `scripts/handoff_hook.py`'s `write_next_prompt()`
writes `.handoff/next-prompt.md` via a plain `write_text()` call, with no
`WriteLock`/`atomic_write_text` — inconsistent with `append_current()`
right above it in the same file, which correctly reuses the shared lock.
Given `next-prompt.md` is only produced on a `StopFailure` hook event and
read by a human/the next agent turn rather than written concurrently by
multiple processes, the practical risk is low, but it's worth the same
one-line consistency fix if this file is touched again.

## Web UI Frontend (`webui/app.js`, 945 lines)

No framework, no build step — a single IIFE organized into clearly
banner-commented feature sections, with module-level `let`s for state
rather than a store/reducer pattern. Appropriate for this size (one
screen, no routing), not a smell.

**Strengths, verified directly**:
- All DOM construction goes through one small `el(tag, attrs, children)`
  helper; the only `.innerHTML` uses are `= ""` to clear a container
  before repopulating with `el()`-built nodes — never assigning untrusted
  content. Provider-authored message text specifically renders through
  `document.createTextNode`/`el(..., {text: ...})` (sets `textContent`),
  with the file's own comment noting this is deliberate because that text
  "can come from a provider's response, which this app doesn't fully
  control." No XSS vector found.
- Every `fetch` call goes through one of two small wrappers
  (`fetchJSON`/`postJSON`) that throw on non-2xx and get caught at every
  call site with a user-visible toast — no silently-swallowed failure
  found anywhere in the file.
- Several real races are guarded with comments that cite the actual bug
  found, not just defensive boilerplate: a monotonic
  `providerPanelRequestId` token discards a stale, out-of-order
  `/api/providers` response instead of letting it overwrite a newer
  render; `runInFlight` blocks a second concurrent `/api/run` the
  Enter-key path wouldn't otherwise catch; the update-check poller is
  bounded and retries on both "not checked yet" and a transient fetch
  exception. No unguarded double-fetch or stale-write path was found
  during this review.

**Minor findings**: if the server ever returns a non-JSON body (e.g. a
proxy's HTML error page), `res.json()` throws before the wrappers'
`!res.ok` check runs, so the user sees a generic parse-error toast
instead of the more specific "request failed: 502" — cosmetic only.
`index.html`'s manual-folder-path overlay text instructs the user to `pip
install pywebview` for a native folder picker — accurate for the legacy
pywebview desktop path, but shown unconditionally, including inside a
compiled Tauri app where there's no Python environment for an end user to
touch at all (related to the Tauri finding below).

## Tauri Shell (`src-tauri/`)

**Sidecar lifecycle is unusually thorough.** `lib.rs` fixed a real,
previously-shipped bug (a dropped `CommandChild` leaked the sidecar
process past app quit — confirmed via `lsof` during Phase 7b's own
testing, per `.handoff/current.md`'s history) with a graceful-then-force
kill of the *entire descendant process tree*, not just the tracked PID —
necessary because a live provider run is 3-4 process generations deep
(sidecar bootloader → its interpreter → a second spawned sidecar → its
interpreter → the real `codex`/`claude`/`gemini` process). `SIGTERM` (or
non-forced `taskkill /T`) is tried first, with a grace period before
`SIGKILL`/`-F`, specifically so a live in-flight provider write isn't
just hard-killed.

**Finding (needs confirmation): sidecar spawn failures use `.expect(...)`,
which panics rather than showing the fatal-startup dialog the rest of the
file otherwise uses.** `lib.rs:90`/`93`'s `.sidecar(...).expect(...)`/
`.spawn().expect(...)` would panic if sidecar-command construction or the
*initial* spawn call itself fails — as distinct from the sidecar starting
and then failing, which the file does handle gracefully via
`CommandEvent::Error`/`Terminated` with a real dialog. In a release build
(`windows_subsystem = "windows"` hides the console), a panic here could
be effectively invisible to the user — the exact "sits there with no
diagnostic trail" failure mode the rest of the file's fatal-error-dialog
design explicitly exists to avoid. Worth confirming whether
`.sidecar()`/`.spawn()` can realistically fail this way in a well-formed
release build (e.g. a missing bundled binary); if so, this is a real
inconsistency with the file's own stated philosophy.

**Capabilities are tightly scoped and verified, not just asserted**:
`capabilities/default.json` grants only `core:default` for the main
window — no `shell:*`/`fs:*`/`dialog:*` exposed to the webview's JS,
confirmed by the *absence* of any `window.__TAURI__`/`invoke(...)` call
anywhere in `webui/app.js` — the frontend only ever talks to the local
Python HTTP server, never to Tauri directly. `tauri.conf.json`'s `"csp":
null` looks alarming out of context but is a documented no-op given the
window navigates to external `http://127.0.0.1:8787/` content rather than
Tauri's own asset/IPC protocol; `docs/security-model.md`'s "Tauri Shell
Boundaries" section explains this accurately and matches the code.

**Finding (real, already partially known): `tauri-plugin-dialog` is
registered but only ever used for the fatal-startup-error dialog, never a
real folder picker.** `webui/app.js`'s `pickFolder()` checks for
`window.pywebview` to decide whether a native picker is available, but
Tauri never injects that global — so the Tauri-packaged app always falls
through to the manual-path-typing overlay, whose copy (see above) tells
the user to `pip install pywebview`, which makes no sense for a compiled
binary. `docs/security-model.md` already names "wiring a native folder
picker" as unimplemented future work, so this isn't a fresh discovery —
but the specific mismatch (wrong install instructions surfacing inside
the exact app that can't act on them) is worth restating as a concrete,
still-open item.

## Documentation Accuracy, Verified Directly

`docs/security-model.md`'s "Tauri Shell Boundaries" section was checked
claim-by-claim against `lib.rs`/`capabilities/default.json` (capability
scoping, the CSP no-op rationale, the dialog-plugin's actual scope, the
sidecar-tree-kill fix and why a single-hop kill wasn't enough) — no
discrepancies found; this section is current. `docs/architecture.md`, by
contrast, still describes only the CLI-bridge path (`handoff_control.py`/
`handoff_desktop.py` → `handoff_bridge.py`) with no mention of
`handoff_webui.py`, `webui/`, or `src-tauri/` at all — confirmed by two
independent reads (this document's own earlier section, and the
dedicated frontend/Tauri review pass). Given `docs/security-model.md` and
`docs/webui-chat-storage.md` both *have* been kept current for that same
subsystem, this reads as a specific, closeable gap in one file rather
than a systemic documentation problem.

## Consolidated Findings

Everything below was either fixed during this session's own work (marked
**fixed**) or is a new observation from this review pass, not yet acted
on (marked **open**) — listed here as a single reference list, in
descending rough severity:

| # | Finding | File | Status |
|---|---|---|---|
| 1 | Every `subprocess.run(text=True, ...)` call omitted `encoding=`, crashing on any non-UTF-8-locale OS | project-wide | **fixed** |
| 2 | `--auto-fallback` in the remote-submit CLI can never be set to `False` (`action="store_true", default=True`) | `remote_handoff_submit.py:93` | **open** |
| 3 | `initialize_task()`'s primary-provider prompt accepts `"auto"`, which `--primary` then rejects | `handoff_control.py:81-88` | **open** |
| 4 | `--instruction-type` accepted and persisted arbitrary values with no validation | `handoff_bridge.py` | **fixed** |
| 5 | `POST /api/provider-key` saved any non-empty key string with no live check that it works | `handoff_webui.py` | **fixed** |
| 6 | Sidecar spawn failures `.expect()`/panic instead of using the file's own fatal-error dialog path | `src-tauri/src/lib.rs:90,93` | **open, needs confirmation** |
| 7 | Frozen-sidecar path construction used host-native `Path`, breaking cross-platform unit tests | `handoff_bridge.py`, `handoff_webui.py`, `scripts/validate_handoff.py` | **fixed** |
| 8 | `package_platforms.py`'s `COMMON_FILES` may omit several test files from the shipped zip | `scripts/package_platforms.py` | **open, needs confirmation** |
| 9 | Tauri app shows "pip install pywebview" folder-picker instructions it can never act on | `webui/index.html`, `webui/app.js` | **open, already named as future work in security-model.md** |
| 10 | `write_next_prompt()` doesn't use the shared `WriteLock`, unlike its sibling `append_current()` | `scripts/handoff_hook.py` | **open, low risk** |
| 11 | `docs/architecture.md` doesn't mention the Web UI or Tauri shell at all | `docs/architecture.md` | **open, docs only** |

## Overall Assessment

For a project built and maintained largely through agent-assisted
sessions, the thing most worth naming is that the *process* the project
uses — record every non-obvious decision with its reasoning
(`flutter-mapping.html`'s Decision Log), keep a running handoff packet
(`.handoff/current.md`) detailed enough that a fresh session can pick up
context cold, write tests that explain *why* they exist, and distinguish
documented plans from documented reality — has produced a codebase that
holds up well under a real, adversarial-ish review. The bugs found in
this pass are real but modest: two CLI argument-parsing gaps
(#2, #3), one Rust error-handling inconsistency to confirm (#6), and a
couple of doc/packaging-list drifts (#8, #9, #11) — nothing structural,
nothing that suggests the architecture itself needs rethinking. The
project's own habit of writing down *why* a decision was made rather than
just what it is (e.g. DEC-21's explicit, interview-confirmed "no sandbox,
same trust level as CLI mode" for `run_shell`) means a reviewer can tell
the difference between "this looks risky because nobody thought about it"
and "this looks risky because that's the accepted tradeoff" — which is
the harder and more valuable thing for a security-adjacent tool like this
to get right.

