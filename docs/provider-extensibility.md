# Provider Extensibility

How to add a new AI provider (CLI-based) to this bridge.
This doc exists because the 2026-08-04 design-review requirements asked for
Gemini CLI to be *recognized* by the v0.2 chat redesign, and for "any other
model beyond that" to be documented rather than designed — see
[docs/design-system/wireframes.html §S8](design-system/wireframes.html#s8)
and its Conflict List entries CFL-12/CFL-13 in
[docs/design-system/flutter-mapping.html](design-system/flutter-mapping.html#s2).

This started as a documentation-only deliverable (nothing described here
was implemented yet) so the next real implementation pass wouldn't have
to rediscover these constraints from scratch. That implementation pass
happened in Phase 5 for the CLI-based case (Gemini). An API-key-based
path was also built (Phase 4, DEC-13~16, DEC-21, DEC-25) and later removed
— see "API-Key-Based Providers (Removed)" below. The sections below mix
the original forward-looking plan (still accurate for a hypothetical
*fourth* CLI provider) with a record of what actually happened for Gemini.
Each section says which it is.

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

## API-Key-Based Providers (Removed)

A Web UI path for reaching a provider over its vendor HTTP API with a
pasted key (Phase 4, [DEC-13~16](design-system/flutter-mapping.html#s1c);
tool-use loop [DEC-21](design-system/flutter-mapping.html#s1c); Gemini
[DEC-25](design-system/flutter-mapping.html#s1c); user-defined OpenAI/
Anthropic-compatible "custom providers"
[DEC-26](design-system/flutter-mapping.html#s1c)) existed through v0.4.11
and was removed afterwards. The reasoning: this bridge hands work between
*subscription* CLIs when one hits a quota or context limit; a pay-as-you-go
API has no such limit to hand off from, so the mode never served that
purpose, and its shell-exec tool loop was the largest risk surface in the
app ([Production Audit F1](production-audit-2026-09-02.md)).

So a new provider is a **CLI** provider only: add it to `PROVIDERS` and
follow the CLI section above. There is no HTTP-API fallback for a provider
whose CLI is absent — the run fails through the normal bridge subprocess
path (`exit_code` 127). The original research is kept as a historical
record in [research-api-key-mode.md](research-api-key-mode.md); the
decisions above stay in the Decision Log, annotated as superseded.

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
