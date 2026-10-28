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
  `webui_chat_storage.py`/`webui_credentials.py`/
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
- a short "Working Rules" summary of the three protocol docs
  (`docs/agent-targeting-protocol.md`, `docs/shared-agent-contract.md`,
  `docs/verification-playbook.md`) — `PROTOCOL_SUMMARY` in
  `handoff_bridge.py`;
- the newest whole entries of `.handoff/current.md`, up to
  `COMPACT_CURRENT_PROMPT_CHARS` (5,000 characters), newest first;
- `.handoff/shared-context.md`, when non-empty (DEC-27);
- `git status --short` and `git diff --stat`.

This gives both models the same source of truth even when their private
transcripts differ. A turn that resumes a provider's live session sends only
the new user message, a git snapshot, and the log entries added since that
provider's last turn.

**Token cost.** This is the default "compact" mode. Measured on this
repository's own 283KB `current.md`, a handoff/cold-start prompt dropped from
about 32,700 characters (~8,200 tokens) to about 8,800 (~2,200 tokens).
`run --full-prompt` restores the previous behavior (the three docs inlined in
full, plus a 20,000-character tail of the log). Compact mode switches to full
by itself when the workspace's copy of any of those three docs differs from the
bundled one (a customised rule set must be sent as written), so the Web UI,
desktop app, and remote server need no extra flag.

**Log archiving.** `.handoff/current.md` is append-only, and an agent told to
"read `.handoff/current.md`" opens the whole file regardless of what the prompt
contains. Before each `--execute` run, once the file passes 60,000 characters
the oldest entries move to `.handoff/archive/current-<UTC timestamp>.md`
(not git-ignored: it travels with the project like `current.md` does) and about
30,000 characters of the newest stay; the live file starts with a one-line
pointer comment. Per-provider offsets in `state.json` are shifted by the same
amount and saved before the provider runs, and an offset that points past the end
of the log is treated as 0, so continuation turns never skip new entries.

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
itself, and of the Web UI layered on it (API-key mode, the one place a
credential was ever stored, was removed after v0.4.11; see
[Security Model § Credential Boundaries](security-model.md#credential-boundaries)).

`.handoff/state.json` and `.handoff/current.md` are written through
`atomic_write_text()` under a cross-process `WriteLock` so a torn write can't
happen even when `remote_handoff_server.py` runs multiple task subprocesses
against the same workspace concurrently. See
[Quality Gates](quality-gates.md#rule-shared-state-files-are-written-atomically-and-under-lock)
for what this does and does not guarantee.

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
- Add project-specific portable Agent Skills under `.agents/skills/<name>/
  SKILL.md` — see [Portable Agent Skill](#portable-agent-skill-agentsskills)
  below for why this directory (not `.claude/skills/` alone) reaches every
  provider this bridge supports.

## Portable Agent Skill (`.agents/skills/`)

`agentskills.io`'s `SKILL.md` format (originally Claude Code's, now an open
standard) is discovered natively by all three CLIs this bridge supports —
Codex CLI reads `.agents/skills/` as its primary location, Claude Code reads
`.claude/skills/` and also discovers `.agents/skills/`, and Gemini CLI
discovers `.agents/skills/`/`.gemini/skills/` as workspace-level skills. This
means a single `SKILL.md` under `.agents/skills/<name>/` reaches every
provider without any bridge-side translation.

`install_standard_files()` ships `.agents/skills/handoff-status/SKILL.md`
(2026-09-02) — a skill that teaches whichever CLI is running to read
`.handoff/current.md`, run `handoff_bridge.py status`/`diagnose` (both free,
no tokens spent) before starting or continuing work, and append a summary
before stopping. It formalizes, in a form all three CLIs discover
automatically, the same convention `AGENTS.md`/`CLAUDE.md` already state in
prose. `tests/test_agent_skills.py` covers the actual SKILL.md frontmatter
contract (required `name`/`description` fields, naming/length constraints)
and the same three-manifest consistency check (`INSTALL_FILES`/
`REQUIRED_FILES`/`COMMON_FILES`) every other installed file gets.
