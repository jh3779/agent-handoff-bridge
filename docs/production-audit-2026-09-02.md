# Production Audit - 2026-09-02

Production audit: 78/100, launchable with caveats for the current single-operator/small-tester distribution, but not something I would broaden without tightening API-key-mode tool execution and fixing source-zip documentation packaging.

## Scope

- Repository: `agent-handoff-bridge`
- Branch/commit checked: `main` at `730b69e`
- Version evidence: `handoff_bridge.py` `BRIDGE_VERSION = "0.4.1"` and `src-tauri/tauri.conf.json` `"version": "0.4.1"`
- Working tree before report creation: clean
- Release surface audited: Python CLI bridge, Web UI, API-key-mode tool loop, optional remote HTTP server/client, source zip packaging, Tauri shell, CI/release documentation

## Ship Recommendation

No local launch blocker was found for the existing scope: a local-first bridge used by the operator and known testers. The core no-token validation suite passes, source zips build and pass their documented check after extraction, tracked-file secret scanning is clean, and the Tauri/Rust shell compiles when the same placeholder sidecar precondition used by CI is reproduced locally.

For broader public distribution, treat the API-key-mode tool loop as the main decision gate. It intentionally grants the model file write/edit and shell execution after the first session confirmation; that may be acceptable for a power-user local tool, but it is the wrong default for less technical users unless the UI and process isolation become stricter.

## Blockers

None from local evidence.

## High-Value Findings

### F1 - API-key mode has a powerful post-confirmation shell surface

Severity: high, accepted-by-design risk.

`webui_api_key_mode.py` documents that once the first send in a session is confirmed, the API-key-mode loop can read/write/edit workspace files and run shell commands with no per-tool confirmation (`webui_api_key_mode.py:217`, `webui_api_key_mode.py:223`, `webui_api_key_mode.py:225`). The shell tool is explicitly not a sandbox: it starts at `cwd=workspace`, but absolute paths and `..` can reach wherever the OS user account can reach (`webui_api_key_mode.py:230`, `webui_api_key_mode.py:233`). The implementation uses `subprocess.run(..., shell=True)` (`webui_api_key_mode.py:462`, `webui_api_key_mode.py:464`).

The code does cap tool iterations and command runtime, which is good. The remaining gap is that the timeout only kills the immediate subprocess, not a whole process tree, so a command that forks/backgrounds work can survive the timeout (`webui_api_key_mode.py:452`, `webui_api_key_mode.py:455`).

Recommended fix before wider rollout: add a visible "full local tool access" mode boundary, per-tool or per-command confirmation for `run_shell`, and process-group/job-object cleanup so timeouts kill descendants. If the current trust model is retained, keep it documented as an operator-grade mode rather than a general-user default.

### F2 - API-key-mode side effects are not recorded in the shared handoff packet

Severity: medium-high continuity risk.

`run_provider_via_api_key()` deliberately does not touch `.handoff/state.json` or `.handoff/current.md` (`webui_api_key_mode.py:1056`). That made sense for early chat-only API-key mode, but the same mode now has file-write/edit and shell tools. The resulting tool transcript lands in Web UI chat storage, while the repository's cross-provider source of truth remains `.handoff/current.md`.

Impact: if an API-key-mode turn modifies files and the operator later switches to CLI/mobile handoff, the next provider sees the changed files but not a durable summary of what the API-key-mode agent did, why, or what remains. That weakens this project's central handoff premise.

Recommended fix: when API-key mode executes a turn, append a compact, non-session CLI-compatible record to `.handoff/current.md` and optionally `.handoff/state.json` history, with `run_dir: null`, provider/model, status, and the transcript excerpt.

### F3 - Source zips pass checks but contain broken documentation links

Severity: medium, user-facing packaging bug.

