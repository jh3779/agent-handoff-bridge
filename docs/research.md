# Research: Claude Code CLI / Codex CLI Handoff

Date: 2026-08-03

## Bottom Line

I did not find an official feature that lets Claude Code CLI and Codex CLI share
one live conversation or automatically transfer remaining provider tokens. The
workable setup is a bridge layer:

1. Keep state in a shared workspace file.
2. Run each CLI through its non-interactive/scriptable interface.
3. Capture machine-readable output, session IDs, usage/error events, and raw
   logs.
4. On quota, rate-limit, auth, context, or max-output failure, generate a
   handoff prompt and invoke the other CLI.

## Useful Codex CLI Capabilities

- `codex exec` is designed for scripts and CI-style use.
- `codex exec --json` emits JSONL events such as `thread.started`,
  `turn.completed`, `turn.failed`, `item.*`, and `error`.
- `turn.completed` includes token usage fields.
- `codex exec resume <SESSION_ID>` or `codex exec resume --last` can continue a
  prior non-interactive run.
- `codex mcp-server` exposes Codex through MCP with tools to start a Codex
  session and reply to an existing thread.
- Codex lifecycle hooks can run scripts at events such as `SessionStart`,
  `PreCompact`, `PostCompact`, `Stop`, and `SessionEnd`.
- `AGENTS.md` is the durable instruction file Codex reads at startup.

## Useful Claude Code Capabilities

- `claude -p` runs Claude Code non-interactively.
- `--output-format json` and `--output-format stream-json` make CLI output
  scriptable.
- `--continue`, `--resume`, `--session-id`, and `--fork-session` support
  continuing or branching sessions.
- `--max-budget-usd` and `--max-turns` can intentionally cap a scripted run.
- Claude Code hooks include `StopFailure`, which reports API failure types such
  as `rate_limit`, `overloaded`, `authentication_failed`, `billing_error`,
  `max_output_tokens`, and `unknown`.
- Claude Code can run as an MCP server with `claude mcp serve`, but that server
  primarily exposes Claude Code tools to another MCP client. For direct
  Claude-agent execution, the CLI or Agent SDK is the cleaner bridge path.
- `CLAUDE.md` is the durable project instruction file Claude Code reads.

## Practical Limitations

- Subscription/account "remaining tokens" is not exposed as a stable,
  provider-neutral machine API by both CLIs. A bridge should detect failure and
  near-context signals rather than rely on a perfect remaining-token counter.
- Interactive TUI sessions are harder to automate safely than non-interactive
  runs. The bridge should prefer `codex exec` and `claude -p`.
- Fully automatic fallback can spend tokens twice if the first provider fails
  late. The bridge therefore defaults to preview mode and requires `--execute`.
- Auth files such as `~/.codex/auth.json` and Claude tokens must never be copied
  into the repo or logs.

## Recommended Architecture

```text
User task
   |
   v
.handoff/current.md  <----> AGENTS.md / CLAUDE.md
   |
   v
handoff_bridge.py
   |
   +--> codex exec --json
   |       captures thread_id, usage, final message, errors
   |
   +--> claude -p --output-format stream-json
           captures session_id, result, cost/usage if present, errors
```

The next CLI does not need the full previous transcript. It needs the current
workspace, git status, shared handoff packet, and a concise latest summary.

## Implementation Plan

1. MVP:
   - create `AGENTS.md`, `CLAUDE.md`, `.handoff/current.md`;
   - add a bridge command that can diagnose local CLI/auth state;
   - add dry-run command previews.
2. Scripted execution:
   - run Codex with `codex exec --json -`;
   - run Claude with `claude -p --output-format stream-json`;
   - parse session IDs, usage, final text, and error signals.
3. Fallback:
   - classify likely handoff causes: `rate_limit`, `quota`, `billing`,
     `context_limit`, `max_output_tokens`, `auth`, `overloaded`;
   - write `.handoff/next-prompt.md`;
   - optionally invoke the other provider with `--auto-fallback`.
4. Hooks:
   - add optional Claude `StopFailure` hook to write handoff state;
   - add optional Codex `Stop`/`PostCompact`/`SessionEnd` hooks to write handoff
     summaries.
5. Hardening:
   - cap log sizes;
   - redact secrets;
   - support provider-specific command overrides in a config file;
   - add integration tests with fake `claude` and `codex` binaries.

## Sources

- OpenAI: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- OpenAI: [Use Codex with the Agents SDK / MCP server](https://learn.chatgpt.com/docs/mcp-server)
- OpenAI: [Codex hooks](https://learn.chatgpt.com/docs/hooks)
- OpenAI: [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- Anthropic: [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- Anthropic: [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- Anthropic: [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- Anthropic: [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
