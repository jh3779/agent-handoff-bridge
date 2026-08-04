# Claude Code / Codex CLI Handoff Bridge

Claude Code CLI and Codex CLI do not currently expose a single official
"share my remaining tokens and continue in the other CLI" switch. This repo is
a small bridge scaffold for the practical version: keep work state in shared
files, run either CLI in a scriptable mode, detect quota/rate/context failures,
then hand the task to the other CLI with the current workspace state.

## Download

No `git clone` required. Grab a zip from
[GitHub Releases](https://github.com/jh3779/agent-handoff-bridge/releases/latest)
(this repo is private, so the link only resolves for accounts with access),
unzip it, and follow the `START_HERE_MACOS.txt` / `START_HERE_WINDOWS.txt`
file inside. Verify the download without spending any provider tokens:

```bash
python3 handoff_bridge.py --version
python3 handoff_bridge.py check
```

Both commands work from the extracted zip with no git repo present. See
[docs/release-process.md](docs/release-process.md) for how releases are cut,
and [docs/release-notes.md](docs/release-notes.md) for what changed.

## Current Local Status

- Requires `codex`, `claude`, and `gh` for the full local workflow.
- Run `python3 handoff_bridge.py diagnose` to inspect local paths and auth.
- Run `claude auth login` before using Claude as an automatic fallback.

## Quick Start

Open the task controller:

```bash
python3 handoff_control.py
```

Open the cross-platform desktop controller with folder selection:

```bash
python3 handoff_desktop.py
```

macOS launcher:

```bash
./launchers/macos/handoff-bridge.command
```

Windows launcher:

```bat
launchers\windows\handoff-bridge.cmd
```

Try the v0.2 chat-style redesign (file browsing, drag/click attaching,
VS Code-style Open Folder, local per-folder chat history, real Codex/Claude
calls with auto-fallback since Phase 1, and — as of Phase 2 — `--workspace`
is optional, auto-creating a folder under `~/Documents/Agent Handoff
Bridge/` from your first message if you don't pick one):

```bash
pip install pywebview   # optional, for a real app window instead of a browser tab
python3 handoff_webui.py --workspace /path/to/project   # or omit --workspace entirely
```

Opens as a native app window (falls back to a browser tab automatically if
`pywebview` isn't installed). See
[Web UI (MVP)](docs/cli-reference.md#web-ui-mvp) for what it does and does
not do yet, and [docs/design-system/](docs/design-system/README.md) for the
full redesign this is the first slice of.

Build macOS and Windows zip packages:

```bash
python3 scripts/package_platforms.py
```

For phone-based instructions, prefer the official app remote features first:

- Codex: ChatGPT mobile app -> **Remote**.
- Claude Code: Claude mobile app -> **Code** or `claude.ai/code`.

Before sending phone instructions, complete
[docs/preflight-setup-guide.md](docs/preflight-setup-guide.md)
and use the header in
[docs/agent-targeting-protocol.md](docs/agent-targeting-protocol.md).

See [docs/mobile-app-remote-guide.md](docs/mobile-app-remote-guide.md).

Inspect the local setup without using model tokens:

```bash
python3 handoff_bridge.py diagnose
```

Create a handoff packet for a task:

```bash
python3 handoff_bridge.py init "Implement the requested feature and keep tests passing" --primary codex --target-model "app-selected default"
```

Install the handoff files into another project folder:

```bash
python3 handoff_bridge.py --workspace /path/to/project install
```

Preview what would be sent to Codex without spending tokens:

```bash
python3 handoff_bridge.py run codex "Start the task"
```

Actually run Codex:

```bash
python3 handoff_bridge.py run codex --execute --instruction-type continue "Start the task"
```

Run whichever provider should go next, and let the bridge switch providers if it
detects a likely quota/rate/context/auth failure:

```bash
python3 handoff_bridge.py run auto --execute --auto-fallback --instruction-type continue "Continue the task"
```

Run once through the controller for a selected folder:

```bash
python3 handoff_control.py --workspace /path/to/project "Implement the requested feature"
```

## How The Handoff Works

- `.handoff/current.md` is the shared task packet.
- `.handoff/state.json` stores provider session IDs and the last run status.
- `.handoff/runs/<timestamp>/` stores raw stdout/stderr for each CLI run.
- `handoff_control.py` is the task controller for choosing a folder and issuing
  work.
- `docs/shared-agent-contract.md` defines the common work direction, quality
  bar, output shape, and handoff criteria.
- `docs/preflight-setup-guide.md` defines account, host, app, and workspace
  setup before remote use.
- `docs/agent-targeting-protocol.md` defines the provider/model header for
  every task change and handoff.
- `docs/verification-playbook.md` defines the common verification routine.
- `schemas/handoff-summary.schema.json` defines the shared machine-readable
  final summary shape.
- `AGENTS.md` gives Codex durable repo instructions that point at the shared
  contract.
- `CLAUDE.md` gives Claude Code durable repo instructions that point at the
  shared contract.
- `examples/` contains optional hook settings for recording handoff events from
  interactive sessions.

The bridge intentionally starts in preview mode. Add `--execute` only when you
want to spend tokens.

## Documentation

- [Documentation Index](docs/index.md)
- [Platform Setup](docs/platform-setup.md)
- [Architecture](docs/architecture.md)
- [CLI Reference](docs/cli-reference.md)
- [Workflow Guide](docs/workflow-guide.md)
- [Korean Operator Guide](docs/ko-operator-guide.md)
- [Security Model](docs/security-model.md)
- [Quality Gates](docs/quality-gates.md)
- [Release Notes](docs/release-notes.md)

## Optional Hook Setup

The hook examples are not active by default:

- `examples/claude-settings.handoff.json`
- `examples/codex-hooks.handoff.json`

Use them as references after reviewing the commands. Both Claude Code and Codex
require hook trust/review flows before project hooks should run.

## Optional Custom HTTP Remote

Official mobile remote features are the recommended path for phone-based
instructions. For trusted automation experiments, this repo also includes:

- `remote_handoff_server.py`: local HTTP task receiver.
- `remote_handoff_submit.py`: JSON task submission client.

Start the server in preview-only mode:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765
```

Submit a preview task:

```bash
python3 remote_handoff_submit.py --workspace /path/to/project --wait "Inspect the handoff setup"
```

Start the server with `--allow-execute` only when remote requests are allowed to
spend provider tokens.

## Consistency Checks

Run the no-token validation suite:

```bash
python3 handoff_bridge.py check
```

This confirms the shared contract, documentation set, provider instruction
files, JSON examples, and Python scripts are internally consistent, that no
likely secrets are tracked, that the handoff failure classification matches
the contract, and that `tests/` passes.

## Quality Gates And Branch Naming

This repo enforces branch naming (`type/short-description`), secret
scanning, failure-classification consistency, and a minimum unit test bar —
see [docs/quality-gates.md](docs/quality-gates.md) for the full rule set and
how each is checked. Install the local git hooks once per clone so they run
automatically on commit/push:

```bash
./scripts/install_git_hooks.sh
```

The same rules run in CI on every pull request
(`.github/workflows/ci.yml`).

## Research

See [docs/research.md](docs/research.md) for the
source-backed research notes and implementation plan.
