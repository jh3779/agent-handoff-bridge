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
  decision rather than starting the framework migration unprompted.
