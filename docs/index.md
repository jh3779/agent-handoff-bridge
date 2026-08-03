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

## Operator Docs

- [Workflow Guide](workflow-guide.md): common day-to-day workflows.
- [CLI Reference](cli-reference.md): command summary for local scripts.
- [Architecture](architecture.md): how the files and command flows fit
  together.
- [Korean Operator Guide](ko-operator-guide.md): Korean setup and instruction
  templates for phone-based operation and handoff.
- [Security Model](security-model.md): safety boundaries and operational risks.

## Remote And Mobile

- [Mobile App Remote Guide](mobile-app-remote-guide.md): official phone-based
  Codex and Claude Code control paths.
- [Research Notes](research.md): source-backed research and implementation
  rationale.

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

After that, direct work from the CLI, ChatGPT mobile **Remote**, or Claude app
**Code**. Every instruction should identify the target provider and model.
