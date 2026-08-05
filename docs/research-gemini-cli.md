# Research: Gemini CLI (Phase 5, resolves CFL-13)

Date: 2026-08-05

## Bottom Line

Gemini CLI fits the same "subprocess + parse structured output + detect
handoff signals" architecture `handoff_bridge.py` already uses for
Codex/Claude — non-interactive execution, a JSON output mode, and
session resume all exist with directly comparable shapes. Three real
differences change the implementation, not just the provider name:

1. **No `auth status`-style diagnostic command exists.** `diagnose()`
   currently runs `codex login status` / `claude auth status --text` —
   there is no Gemini equivalent to shell out to. Auth state has to be
   inferred indirectly (a real headless call, checked for a specific
   failure signal).
2. **JSON output is one object at the end, not a JSONL stream by
   default.** `--output-format json` returns a single blob after the
   run completes; a `stream-json` mode exists but its per-event field
   schema isn't documented anywhere findable, only event *names* — not
   solid enough to build `summarize_gemini()`'s event parsing on yet.
3. **No distinct signal for rate-limit vs. quota vs. context-length
   errors.** These all appear to collapse into a generic API error
   (exit code 1, JSON `error.type: "ApiError"`) — `classify_handoff()`'s
   pattern-matching approach (matching `HANDOFF_LABELS` against
   stdout/stderr text) will have to lean on message-text matching more
   than Gemini's own structured signal, unlike Codex/Claude where
   distinct JSON error types or hook payloads exist.

## Useful Gemini CLI Capabilities

- Binary: `gemini` (npm package `@google/gemini-cli`, Node.js 20+).
  `gemini --version` both confirms installation and matches this
  project's existing `shutil.which(provider)` + `--version` diagnostic
  pattern.
- `gemini -p "<prompt>" --output-format json` is the non-interactive,
  scriptable analog to `codex exec --json` / `claude -p
  --output-format stream-json`. Also auto-triggers in non-TTY contexts
  (piped stdin/stdout), and accepts piped file content as context
  (`cat file | gemini -p "..."`).
- JSON mode's response shape (confirmed with a real worked example):
  ```json
  {
    "response": "string",
    "stats": {
      "models": {"<model-name>": {"api": {...}, "tokens": {"prompt":n,"candidates":n,"total":n,...}}},
      "tools": {"totalCalls":n,"totalSuccess":n,"totalFail":n,...},
      "files": {"totalLinesAdded":n,"totalLinesRemoved":n}
    },
    "error": {"type": "ApiError|AuthError|...", "message": "string", "code": n}
  }
  ```
  No session/thread ID field appears in this schema — see "Practical
  Limitations" below.
- `--resume`/`-r` (`latest`, an index, or a session UUID),
  `--list-sessions`, `--delete-session <index>` — directly comparable
  to `codex exec resume <SESSION_ID>` / `claude --resume`. Sessions are
  stored per-project under `~/.gemini/tmp/<project_hash>/chats/`.
- `GEMINI_API_KEY` (AI Studio) or Vertex AI credentials
  (`GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION` plus
  `gcloud auth application-default login` or a service-account JSON) —
  usable headlessly without an interactive OAuth browser flow, which
  matters for a bridge invoking this non-interactively.
- `GEMINI.md` is the durable project-instruction file, directly
  analogous to `AGENTS.md`/`CLAUDE.md`. Its filename is even
  configurable (`context.fileName` in `settings.json`) to also read
  `AGENTS.md` if wanted — not something this project needs to rely on,
  since `install_standard_files()` already writes a dedicated file per
  provider, but worth knowing it exists.
- A full lifecycle hooks system — `SessionStart`, `SessionEnd`,
  `BeforeAgent`/`AfterAgent`, `BeforeModel`/`AfterModel`,
  `BeforeToolSelection`, `BeforeTool`/`AfterTool`, `PreCompress`,
  `Notification` — richer than Codex's or Claude Code's hook sets.
  `PreCompress` is the closest analog to Codex's `PreCompact`. Not
  needed for the current subprocess-per-call architecture (this
  project doesn't use Codex/Claude's hooks for that either), but
  available if a future phase wants push-based signals instead of
  parsing stdout after the fact.
