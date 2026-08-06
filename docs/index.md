# Documentation Index

This repo prepares a shared operating environment for Codex CLI, Claude Code
CLI, and official mobile remote control surfaces. Start here when you need to
understand what exists, where to configure it, and how to run it without hidden
context.

## First Read

- [Preflight Setup Guide](preflight-setup-guide.md): account, host, app, and
  workspace setup before remote or phone-based use.
- [Agent Targeting Protocol](agent-targeting-protocol.md): the required
  provider/model header for every task change or handoff.
- [Shared Agent Contract](shared-agent-contract.md): the common working
  standard both agents must follow.
- [Verification Playbook](verification-playbook.md): checks and acceptance
  criteria.
- [Quality Gates](quality-gates.md): every rule this repo actually enforces
  (branch naming, secret scanning, failure-classification consistency,
  minimum test coverage, atomic shared-state writes) and how each is checked.

## Operator Docs

- [Workflow Guide](workflow-guide.md): common day-to-day workflows.
- [CLI Reference](cli-reference.md): command summary for local scripts.
- [Platform Setup](platform-setup.md): macOS and Windows launcher setup.
- [Architecture](architecture.md): how the files and command flows fit
  together.
- [Korean Operator Guide](ko-operator-guide.md): Korean setup and instruction
  templates for phone-based operation and handoff.
- [Security Model](security-model.md): safety boundaries and operational risks.
- [Release Process](release-process.md): how to cut a tagged release with
  both source zips (terminal/CLI use) and Tauri desktop installers (GUI
  use) attached.
- [Release Notes](release-notes.md): what changed in each version.
- [Design System Docs](design-system/README.md): wireframes for the desktop
  GUI and terminal menu, plus the end-to-end handoff workflow diagram.
- [v0.2 Roadmap](design-system/roadmap.md): phased plan from the current
  local-only MVP to the full provider-connected, multi-project production app.
- [Web UI Chat Storage](webui-chat-storage.md): the on-disk data model for
  local chat history — schema, atomicity, archiving, git visibility.
- [Provider Extensibility](provider-extensibility.md): what it actually
  takes to add a new AI provider (CLI-based or API-key-based) beyond Codex
  and Claude Code.

## Remote And Mobile

- [Mobile App Remote Guide](mobile-app-remote-guide.md): official phone-based
  Codex and Claude Code control paths.
- [Research Notes](research.md): source-backed research and implementation
  rationale.
- [Research: API-Key Mode](research-api-key-mode.md): Phase 4's
  pre-implementation research into Anthropic/OpenAI direct-API capabilities
  and credential storage options.
- [Research: Gemini CLI](research-gemini-cli.md): Phase 5's
  pre-implementation research into Gemini CLI's non-interactive mode,
  JSON output, session resume, auth, and hooks.
- [Research: Framework Migration](research-phase7-framework.md): Phase 7's
  pre-implementation research into Tauri vs Electron, Python-backend
  sidecar support, auto-update against a private repo, and code signing.

## Templates And Machine Contracts

- [`.handoff/task-template.md`](../.handoff/task-template.md): task packet
  structure.
- [`schemas/handoff-summary.schema.json`](../schemas/handoff-summary.schema.json):
  machine-readable summary shape.

## Typical Setup

```bash
python3 handoff_bridge.py check
python3 handoff_bridge.py diagnose
python3 handoff_bridge.py --workspace /path/to/project install
python3 handoff_bridge.py --workspace /path/to/project init "Describe the task"
```

Contributors working on this repo itself (not a downstream project) should
also run `scripts/install_git_hooks.sh` once — see
[Quality Gates](quality-gates.md).

After that, direct work from the CLI, ChatGPT mobile **Remote**, or Claude app
**Code**. Every instruction should identify the target provider and model.
