# Shared Agent Contract

This is the single source of truth for both Codex CLI and Claude Code CLI when
they work through this handoff bridge. Model style may differ, but the process,
quality bar, and stopping criteria must stay consistent.

## Priority Order

1. User's latest instruction.
2. Safety, permissions, and non-destructive workspace handling.
3. This shared contract.
4. `.handoff/current.md`.
5. Project-local conventions and existing code patterns.
6. Provider-specific defaults.

If two instructions conflict, follow the higher-priority source and record the
conflict in `.handoff/current.md`.

## Work Direction

- Treat the workspace files as the source of truth, not the previous provider's
  hidden transcript.
- Continue from the latest committed and uncommitted files on disk.
- Keep work scoped to the active task. Do not add broad refactors, dependencies,
  formatting churn, or unrelated cleanup unless the task requires it.
- Prefer boring, maintainable solutions over clever ones.
- Use repo-native tools and established patterns before introducing new
  structure.
- Preserve user changes. If a file has unrelated edits, work around them and do
  not revert them.

## Start Of Turn Checklist

- Read `.handoff/current.md`.
- Read this contract, `docs/agent-targeting-protocol.md`, and
  `docs/verification-playbook.md`.
- Run or inspect `git status --short`.
- Confirm the intended provider/model from the latest instruction header or
  `.handoff/current.md`.
- Identify the current goal, known blockers, and the smallest useful next step.
- If continuing after a failure, classify it as one of: `quota`, `rate_limit`,
  `auth`, `billing`, `context_limit`, `overloaded`, `tool_failure`, or
  `unknown`.

## Implementation Standard

- Make the minimum complete change that satisfies the task.
- Keep interfaces stable unless the user asked for a breaking change.
- Prefer structured parsers and existing APIs over fragile string operations.
- Add comments only when they explain non-obvious intent or risk.
- Do not commit secrets, auth files, raw provider credentials, or large logs.
- Keep generated handoff logs in `.handoff/runs/`.

## Output Standard

Every provider should leave the next provider with the same shape of summary:

- `Target`: provider/model/account/workspace used for this run.
- `Changed`: files or behavior changed.
- `Verified`: checks run and their result.
- `Remaining`: unfinished work, if any.
- `Blocked`: quota/auth/tool/context blockers, if any.
- `Next`: the next safe action.

When a machine-readable summary is needed, use
`schemas/handoff-summary.schema.json`.

If no code or file change was made, say that clearly.

## Completion Criteria

A task is done only when:

- the requested behavior or artifact exists;
- relevant checks from `docs/verification-playbook.md` passed or are explicitly
  marked as not applicable;
- the active provider/model is recorded in `.handoff/current.md`;
- `.handoff/current.md` has been updated with a concise final state;
- the workspace can be picked up by the other CLI without hidden context.

## Handoff Criteria

Handoff to the other provider when:

- the current provider hits quota, rate limit, billing, auth, context, or
  max-output failure;
- tool execution is repeatedly blocked and the other provider may have a
  usable path;
- the user explicitly asks to switch providers;
- the provider cannot continue within the current context.

Before handoff, update `.handoff/current.md` and generate or refresh
`.handoff/next-prompt.md`.
