# Research: API-Key Mode (Phase 4, resolves CFL-12)

Date: 2026-08-05

## Bottom Line

There is no drop-in replacement for `run_provider()`'s subprocess model that
just swaps "local CLI" for "API key." Both vendors' direct APIs are raw
chat/completion primitives — they do not read or edit the user's actual
workspace files the way `codex exec` / `claude -p` do. To get file-editing
parity with CLI mode via API key, this project would have to build its own
tool-use loop (file read/write/edit tools, a shell-exec tool, a turn loop that
feeds tool results back to the model) — essentially reimplementing a slice of
what the CLIs already do internally. That is a materially larger scope than
"Phase 4 adds a second way to authenticate," and needs an explicit decision
before design (see "Open Scope Question" below).

Neither vendor's "hosted agent that touches your repo without a local CLI"
product (Claude Code routines, Codex cloud tasks) is usable as a drop-in
either — both are fire-and-forget, require pre-configuration through the
vendor's own web UI, and don't stream results back through the API call that
starts them.

## Anthropic: Messages API

- The Messages API (`POST /v1/messages`) is stateless per call. There is no
  server-side "session" — the caller resends the full message history on
  every call. This is a different model from `claude --resume`/
  `--session-id`, which resume server-tracked CLI session state.
- Error taxonomy (from the platform docs): `invalid_request_error` (400),
  `authentication_error` (401), `billing_error` (402), `permission_error`
  (403), `not_found_error` (404), `conflict_error` (409),
  `request_too_large` (413), `rate_limit_error` (429), `api_error` (500),
  `timeout_error` (504), `overloaded_error` (529). This is a usable
  replacement for the CLI-side signal set `docs/research.md` built around
  (`rate_limit`, `overloaded`, `authentication_failed`, `billing_error`,
  `max_output_tokens`) — the shapes map cleanly, just over HTTP status +
  JSON body instead of a `StopFailure` hook payload.
- Official SDKs retry transient failures (connection errors, rate limits,
  5xx) with exponential backoff, honoring `retry-after`. A hand-rolled
  stdlib `urllib`-based client (no new dependency) would need to replicate at
  least basic backoff/retry itself.
- Streaming (SSE) has its own mid-stream error-event mechanism, separate
  from top-level HTTP error handling — relevant if the web UI keeps its
  current "assistant reply streams into the chat thread" feel.
- Request size cap: 32MB per Messages API call — relevant if this project
  ever wants to send large attachments inline rather than referencing them.
- Tool use exists (function-calling: caller declares a tool schema, model
  requests a call, caller executes it and returns the result) but Anthropic
  does not ship a "edit this file on disk" or "run this shell command" tool
  — those would be tools this project defines and executes itself.

## Anthropic: Claude Code Routines (`routines-fire`, EXPERIMENTAL)

Found while researching a "run an actual agentic Claude Code session via
API" path — it exists, but doesn't fit this project's shape:

- `POST /v1/claude_code/routines/{routine_id}/fire` starts a real Claude
  Code agentic coding session (reads/edits files, runs commands) on
  Anthropic-managed cloud infrastructure — not a raw chat completion.
- Requires a "routine" pre-created through the claude.ai/code/routines web
  UI (saved prompt + repo + connector config). There is no API to create or
  configure a routine from this bridge; the routine must already exist.
- Auth is a per-routine bearer token (`sk-ant-oat01-...`), not a general
  Claude Platform API key — it's scoped to firing exactly that one routine.
- Fire-and-forget: the response is just
  `{claude_code_session_id, claude_code_session_url}`. No streaming, no
  polling for the transcript via API — results are viewed at the returned
  `claude.ai/code/...` URL in a browser.
- No idempotency key; retrying a request creates a duplicate session.
- Requires a claude.ai Pro/Max/Team/Enterprise plan with "Claude Code on the
  web" enabled, billed against Claude Code subscription usage, not Platform
  API usage — a different billing relationship than "paste an API key."
