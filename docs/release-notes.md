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
  - added tests for all of the above -- run
    `python3 -m unittest discover -s tests -v` for the current pass/fail
    count rather than trusting a number pinned here; it drifts every time a
    test is added (see prior review finding on this exact line).

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
