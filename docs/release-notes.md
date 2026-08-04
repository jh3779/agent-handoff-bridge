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
