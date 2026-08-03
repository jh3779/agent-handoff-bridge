# Claude Code / Codex CLI Handoff Bridge

Claude Code CLI and Codex CLI do not currently expose a single official
"share my remaining tokens and continue in the other CLI" switch. This repo is
a small bridge scaffold for the practical version: keep work state in shared
files, run either CLI in a scriptable mode, detect quota/rate/context failures,
then hand the task to the other CLI with the current workspace state.

## Current Local Status

- Requires `codex`, `claude`, and `gh` for the full local workflow.
- Run `python3 handoff_bridge.py diagnose` to inspect local paths and auth.
- Run `claude auth login` before using Claude as an automatic fallback.

## Quick Start

Open the task controller:

```bash
python3 handoff_control.py
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
- [Architecture](docs/architecture.md)
- [CLI Reference](docs/cli-reference.md)
- [Workflow Guide](docs/workflow-guide.md)
- [Korean Operator Guide](docs/ko-operator-guide.md)
- [Security Model](docs/security-model.md)
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
files, JSON examples, and Python scripts are internally consistent.

## Research

See [docs/research.md](docs/research.md) for the
source-backed research notes and implementation plan.
