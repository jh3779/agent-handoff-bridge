# Provider Extensibility

How to add a new AI provider (CLI-based or API-key-based) to this bridge.
This doc exists because the 2026-08-04 design-review requirements asked for
Gemini CLI to be *recognized* by the v0.2 chat redesign, and for "any other
model beyond that" to be documented rather than designed — see
[docs/design-system/wireframes.html §S8](design-system/wireframes.html#s8)
and its Conflict List entries CFL-12/CFL-13 in
[docs/design-system/flutter-mapping.html](design-system/flutter-mapping.html#s2).

This is a documentation-only deliverable: nothing described here is
implemented yet. It exists so the next real implementation pass doesn't have
to rediscover these constraints from scratch.

## The Current Code Assumes Exactly Two Providers

`handoff_bridge.py` was written for a Codex/Claude pair, and that assumption
is load-bearing in a few places, not just a naming convention:

- `PROVIDERS = ("codex", "claude")` (`handoff_bridge.py:47`) — iterated by
  `diagnose()` and `choose_auto_provider()`'s fallback scan, so adding a
  third entry to this tuple is mechanically easy for those two call sites.
- `other_provider()` (`handoff_bridge.py:412-413`) is a **hardcoded binary
  toggle**: `"claude" if provider == "codex" else "codex"`. This is the
  actual blocker. With three or more providers, "the other one" stops being
  well-defined — auto-fallback needs an explicit ordered list (e.g. "try the
  next provider in `PROVIDERS` order, wrapping around, skipping the one that
  just failed") instead of a two-way branch.
- `provider_command()` (`handoff_bridge.py:492-543`) is a single function
  with an `if provider == "codex": ... else: # assumed claude` structure. A
  third provider needs its own branch, not a fallthrough.
- `parse_jsonl()` + `summarize_codex()` / `summarize_claude()` are separate
  parsers because Codex's and Claude Code's JSONL event shapes differ. A new
  CLI needs its own `summarize_<provider>()` unless its event schema happens
  to match one of the existing two.
- `ERROR_PATTERNS` / `classify_handoff()` (`handoff_bridge.py:68-83`,
  `:517-536`) are provider-agnostic regex matching over combined
  stdout/stderr — these do **not** need per-provider changes, which is the
  one part of this that already generalizes for free.

## Adding A New CLI-Based Provider (e.g. Gemini CLI)

Recognizing a CLI provider means: detect it, run it, and parse its output
into the same `{session_id, usage, cost_usd, final_text, errors}` shape the
rest of the bridge already expects. Concretely:

1. **Confirm the actual CLI surface first.** `docs/research.md` did this
   research for Codex and Claude before any code was written (non-interactive
   mode flags, JSON event streaming, session resume, hooks). No equivalent
   research exists for Gemini CLI yet — binary name, auth command, a
   non-interactive/scriptable invocation mode, and whether it emits
   structured (JSON/JSONL) output are all unverified assumptions in the v0.2
   wireframes. Do this research before writing code, the same way
   `docs/research.md` did.
2. Add the provider name to `PROVIDERS` in `handoff_bridge.py`.
3. Rewrite `other_provider()` into an ordered-fallback function (e.g.
   `next_provider(current, tried)`) — a binary ternary cannot express "which
   provider comes next" once there are three or more.
4. Add a branch to `provider_command()` for the new binary's exec/resume
   invocation shape.
5. Add `summarize_<provider>()` and wire it into the `provider == "codex" /
   "claude"` dispatch near `handoff_bridge.py:743`.
6. `diagnose()` already loops over `PROVIDERS` for version checks
   (`handoff_bridge.py:317-328`) — no change needed there.
7. Update `scripts/validate_handoff.py`'s `HANDOFF_CLASSIFICATION_LABELS`
   only if the new provider needs a failure signal the existing 8 labels
   don't cover — unlikely, since those are about failure *category*, not
   provider identity.
8. Add unit tests to `tests/test_handoff_bridge.py` for the new
   `provider_command()` branch and `summarize_<provider>()`, following the
   existing pattern (`docs/quality-gates.md` "Core Logic Has Unit Tests").

## Adding An API-Key-Based Provider (No Local CLI)

**Implemented in Phase 4** (`docs/design-system/roadmap.md`), chat-only
scope, per [DEC-13~16](design-system/flutter-mapping.html#s1c) — this
section originally described the open questions before that work started
(as CFL-12); kept below for what was actually decided plus what's still
open.

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
- **Still open, deliberately** ([CFL-17](design-system/flutter-mapping.html#s2)):
  this API-key path only exchanges text. It does not read or edit workspace
  files, and does not run shell commands — the CLI's actual agentic
  capability. Neither vendor's direct API exposes that behind a plain
  API-key call; getting there means this project defining its own file-
  read/write/edit and shell-exec tool schemas and running its own tool-use
  turn loop, which is a substantially larger and riskier build (a new,
  bridge-controlled shell-exec surface) than this phase's scope. Deferred to
  a future phase by explicit user decision, not an oversight.

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
