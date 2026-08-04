# CLI Reference

All commands are safe preview commands unless `--execute` or
`--allow-execute` is present.

## `handoff_bridge.py`

### Diagnose

```bash
python3 handoff_bridge.py diagnose
```

Checks local CLI paths and auth status for Codex and Claude Code.

### Install Into A Workspace

```bash
python3 handoff_bridge.py --workspace /path/to/project install
```

Installs shared handoff files into the selected project. Existing files are
preserved unless `--force` is provided.

```bash
python3 handoff_bridge.py --workspace /path/to/project install --force
```

### Initialize A Task

```bash
python3 handoff_bridge.py --workspace /path/to/project init \
  "Implement the requested feature" \
  --primary codex \
  --target-model "app-selected default"
```

Creates `.handoff/current.md` and `.handoff/state.json` for a new task.

### Preview A Run

```bash
python3 handoff_bridge.py --workspace /path/to/project run auto \
  --instruction-type continue \
  "Continue the task"
```

Builds `.handoff/next-prompt.md` without calling a model provider.

### Execute A Run

```bash
python3 handoff_bridge.py --workspace /path/to/project run auto \
  --execute \
  --auto-fallback \
  --instruction-type continue \
  "Continue the task"
```

Calls the selected provider and records the result. Use deliberately because it
may spend provider tokens.

### Model Labels And Overrides

```bash
python3 handoff_bridge.py run codex --model "app-selected default" "Preview"
```

Records the model label only.

```bash
python3 handoff_bridge.py run codex --model "exact-model-id" "Preview"
```

Records and passes the exact model ID as a provider override.

### Check

```bash
python3 handoff_bridge.py check
```

Runs no-token consistency checks: required files, JSON, Python syntax, the
secret scan, handoff failure-classification consistency, and `tests/`. See
[Quality Gates](quality-gates.md).

## Quality Gate Scripts

```bash
python3 scripts/scan_secrets.py [--staged]
python3 scripts/check_branch_name.py [branch]
./scripts/install_git_hooks.sh
```

`scan_secrets.py` and the test suite run automatically as part of
`handoff_bridge.py check`. `check_branch_name.py` and the git hooks are this
repo's own contribution rules and are not installed into downstream
projects — see [Quality Gates](quality-gates.md).

## `handoff_control.py`

Open the guided menu:

```bash
python3 handoff_control.py
```

Run a one-shot preview setup:

```bash
python3 handoff_control.py --workspace /path/to/project \
  --provider auto \
  --primary codex \
  --model "app-selected default" \
  "Implement the requested feature"
```

Execute through the controller:

```bash
python3 handoff_control.py --workspace /path/to/project \
  --execute \
  "Implement the requested feature"
```

The controller asks for confirmation before spending tokens unless `--yes` is
also supplied.

## `handoff_desktop.py`

Open the desktop controller:

```bash
python3 handoff_desktop.py
```

macOS:

```bash
./launchers/macos/handoff-bridge.command
```

Windows:

```bat
launchers\windows\handoff-bridge.cmd
```

The desktop controller exposes the same bridge actions with folder selection,
mobile prompt generation, and preview-only remote server startup.

## Optional HTTP Remote

Start a preview-only local server:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765
```

Submit a preview task:

```bash
python3 remote_handoff_submit.py \
  --url http://127.0.0.1:8765 \
  --workspace /path/to/project \
  --wait \
  "Inspect the handoff setup"
```

Allow remote requests to call providers:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765 --allow-execute
```

Do this only for trusted automation.

## Web UI (MVP)

```bash
python3 handoff_webui.py --workspace /path/to/project
```

Chat redesign from [`docs/design-system/`](design-system/README.md) — as of
Phase 1, this actually calls Codex/Claude. A local stdlib HTTP server. What
it does:

- Serves a page with a workspace file-tree sidebar and a chat-style composer.
- Lets you click a file in the tree, or drag a file onto the chat area, to
  attach it to the draft message.
- Lets you switch workspace at runtime with **Open Folder** (VS Code-style —
  a real OS folder picker in the native window; a manual absolute-path
  prompt in browser mode), instead of only being able to browse the single
  folder passed at startup via `--workspace`.
- Lets you pick a provider (`auto`/`codex`/`claude`) from the titlebar, then
  **"Send" actually runs it** — `POST /api/run` shells out to
  `handoff_bridge.py run <provider> --execute --auto-fallback` (the same CLI
  a human would type; see "Why a subprocess" below) and reads back the
  structured result from `.handoff/state.json`. Only the **first send in a
  browser session** asks "this may spend tokens, continue?" — after that,
  sends in the same session run immediately (`sessionRunConfirmed` in
  `webui/app.js`). Auto-fallback is visible in the thread as a second agent
  message from the other provider, not hidden.
- Every message — yours and the agent's — persists to
  `<workspace>/.handoff/webui/chat/YYYY-MM.jsonl`. History is scoped to the
  folder you have open, the same way `.handoff/current.md` already is, so it
  travels with the project if you copy/sync/zip the folder elsewhere. Past
  months are gzip-compressed automatically (`archive_old_months()`, run on
  startup and on every folder switch). Defaults to **not tracked by git**
  (`.handoff/webui/.gitignore`, written proactively regardless of whether
  this workspace ever ran `install`). Full schema, atomicity, and retention
  details: [Web UI Chat Storage](webui-chat-storage.md).
- Agent replies render fenced ` ```code``` ` blocks as monospace blocks;
  everything else is plain text, inserted via `textContent`/
  `createTextNode` only (never `innerHTML`) since a provider's response
  isn't fully trusted input.

