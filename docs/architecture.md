# Architecture

The project is intentionally file-centered. It does not try to merge
provider transcripts together. Instead, it makes the workspace itself the
durable handoff surface.

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
        |                    |
        |                    +--> gemini --output-format json
        |
        v
.handoff/current.md + shared docs + git workspace
```

Gemini was added in Phase 5
([docs/design-system/roadmap.md](design-system/roadmap.md)) as this
project's first test of whether the two-provider assumption baked into
earlier code actually generalized — see
[docs/provider-extensibility.md](provider-extensibility.md) for what had
to change (`other_provider()` → `next_provider()`) and what didn't
(`classify_handoff()`).

`handoff_desktop.py` provides the same control flow with a macOS/Windows GUI
for folder selection and mobile prompt generation.

## File Roles

- `handoff_control.py`: guided terminal controller for choosing a workspace and
  issuing instructions.
- `handoff_desktop.py`: cross-platform desktop controller for macOS and
  Windows.
- `handoff_bridge.py`: scriptable bridge that installs support files, creates
  task packets, builds provider prompts, runs providers, and records results.
- `remote_handoff_server.py`: optional HTTP task receiver for trusted
  automation experiments.
- `remote_handoff_submit.py`: optional HTTP submission client.
- `handoff_webui.py` + `webui_common.py`/`webui_workspace.py`/
  `webui_chat_storage.py`/`webui_credentials.py`/`webui_api_key_mode.py`/
  `webui_bridge_run.py`: the web UI MVP (`docs/cli-reference.md#web-ui-mvp`).
  Split by domain (structure audit, 2026-08-15) -- `handoff_webui.py` itself
  keeps only the HTTP routing layer, `AppState`/`Api`, and the process entry
  point; see each module's own docstring for what it owns.
- `AGENTS.md`: Codex-facing durable instructions.
- `CLAUDE.md`: Claude Code-facing durable instructions.
- `.handoff/current.md`: current task packet and handoff log.
- `.handoff/state.json`: local runtime state; ignored by git.
- `.handoff/runs/`: raw provider outputs; ignored by git.
- `.handoff/shared-context.md` (DEC-27): free-form, user-authored project
  context -- not git-ignored (meant to travel with the project); reaches
  every provider call regardless of mode, see
  [webui-chat-storage.md § Shared Project Context](webui-chat-storage.md#shared-project-context-dec-27).

## Prompt Construction

`handoff_bridge.py run` builds a prompt with:

- active provider/model metadata;
- user prompt for the current turn;
- `docs/agent-targeting-protocol.md`;
- `docs/shared-agent-contract.md`;
- `docs/verification-playbook.md`;
- `.handoff/current.md`;
- `.handoff/shared-context.md`, when non-empty (DEC-27);
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

The bridge (`handoff_bridge.py`) does not copy credentials, auth files,
provider transcripts, or hidden app state. Each provider continues from
files on disk and the current git state. This remains true of the bridge
itself — the one deliberate exception lives one layer up, in the Web UI's
Phase 4 API-key mode (`webui_credentials.py`), which stores a provider
credential for CLI-less use (including a custom provider's, DEC-26). See
[Security Model § Credential Boundaries](security-model.md#credential-boundaries)
for what that exception actually stores, where, and why.

`.handoff/state.json` and `.handoff/current.md` are written through
`atomic_write_text()` under a cross-process `WriteLock` so a torn write can't
happen even when `remote_handoff_server.py` runs multiple task subprocesses
against the same workspace concurrently. See
[Quality Gates](quality-gates.md#rule-shared-state-files-are-written-atomically-and-under-lock)
for what this does and does not guarantee.

API-key mode (`webui_api_key_mode.py`'s `run_provider_via_api_key()`)
appends a record to `.handoff/current.md` after every turn too
(`_append_api_key_mode_record()`) — added 2026-09-02 to close a real
continuity gap a production audit found: that mode's tool loop
(CFL-17/DEC-21) can write/edit files and run shell commands, and before
this fix nothing recorded that a next CLI/mobile handoff could read. It
does not touch `.handoff/state.json` — that file's actual content
(`last_provider`/auto-fallback bookkeeping) has no API-key-mode
equivalent to record, since this mode has no provider-managed session or
auto-fallback-to-the-other-provider concept. Because this runs in-process
inside the Web UI's threaded HTTP server (unlike CLI mode, which always
shells out to a `handoff_bridge.py` subprocess specifically to avoid a
shared-`cwd` race across concurrent requests — see
[Quality Gates](quality-gates.md#rule-shared-state-files-are-written-atomically-and-under-lock)),
it builds its own workspace-parameterized version of the same
`append_current()`-shaped block by hand rather than calling
`handoff_bridge.append_current()` directly, but contends for the exact
same `.handoff/.write.lock` file so a concurrent CLI-mode run against the
same workspace still serializes correctly with it instead of racing.

The same `WriteLock` (imported directly from `handoff_bridge`, not
reimplemented) guards the Web UI MVP's local chat log at
`.handoff/webui/chat/` (`webui_chat_storage.py`'s `append_chat_message()` and
`archive_old_months()`) — this repo's storage-policy stance (shared
`.handoff/` state, locked/atomic writes, gitignored runtime data) applies to
that store the same way it applies to `state.json`/`current.md`. Full
schema, atomicity, retention, and git-visibility details for that specific
store: [Web UI Chat Storage](webui-chat-storage.md).

## Recommended Extension Points

- Add project-specific instructions to the target project's own `AGENTS.md` and
  `CLAUDE.md`.
- Add project verification commands to `docs/verification-playbook.md`.
- Use hook examples in `examples/` after reviewing provider trust flows.
- Use the custom HTTP remote scripts only inside a trusted network or local
  tunnel.
