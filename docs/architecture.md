# Architecture

The project is intentionally file-centered. It does not try to merge Codex and
Claude transcripts. Instead, it makes the workspace itself the durable handoff
surface.

## Components

```text
User / phone / CLI
        |
        v
handoff_control.py  ->  handoff_bridge.py
        |                    |
        |                    +--> codex exec --json
        |                    |
        |                    +--> claude -p --output-format stream-json
        |
        v
.handoff/current.md + shared docs + git workspace
```

## File Roles

- `handoff_control.py`: guided terminal controller for choosing a workspace and
  issuing instructions.
- `handoff_bridge.py`: scriptable bridge that installs support files, creates
  task packets, builds provider prompts, runs providers, and records results.
- `remote_handoff_server.py`: optional HTTP task receiver for trusted
  automation experiments.
- `remote_handoff_submit.py`: optional HTTP submission client.
- `AGENTS.md`: Codex-facing durable instructions.
- `CLAUDE.md`: Claude Code-facing durable instructions.
- `.handoff/current.md`: current task packet and handoff log.
- `.handoff/state.json`: local runtime state; ignored by git.
- `.handoff/runs/`: raw provider outputs; ignored by git.

## Prompt Construction

`handoff_bridge.py run` builds a prompt with:

- active provider/model metadata;
- user prompt for the current turn;
- `docs/agent-targeting-protocol.md`;
- `docs/shared-agent-contract.md`;
- `docs/verification-playbook.md`;
- `.handoff/current.md`;
- `git status --short` and `git diff --stat`.

This gives both models the same source of truth even when their private
transcripts differ.

## Provider Selection

`run auto` chooses the primary provider unless the previous run recorded
`handoff_needed`. In that case, it switches to the other provider.

Known handoff signals include:

- rate limits;
- quota or token exhaustion;
- billing/auth failures;
- context or max-output failures;
- provider overload;
- non-zero provider exit codes.

## Model Handling

The model field has two meanings:

- recording label: `app-selected default`, `provider default`, `default`, or
  `unknown`;
- exact override: a real provider model string.

Recording labels are written into `.handoff/current.md` and prompts, but are not
passed to CLI `--model`. Exact model strings are passed through.

## State Boundaries

The bridge does not copy credentials, auth files, provider transcripts, or
hidden app state. Each provider continues from files on disk and the current git
state.

## Recommended Extension Points

- Add project-specific instructions to the target project's own `AGENTS.md` and
  `CLAUDE.md`.
- Add project verification commands to `docs/verification-playbook.md`.
- Use hook examples in `examples/` after reviewing provider trust flows.
- Use the custom HTTP remote scripts only inside a trusted network or local
  tunnel.