**Why a subprocess, not an in-process function call**: `handoff_bridge.py`'s
state functions resolve paths like `.handoff/state.json` relative to the
*process* cwd (via `chdir_workspace()`). That's fine for a one-shot CLI
invocation but not safe to call in-process from a `ThreadingHTTPServer`
handler, where `os.chdir()` is process-wide and would race every other
in-flight request's thread. `run_provider_via_bridge()` shells out instead
— exactly what `handoff_desktop.py` already does for the same reason — and
diffs `.handoff/state.json`'s `history[]` before/after to get back the new
record(s) as structured data, including every record an auto-fallback chain
produced in that one call.

**Timeout**: the Web UI passes `--timeout-seconds 600`
(`PROVIDER_RUN_TIMEOUT_SECONDS`, `handoff_webui.py`) to `handoff_bridge.py
run`, so the 600s budget is enforced on the *actual* codex/claude
subprocess, per provider call — killing only the outer bridge wrapper
would leave a still-running, still-token-spending provider process behind,
since neither process runs in its own process group. Auto-fallback means
up to two sequential provider calls, each with its own 600s budget; the
outer `run_provider_via_bridge()` wrapper adds a second, more generous
timeout (`OUTER_SUBPROCESS_TIMEOUT_SECONDS`, `600 * 2 + 60` — the extra 60s
covers up to two rounds of `handoff_bridge.WriteLock` contention, 10s each,
on top of ordinary process-startup overhead) as a hard-kill
backstop for cases outside normal provider execution (e.g. the bridge
process itself hanging on I/O) — this one *can* leave a child process
running if it ever fires, but it's sized to rarely need to. This is a Web
UI-only limit; plain CLI `run` has no timeout by default
(`--timeout-seconds 0`). If the hard-kill backstop fires after the first
provider already produced a record but before a triggered fallback
finished, the Web UI appends a synthetic "timed out" agent message for the
fallback rather than silently showing only the first reply.

The prompt itself travels to `handoff_bridge.py` via a temporary
`--prompt-file`, not as a trailing CLI argument — a long prompt as a bare
argv value risks OS argument-length limits and is visible in the local
process list, and (found the hard way, via a CI-only failure) argparse's
handling of a positional interleaved after `--instruction-type <value>`
isn't consistent across Python versions.

By default it opens as a **native app window** (via the optional
[pywebview](https://pywebview.flowrl.com/) package) so this tests like a
real program, not a browser tab — that was an explicit requirement, not a
polish detail. If `pywebview` isn't installed, it prints a note and falls
back to opening a regular browser tab automatically; nothing breaks either
way.

```bash
pip install pywebview   # optional: for the native window; auto-detected
python3 handoff_webui.py --workspace /path/to/project
```

Flags:

```bash
python3 handoff_webui.py --workspace /path/to/project --port 8787 --host 127.0.0.1 --browser --no-browser
```

- `--port`: default `8787`.
- `--host`: default `127.0.0.1`. Keep this local-only — there is no auth.
- `--browser`: force a regular browser tab even if `pywebview` is installed.
- `--no-browser`: don't open anything automatically; just serve (useful for
  scripting or when something else will hit the HTTP endpoints directly).

`choose_ui_mode()` in `handoff_webui.py` is the pure function behind this
native/browser decision — see `tests/test_handoff_webui.py`'s
`ChooseUiModeTests` for its four cases.

Endpoints:

| Method | Path | Does |
|---|---|---|
| GET | `/api/info` | Active workspace path/name |
| GET | `/api/tree?path=` | List a directory (scoped to the workspace, symlink-escape-safe) |
| GET | `/api/file?path=` | Read a text file's content (binary refused, oversized truncated) |
| GET | `/api/chat?month=YYYY-MM` | This month's (or a given month's) chat history, plus the list of months that exist |
| POST | `/api/chat` | Append one message to the current month's log |
| POST | `/api/open-folder` | Switch the active workspace (validates the path is a real, absolute directory) |
| POST | `/api/run` | Run `provider` (`auto`\|`codex`\|`claude`) with `text` as the turn prompt; persists and returns the resulting agent message(s) |

`/api/run` is the one endpoint that reaches outside the sandbox this
server otherwise keeps itself in — it invokes a real provider CLI via
`handoff_bridge.py`, which can spend tokens and can act on the workspace
however that CLI session decides to. Everything else (`/api/tree`,
`/api/file`, `/api/chat`, `/api/open-folder`) stays inside the read/local-
write boundary described in [Web UI Chat Storage](webui-chat-storage.md),
and the workspace-scoping/symlink checks that guard `/api/tree` and
`/api/file` are covered by `tests/test_handoff_webui.py`'s traversal tests.
`/api/run` itself is covered by `RunProviderViaBridgeTests` and
`ApiRunLiveServerTests` — including a real auto-fallback chain test using
fake `codex`/`claude` scripts on `PATH` (deterministic, no tokens spent, no
network). See [Provider Extensibility](provider-extensibility.md) and the
Conflict List in
[design-system/flutter-mapping.html](design-system/flutter-mapping.html#s2)
for what's intentionally still missing (cross-project history browsing,
API-key auth, Gemini, update checks).

## Platform Packages

Build macOS and Windows zip packages:

```bash
python3 scripts/package_platforms.py
```

Build one platform:

```bash
python3 scripts/package_platforms.py --platform macos
python3 scripts/package_platforms.py --platform windows
```