The source zip builder includes `README.md` and top-level `docs/*.md`, but not `README.ko.md` or `docs/design-system/**` (`scripts/package_platforms.py:19`, `scripts/package_platforms.py:26`, `scripts/package_platforms.py:117`, `scripts/package_platforms.py:119`). `README.md` links to both `README.ko.md` and `docs/design-system/README.md` (`README.md:3`, `README.md:121`, `README.md:213`), and `docs/index.md` links to design-system pages (`docs/index.md:39`, `docs/index.md:41`).

I confirmed the generated macOS source zip did not contain `README.ko.md`, `docs/design-system/README.md`, or `docs/design-system/roadmap.md`, while the extracted package's `python3 handoff_bridge.py check` still passed. So this is not an executable-package failure; it is a documentation/package completeness failure.

Recommended fix: either include `README.ko.md` plus `docs/design-system/**` in source zips, or change packaged README/index links to point only at shipped docs. Add a package-content regression test for linked docs.

### F4 - Remote submit cannot disable auto-fallback

Severity: medium CLI contract bug.

`remote_handoff_submit.py` defines `--auto-fallback` as `action="store_true", default=True` (`remote_handoff_submit.py:93`). That means the parsed value is already true when the flag is absent, and there is no `--no-auto-fallback` counterpart. The client therefore always sends `"auto_fallback": true` unless code constructs arguments directly.

Impact: a remote automation caller cannot request "try only the selected provider" even though the server payload supports `auto_fallback` and `remote_handoff_server.py` respects it (`remote_handoff_server.py:305`, `remote_handoff_server.py:307`).

Recommended fix: use `argparse.BooleanOptionalAction` where available, or add an explicit `--no-auto-fallback` that sets `dest="auto_fallback"` to false.

### F5 - Optional remote server has unbounded defaults

Severity: medium-low, trusted-automation DoS risk.

The custom remote server starts worker threads per task (`remote_handoff_server.py:158`) and its default `--task-timeout` is `0`, documented as "no timeout" (`remote_handoff_server.py:325`). That flows to `short_run(..., timeout=None)` (`remote_handoff_server.py:277`). Its request reader also checks only `length <= 0`, unlike the Web UI's 2 MB request cap (`remote_handoff_server.py:162`, `remote_handoff_server.py:164`; compare `handoff_webui.py:121`, `handoff_webui.py:123`).

The server is optional, token-protected by default, and rejects `--no-auth` on non-local hosts, so this is not a default public exposure. Still, if used for automation, one bad or malicious caller can tie up threads or memory.

Recommended fix: give remote tasks a finite default timeout, add a request body limit, and document the expected operator override for unusually long runs.

### F6 - Interactive task initialization can pass `auto` as primary provider

Severity: low UX bug.

`handoff_control.py`'s interactive `initialize_task()` calls `ask_provider("codex")` and then passes that value to `handoff_bridge.py init --primary` (`handoff_control.py:87`, `handoff_control.py:89`). `ask_provider()` accepts `"auto"` because it is shared with run/preview menus, but `init --primary` only accepts real providers. The result is an avoidable CLI error if the user chooses `auto` during task creation.

Recommended fix: split `ask_primary_provider()` from `ask_provider()`, using only `BRIDGE_PROVIDERS` for init.

### F7 - `write_next_prompt()` still bypasses the shared atomic write pattern

Severity: low durability risk.

`scripts/handoff_hook.py` correctly uses `WriteLock` and `atomic_write_text()` for `.handoff/current.md`, but `write_next_prompt()` writes `.handoff/next-prompt.md` with a plain `write_text()` (`scripts/handoff_hook.py:86`, `scripts/handoff_hook.py:103`). This was already recorded as an open low-risk finding in `docs/codebase-review.md`.

Recommended fix: use the same `WriteLock`/`atomic_write_text()` pattern for `next-prompt.md`, mostly for consistency and crash-safety.

### F8 - Tauri crate metadata is still scaffold-default

Severity: low release hygiene issue.