- Conclusion: this does not match SCR-06 / `components.html` §14's assumed
  UX (paste a key, chat in this app's own thread). It's a different product
  aimed at "kick off cloud coding sessions you watch on claude.ai," not
  "authenticate this app's existing chat UI with a key instead of a CLI
  login."

## OpenAI: Responses API (Codex-equivalent)

- OpenAI's current recommended primitive for new integrations is the
  Responses API, positioned as the agentic-capable evolution of Chat
  Completions. Built-in tools include web search, file search, code
  interpreter, image generation, remote MCP servers, and a newer
  containerized shell tool (runs arbitrary shell commands, not just Python,
  inside an OpenAI-managed sandboxed container/filesystem — not the user's
  actual local workspace).
- Custom function-calling works the same way as Anthropic's: the caller
  declares tool schemas, OpenAI's model requests calls, the caller executes
  them and returns results. No built-in "edit a file in this specific local
  directory" tool — same gap as the Anthropic side.
- OpenAI's actual "agent that edits your real repo" product is Codex cloud
  tasks: each task runs in a separate cloud environment preloaded with the
  user's repository, where the agent reads/edits files, runs tests, and
  invokes checks. This is the same shape of mismatch as Claude Code
  routines — a hosted, pre-configured, fire-and-forget agent session, not
  something this bridge's `run_provider()` could call synchronously in
  place of `codex exec`.
- Did not find evidence of an OpenAI-side "session ID you resume across
  process invocations" concept equivalent to `codex exec resume` reachable
  through a plain API-key HTTP call — Responses API conversation
  continuation is the same "client resends prior turns/response ID" shape
  as Anthropic's Messages API, not a persistent server-side session file.

## Credential Storage (stdlib-only constraint)

No option here needs a new pip dependency; this project already treats
"shell out to a platform-provided binary via `subprocess`" as an acceptable
stdlib-only pattern (that's how `codex`/`claude` are invoked today), so the
same style extends naturally to key storage:

- **OS keychain via subprocess to a platform binary** — macOS `security`
  (Keychain Access), Windows `cmdkey`/DPAPI (would need `ctypes`, no stdlib
  wrapper), Linux `secret-tool` (libsecret — not guaranteed installed on a
  minimal system). Real OS-backed encryption at rest, but three separate
  code paths with three different failure modes, and Linux coverage is not
  guaranteed. The third-party `keyring` package unifies this but is a new
  dependency this project has consistently avoided.
- **Local config file** (e.g. under
  `~/Documents/Agent Handoff Bridge/`, mirroring where `registry.json`
  already lives) — plaintext at rest unless this project also hand-rolls
  encryption, but simple, cross-platform, and consistent with the existing
  `AUTO_WORKSPACE_BASE_DIR` pattern. Needs restrictive file permissions
  (`0o600`) and must never be reachable by `scripts/scan_secrets.py`'s
  git-commit scan — which it wouldn't be, since it lives outside the repo,
  same as `registry.json` today.
- **Environment variable, no bridge-side storage** — user sets
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in their own shell/OS environment;
  the web UI reads it at request time and never persists it. Zero new
  attack surface, zero new code paths, but does not match SCR-06's "paste a
  key into a masked field in this app" UX — that mockup implies the app
  itself stores the key somewhere.
- Recommendation for the design interview: local config file with `0o600`
  permissions is the best fit for this project's existing conventions
  (same tier of trust as `registry.json`, no per-OS branching, no new
  dependency) unless the user specifically wants OS-keychain-grade
  protection enough to accept the three-platform-branch cost.

## Open Scope Question (blocks design interview)

`docs/provider-extensibility.md` already flags that `run_provider()`'s
subprocess/session-file/JSONL-stream model doesn't carry over to API-key
mode, but the research above sharpens exactly *how much* doesn't carry over:
neither vendor exposes "resume a session, edit files, run commands" behind a
plain synchronous API-key call. Phase 4 needs an explicit decision on which
of these it actually is, since they're very different sizes of project:

1. **Chat-only API-key mode.** The provider becomes a stateless
   chat responder — user messages go to Messages/Responses API, replies
   come back as text, no file edits, no shell execution. Small scope, fits
   in this phase. Loses the "provider edits your files" capability CLI mode
   has; the UI would need to make that limitation visible, not just swap
   the connection method silently.
2. **Full agentic parity via a bridge-built tool loop.** This project
   defines its own file-read/write/edit and shell-exec tool schemas, runs
   the turn loop (send message + tool schemas → model requests tool call →
   bridge executes it locally → send result back → repeat until the model
   stops), and reimplements session continuation by resending accumulated
   history. This matches CLI-mode capability but is a substantially larger
   and riskier build (a bridge-controlled shell-exec tool is a new
   sandboxing/security surface this project doesn't have today).
3. **Hosted-routine mode.** Point the UI at Claude Code routines /
   Codex cloud tasks instead of a raw API. Requires the user to
   pre-configure a routine/task in the vendor's own web UI first, and the
   bridge only fires it and links out — no in-app chat turn-by-turn like
   CLI mode has today.

This has to be resolved before wireframes/decisions can be finalized,
because it changes what SCR-06's masked-key field is actually for.

## Sources

- Anthropic: [API errors](https://platform.claude.com/docs/en/api/errors)
- Anthropic: [Trigger a Claude Code routine](https://platform.claude.com/docs/en/api/claude-code/routines-fire)
- OpenAI: [New tools and features in the Responses API](https://openai.com/index/new-tools-and-features-in-the-responses-api/)
- OpenAI: [New tools for building agents](https://openai.com/index/new-tools-for-building-agents/)
- OpenAI: [Using tools](https://developers.openai.com/api/docs/guides/tools)
- OpenAI: [Migrate to the Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- Wikipedia: [OpenAI Codex (AI agent)](https://en.wikipedia.org/wiki/OpenAI_Codex_(AI_agent))