- `--model`/`-m` (default `auto`). Confirmed current model IDs:
  `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`,
  `gemini-3-pro-preview`, `gemini-3-flash-preview`. No default is
  planned here for the same reason Phase 4 ended up requiring an
  explicit model for both Anthropic/OpenAI API-key mode — model IDs
  drift and a wrong hardcoded one fails silently confusingly; `auto`
  itself is a valid `--model` value if a default is wanted without
  pinning a specific generation.
- Gemini CLI **consumes** MCP servers (`gemini mcp add/list/remove`)
  but does not expose itself as one — no `gemini mcp-server` command
  exists. The closest "drive it programmatically" surface is `--acp`
  (Agent Client Protocol, JSON-RPC over stdio for IDE integrations) —
  a different protocol, not relevant to this project's subprocess
  model.

## Practical Limitations

- **No `gemini auth status` equivalent.** `diagnose()`'s existing
  pattern (run a status subcommand, print its output) has nothing to
  call for Gemini. The practical alternative found: run a trivial
  headless call (`gemini -p "ping" --output-format json`) and check for
  exit code 41 (`FatalAuthenticationError`, from
  `docs/resources/troubleshooting.md`'s table) or `error.type:
  "AuthError"` in the JSON — an actual probe, not a status query. This
  costs a real API call/token every time `diagnose()` runs, unlike the
  free `login status`/`auth status` checks Codex/Claude get. Whether
  that tradeoff is acceptable is a design question for the
  pre-implementation interview, not decided here.
- **No verified session/thread ID in the JSON response.** Codex's
  `thread.started` event and Claude's `session_id` field both surface a
  resumable ID directly in the structured output `parse_jsonl()`/
  `summarize_codex()`/`summarize_claude()` already extract. Gemini's
  `--output-format json` response shape (confirmed above) has no such
  field — `--resume latest` exists, but a *specific* prior session
  would need to be located by listing sessions
  (`--list-sessions`) or relying on `--resume latest` always being
  "the same conversation," which doesn't hold once other Gemini CLI
  activity happens outside this bridge in the same project directory.
- **Exit codes are the primary structured signal, not error-type
  variety.** Two exit-code tables exist in Gemini's own docs and don't
  fully agree with each other (a general docs page lists 0/1/42/53; a
  troubleshooting page adds 41/44/52) — using the more detailed
  troubleshooting table is the safer bet, but this is worth flagging as
  an internal documentation inconsistency on Gemini's side, not
  something this project can resolve.
- **No distinct rate-limit/quota/context-length signal was found.**
  `classify_handoff()`'s existing `ERROR_PATTERNS` regex-matching
  approach (matching text like "rate limit", "quota" against
  stdout/stderr) will likely have to carry more of the classification
  weight for Gemini than it does for Codex/Claude, where hook payloads
  and JSON error types add a second, more structured signal on top of
  text matching.
- **`stream-json` mode's per-event schema is unverified.** Only event
  *names* (`init`, `message`, `tool_use`, `tool_result`, `error`,
  `result`) are documented, no field-level shape or worked example
  found. Building `summarize_gemini()` against `--output-format json`'s
  single end-of-run object (fully documented, worked example
  confirmed) is the safer starting point; `stream-json` could be
  revisited later if its schema gets properly documented upstream.
- **Unverified, do not rely on without independently re-checking**: the
  on-disk path of cached OAuth credentials (commonly cited elsewhere as
  `~/.gemini/oauth_creds.json`, not confirmed in official docs);
  `GOOGLE_GENAI_USE_VERTEXAI` as an env var (seen in one secondary
  source, not confirmed in the fetched official auth doc).
- **Compliance note, not a technical limitation**: Gemini's FAQ states
  that using third-party software to "harvest or piggyback on Gemini
  CLI's OAuth authentication to access backend services" violates ToS.
  Shelling out to the official `gemini` binary the same way this
  project already does for `codex`/`claude` shouldn't trigger this, but
  it's a Gemini-specific clause with no stated Codex/Claude equivalent,
  worth a conscious read before implementation rather than assuming
  it's identical to the other two providers' terms.

## Recommended Architecture

Unchanged from `docs/research.md`'s original diagram — Gemini slots in
as a third `+-->` branch alongside Codex/Claude:

```text
handoff_bridge.py
   |
   +--> codex exec --json
   +--> claude -p --output-format stream-json
   +--> gemini -p "..." --output-format json
           captures stats/tokens, final response, error.type if present
```

The auth-probe cost (above) and missing session-ID field are the two
things that don't slot in cleanly and need an explicit decision before
implementation, not just a mechanical `PROVIDERS` tuple extension.

## Implementation Plan

Per `docs/design-system/roadmap.md`'s Phase 5 plan, in order:

1. Pre-implementation interview, grounded in this doc, resolving at
   least: how `diagnose()` should represent "can't cheaply check auth
   for this provider" (skip the probe by default? separate opt-in
   flag? accept the token cost?), whether `error.type`/exit-code
   matching or text-pattern matching is primary for
   `classify_handoff()`'s Gemini branch, and whether missing a real
   session ID blocks resume-based fallback chaining for Gemini or just
   means "always resume latest."
2. Refactor `handoff_bridge.py`'s `other_provider()` (binary toggle) so
   picking a fallback *target* generalizes to N providers, not just
   two — already documented as a known gap in
   `docs/provider-extensibility.md`'s "The Current Code Assumes Exactly
   Two Providers." (Ended up as `next_provider()`/
   `next_available_provider()`; auto-fallback itself stayed exactly one
   hop, unchanged from the original 2-provider design — see
   `docs/provider-extensibility.md` for why.)
3. Extend `PROVIDERS`, add `provider_command()` branch,
   `summarize_gemini()` (parsing the JSON-mode response shape
   documented above), and `classify_handoff()` signal matching for
   Gemini's exit codes/error types.
4. `diagnose()`: add a Gemini row using whatever the interview decides
   for the auth-check tradeoff.
5. Tests: fake `gemini` binary script (mirroring `FAKE_CODEX_SUCCESS`/
   `FAKE_CLAUDE_SUCCESS` in `tests/test_handoff_webui.py` and the
   bridge's own provider tests), covering success, the chosen
   auth-failure signal, and fallback-target selection reaching an
   installed provider even when one in between isn't.

## Sources

- Google: [Gemini CLI installation](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/get-started/installation.mdx)
- Google: [Gemini CLI reference](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/cli-reference.md)
- Google: [Headless mode](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/headless.md)
- Google: [Headless mode, worked JSON example](https://google-gemini.github.io/gemini-cli/docs/cli/headless.html)
- Google: [Automation tutorial](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/tutorials/automation.md)
- Google: [Session management](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/session-management.md)
- Google: [Checkpointing](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/checkpointing.md)
- Google: [Troubleshooting (exit codes)](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/resources/troubleshooting.md)
- Google: [FAQ](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/resources/faq.md)
- Google: [Authentication](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/get-started/authentication.mdx)
- Google: [Configuration reference](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/reference/configuration.md)
- Google: [Commands reference](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/reference/commands.md)
- Google: [Hooks overview](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/hooks/index.md)
- Google: [Hooks reference](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/hooks/reference.md)
- Google: [GEMINI.md context files](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/gemini-md.md)
- Google: [MCP server support](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/tools/mcp-server.md)
- Google: [ACP mode](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/acp-mode.md)
- Google: [Model selection](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/model.md)
- Google: [Gemini 3](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/get-started/gemini-3.md)
