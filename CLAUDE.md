# Agent Handoff Protocol

This repository is configured for handoff between Claude Code CLI and Codex CLI.

## Shared Source Of Truth

- Read `docs/preflight-setup-guide.md` before first remote/mobile use.
- Read `docs/agent-targeting-protocol.md` when task scope, provider, or model changes.
- Read `docs/shared-agent-contract.md` before planning or editing.
- Use `docs/verification-playbook.md` to choose checks and record results.
- Read `docs/quality-gates.md` before committing or pushing in this repo —
  branch naming, secret scanning, failure-classification consistency, and
  minimum test coverage are enforced, not just documented.
- Treat those two files as the shared operating standard for both providers.

## Before Work

- If `.handoff/current.md` exists, read it before making changes.
- Confirm the target provider and model from the latest instruction or
  `.handoff/current.md`.
- Check `git status --short` before editing.
- Treat the workspace as shared. Do not overwrite user or other-agent changes
  without understanding them.

## During Work

- Keep changes scoped to the active task.
- Name any new branch `type/short-description` per `docs/quality-gates.md`
  (types: feature, fix, docs, chore, refactor, test, release, hotfix).
- Prefer the existing repo conventions and tooling once a real project is added.
- If you are continuing from the other CLI, inspect `.handoff/runs/` only as
  needed; raw logs can be large.

## Before Stopping

- Follow the summary shape in `docs/shared-agent-contract.md`.
- Update `.handoff/current.md` with:
  - what changed,
  - tests or checks run,
  - remaining work,
  - blockers or quota/auth errors.
- Leave the workspace in a state the other CLI can inspect with normal file and
  git commands.
