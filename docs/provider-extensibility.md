# Provider Extensibility

How to add a new AI provider (CLI-based or API-key-based) to this bridge.
This doc exists because the 2026-08-04 design-review requirements asked for
Gemini CLI to be *recognized* by the v0.2 chat redesign, and for "any other
model beyond that" to be documented rather than designed — see
[docs/design-system/wireframes.html §S8](design-system/wireframes.html#s8)
and its Conflict List entries CFL-12/CFL-13 in
[docs/design-system/flutter-mapping.html](design-system/flutter-mapping.html#s2).

This started as a documentation-only deliverable (nothing described here
was implemented yet) so the next real implementation pass wouldn't have
to rediscover these constraints from scratch. That implementation pass
happened in Phase 5 for the CLI-based case (Gemini), and again for the
API-key-based path's full agentic parity (CFL-17, resolved as DEC-21) —
the sections below now mix the original forward-looking plan (still
accurate for a hypothetical *fourth* CLI provider) with a record of what
actually happened for Gemini and for the API-key path specifically. Each
section says which it is.

## The Current Code Assumed Exactly Two Providers (Resolved In Phase 5)

`handoff_bridge.py` was originally written for a Codex/Claude pair, and
that assumption was load-bearing in a few places, not just a naming
convention. **Gemini CLI was added as the worked example in Phase 5**
(`docs/design-system/roadmap.md`), so this section is kept as a record of
what had to change, not a forward-looking plan:

- `PROVIDERS` (`handoff_bridge.py`) — was `("codex", "claude")`, now
  `("codex", "claude", "gemini")`. Iterated by `diagnose()` and
  `choose_auto_provider()`'s fallback scan, so this part really was
  mechanically easy, as originally predicted.
- `other_provider()` was a **hardcoded binary toggle**:
  `"claude" if provider == "codex" else "codex"` — the actual blocker,
  exactly as flagged. Replaced with `next_provider(current, tried=frozenset())`:
  walks `PROVIDERS` in order starting after `current`, wraps around, and
  skips anything already in `tried`. All three call sites
  (`init_handoff()`'s "fallback provider" message,
  `choose_auto_provider()`, and `run_provider()`'s auto-fallback target)
  now use it. Auto-fallback itself is still exactly one hop, same as
  before this change — `next_provider()` only generalized *which*
  provider a hop lands on, not how many hops happen.
- `provider_command()` gained an explicit `provider == "gemini"` branch
  (prompt via stdin like the other two, `--resume latest` once a prior
  clean run in this workspace is recorded — see
  [DEC-17](design-system/flutter-mapping.html#s1c) for why Gemini can't
  resume a *specific* session by ID the way Codex/Claude do).
- `parse_jsonl()` + `summarize_codex()` / `summarize_claude()` remain
  Codex/Claude-specific. Gemini needed its own `summarize_gemini()` for a
  different reason than "different event names" — its
  `--output-format json` returns one JSON object at the end of the run,
  not a JSONL event stream at all, so `parse_jsonl()` doesn't apply to it
  and `summarize_gemini()` parses `stdout` directly instead of taking
  pre-parsed `events`.
- `classify_handoff()` itself needed no changes, as predicted — it still
  classifies Gemini's failures via generic stdout/stderr/`errors` text
  matching, provider-agnostic as before. `ERROR_PATTERNS`'s `auth` entry
  did need one addition, found by review after this section originally
  claimed "no changes needed at all": Gemini's literal, verified
  `error.type` value `"AuthError"` (and exit code 41's label,
  `FatalAuthenticationError`) didn't match any existing phrase
  (`not logged in`/`authentication_failed`/`unauthorized`/`forbidden`),
  so a real Gemini auth failure was classified `unknown` instead of
  `auth` until those two exact, sourced strings were added to the
  pattern. Gemini's own docs still don't expose a distinct rate-limit/
  quota/context-length signal beyond a generic API error, so beyond that
  one verified addition, this project's existing text-pattern approach
  still ends up doing relatively more work for Gemini than it does for
  Codex/Claude's more structured signals — documented as a known
  imprecision in [docs/research-gemini-cli.md](research-gemini-cli.md)
  "Practical Limitations," not solved with new bespoke logic.
- `handoff_webui.py`'s API-key mode (Phase 4) imports the same
  `PROVIDERS` for CLI-detection purposes, but does **not** automatically
  extend to a new CLI provider — it has its own, deliberately separate
  `API_KEY_MODE_PROVIDERS` tuple ([DEC-15](design-system/flutter-mapping.html#s1c)).
  Adding a CLI provider to `PROVIDERS` never silently changes what
  API-key mode supports; that stays a distinct decision. Gemini's own
  API-key-mode decision was made later, separately, as
  [DEC-25](design-system/flutter-mapping.html#s1c) — see "Adding A New
  API-Key-Based Provider" below.

## Adding A New CLI-Based Provider — What Actually Happened For Gemini

This section used to be a plan; it's now a record of the real Phase 5
sequence, kept as the template for a fourth provider someday:

1. **Confirmed the actual CLI surface first** —
   [docs/research-gemini-cli.md](research-gemini-cli.md), the same
   discipline `docs/research.md` used for Codex/Claude before any code
   was written. Two things the wireframes had assumed turned out to need
   real design decisions once researched, not just implementation:
   Gemini has no session ID in its JSON output (resolved as
   [DEC-17](design-system/flutter-mapping.html#s1c)) and no free
   auth-status command (resolved as
   [DEC-18](design-system/flutter-mapping.html#s1c)) — both required a
   pre-implementation interview, not just mechanical extension.
2. Added `"gemini"` to `PROVIDERS`.
3. Replaced `other_provider()` with `next_provider(current, tried)` (see
   above).
4. Added the `gemini` branch to `provider_command()`.
5. Added `summarize_gemini()` and wired it into `run_provider()`'s
   dispatch (now an explicit `if/elif/else` across all three providers,
   not a two-way ternary).
6. `diagnose()` needed one small addition beyond "no change needed" —
   the CLI-detection loop over `PROVIDERS` really did need nothing, but
   an explicit "gemini auth: not checked" line was added so the auth-
   probe gap (DEC-18) is visible in the output rather than silently
   absent.
7. `scripts/validate_handoff.py`'s `HANDOFF_CLASSIFICATION_LABELS` needed
   no changes, as predicted.
8. Added unit tests to `tests/test_handoff_bridge.py`: `next_provider()`
   (ordering, wraparound, skip-tried), `provider_command()`'s gemini
   branch, `summarize_gemini()` (success/error/malformed input), and a
   real-subprocess integration test with a fake `gemini` binary script —
   following the existing pattern
   (`docs/quality-gates.md` "Core Logic Has Unit Tests").

## Adding An API-Key-Based Provider (No Local CLI)

**Implemented in Phase 4** (`docs/design-system/roadmap.md`), per
[DEC-13~16](design-system/flutter-mapping.html#s1c) — this section
originally described the open questions before that work started (as
CFL-12); kept below for what was actually decided. Started chat-only;
extended to full agentic parity by the CFL-17 follow-up
([DEC-21](design-system/flutter-mapping.html#s1c)), see below.

- `run_provider()`'s CLI subprocess path is completely unchanged.
  `_run_provider_via_bridge_locked()` (`handoff_webui.py`) only diverts to
  `run_provider_via_api_key()` when a provider's CLI is genuinely absent
  (`shutil.which()`) *and* a key is saved for it — every previously-existing
  code path (CLI available, or CLI absent with no key) behaves exactly as
  before this phase.
- Session resume (`codex exec resume <id>`, `claude --resume <id>`) and the
  hooks-based signals `docs/research.md` relies on (`StopFailure`,
  `PostCompact`, etc.) genuinely have no equivalent behind a plain API-key
  call, confirmed by [docs/research-api-key-mode.md](research-api-key-mode.md)
  before this was built: neither Anthropic's Messages API nor OpenAI's
  Responses API is session-based. `build_api_message_history()` replays the
  chat log as alternating turns on every call instead — conversation
  continuity, not a resumable server-side session.
- Credential storage: `~/Documents/Agent Handoff Bridge/credentials.json`
  (`0600` permissions), not an OS keychain or the `keyring` package
  (DEC-14 — see `docs/webui-chat-storage.md`'s "Credentials & API-Key Mode"
  section for the full schema and security posture).
- The v0.2 UI entry point (masked key field, per-provider connection-mode
  badge) designed in
  [docs/design-system/components.html §14](design-system/components.html#s14)
  and [wireframes.html §S8](design-system/wireframes.html#s8) is now real —
  `webui/index.html`'s Diagnose button opens it, backed by
  `GET /api/providers`/`POST /api/provider-key`.
- **Full agentic parity, added by the CFL-17 follow-up**
  ([DEC-21](design-system/flutter-mapping.html#s1c)): this API-key path
  no longer only exchanges text. `call_anthropic_messages_api()`/
  `call_openai_responses_api()` run a bridge-built tool-use turn loop
  (`read_file`/`write_file`/`edit_file`/`run_shell`, one schema list in
  `_TOOL_SPECS` rendered into each vendor's own shape) so the model can
  read/edit workspace files and run shell commands the same way CLI mode
  can. File tools reuse `safe_join()` for workspace confinement;
  `run_shell` runs with `cwd=workspace` and no further restriction, the
  same trust level CLI mode's own subprocesses already have. DEC-02
  (confirm only the first send per session) covers every tool call this
  loop makes, so nothing beyond that first confirmation gates a shell
  command once a session is underway — a real, consciously-accepted
  tradeoff from DEC-21's design interview, not an oversight. Full
  reasoning: `docs/webui-chat-storage.md`'s "Tool loop" section and
  [flutter-mapping.html DEC-21](design-system/flutter-mapping.html#s1c).
- **Keys are verified before they're saved.** `POST /api/provider-key`
  previously wrote any non-empty key string to `credentials.json`
  unconditionally, trusting its shape alone. It now calls
  `validate_provider_api_key()` first -- one real, minimal, tool-free call
  to the provider's own API asking for a one-word reply -- and only saves
  on success; a bad key, wrong model name, or network failure returns a
  400 with nothing written. `model` was already required in practice (no
  built-in default exists per DEC-13); it's now enforced by the endpoint
  itself, since there is no default to fall back on for the verification
  call either. See `docs/webui-chat-storage.md`'s "Credentials & API-Key
  Mode" section for the full contract.
- **Gemini added as a third API-key-mode provider**
  ([DEC-25](design-system/flutter-mapping.html#s1c), resolving DEC-15's
  originally-left-open "should Gemini get API-key mode too" question).
  `API_KEY_MODE_PROVIDERS` grew to `("codex", "claude", "gemini")` --
  still its own tuple, not an alias for `PROVIDERS`, so a future CLI
  provider still needs its own separate decision the same way this one
  did. New `call_gemini_api()` matches `call_anthropic_messages_api()`/
  `call_openai_responses_api()`'s contract and tool-use loop exactly,
  but Gemini's actual wire format differs enough to need real
  translation, not a drop-in copy: `Content` objects use `{"role",
  "parts": [...]}` instead of `{"role", "content": "..."}` (`"model"` is
  Gemini's assistant-turn role, not `"assistant"`), and its function
  calling uses `functionCall`/`functionResponse` parts (the response
  sent back with `role: "user"`, wrapping the tool executor's plain-text
  result as `{"result": <text>}` since Gemini requires an object there,
  unlike the other two). Confirmed against
  [ai.google.dev/api/generate-content](https://ai.google.dev/api/generate-content)
  and
  [ai.google.dev/gemini-api/docs/generate-content/function-calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling)
  before implementing -- same "don't assume the shape" discipline this
  project already applied to Anthropic's and OpenAI's APIs. Auth via the
  `x-goog-api-key` header, not the `?key=` query-string alternative the
  same docs also describe, so the secret never ends up in a URL.

## Checklist When A Provider Is Actually Added

Update these regardless of CLI or API mode — this list exists so a future
provider addition doesn't silently leave the docs describing only Codex and
Claude:

- `docs/architecture.md` — component diagram and "Provider Selection"
  section assume exactly two.
- `docs/cli-reference.md`, `docs/agent-targeting-protocol.md` — provider
  enums in examples and required-header field descriptions.
- `docs/shared-agent-contract.md` — nothing provider-specific here today;
  confirm it stays that way.
- `docs/research.md` — append the new provider's research the way Codex and
  Claude are documented, so the next person doesn't have to redo it.
- `handoff_desktop.py` / `handoff_control.py` (or their v0.2 replacements) —
  `PROVIDERS`/`PRIMARY_PROVIDERS` tuples are duplicated in
  `handoff_desktop.py:32-33`, independent of `handoff_bridge.py`'s copy.
- `docs/design-system/` — components and wireframes that hardcode "Codex /
  Claude Code" pairs (e.g. the mobile prompt account/app mapping in
  `handoff_desktop.py:283` and `docs/mobile-app-remote-guide.md`).