`src-tauri/Cargo.toml` still says `name = "app"`, `version = "0.1.0"`, `description = "A Tauri App"`, `authors = ["you"]`, and empty license/repository fields (`src-tauri/Cargo.toml:1`, `src-tauri/Cargo.toml:3`, `src-tauri/Cargo.toml:4`, `src-tauri/Cargo.toml:5`). The actual Tauri bundle version is correctly set in `src-tauri/tauri.conf.json` (`src-tauri/tauri.conf.json:3`, `src-tauri/tauri.conf.json:4`), so this does not break the installer version today. It does make Rust/package metadata look unfinished.

Recommended fix: align Cargo metadata with the product name, repository, license posture, and release version policy, or document that Cargo package metadata is intentionally not release-significant.

## Strengths

- The repository has a strong no-token quality gate: required files, JSON parsing, Python compilation, handoff classification vocabulary, secret scanning, and unit tests all run through `python3 handoff_bridge.py check`.
- Tests are broad for the high-risk Python surface: path traversal, update-check states, custom providers, provider-key storage, tool-loop transcript safety, fallback classification, remote server behavior, branch naming, and secret scanning.
- Web UI file access uses resolved-root path confinement through `safe_join()`, including traversal and symlink escape protection.
- The Web UI refuses non-loopback binds because it has no authentication, and the optional remote server keeps token auth enabled by default.
- Tauri capabilities are narrow: `src-tauri/capabilities/default.json` grants only `core:default`, while updater/shell work is initiated by trusted Rust backend code rather than frontend `invoke(...)`.
- Desktop self-update uses Tauri's signed updater artifacts and requires user confirmation before install.
- The release process explicitly separates source zips and desktop installers, documents unsigned OS-level installer warnings, and includes sanity checks for source zips and installers.

## Verification Run

- `git status --short --branch`: `## main...origin/main`
- `git log --oneline --decorate -20`: recent HEAD `730b69e`, latest tag in local history `v0.4.1`
- `git diff --stat origin/main...HEAD`: no diff
- `python3 handoff_bridge.py check`: first sandboxed run failed because loopback socket bind was denied; approved re-run outside the sandbox passed, `Ran 524 tests`, `OK`
- `node --check webui/app.js`: passed
- `python3 scripts/check_branch_name.py main`: passed, `main` exempt
- `python3 scripts/scan_secrets.py --root /Users/jihun/Documents/통합cli`: passed, no likely secrets found
- `cargo test --manifest-path src-tauri/Cargo.toml`: first run failed because local placeholder sidecars were absent; after reproducing CI's placeholder sidecar precondition under `src-tauri/binaries/`, passed with 0 Rust tests
- `python3 scripts/package_platforms.py --output /private/tmp/ahb-audit-dist`: built macOS and Windows source zips
- Extracted macOS source zip, then ran `python3 handoff_bridge.py check` outside the sandbox: passed, `Ran 524 tests`, `OK`
- `python3 handoff_bridge.py check-update`: returned `{"status": "unavailable", "current_version": "0.4.1"}`
- `gh auth status`: local `gh` exists, but the active GitHub token is invalid, so live release/latest checks were not available from this environment

## Evidence Missing

- Live GitHub Release asset verification was not performed because local `gh` authentication is invalid.
- A real installed desktop app launch/update flow was not run in this audit; only Rust compilation, config inspection, and documented release-process checks were covered.
- No external dependency vulnerability audit was run. The Python runtime intentionally avoids third-party runtime dependencies, but Rust/Tauri dependencies were not checked against a live advisory source in this local-only pass.
- No visual browser or desktop screenshot pass was run for the Web UI.

## Next Actions

1. Fix source zip documentation packaging first: it is small, concrete, and user-visible.
2. Decide whether API-key-mode shell execution remains operator-grade or gets stricter UX/process isolation before any broader audience.
3. Patch the remote-submit `--no-auto-fallback` gap and give the optional remote server finite defaults.
