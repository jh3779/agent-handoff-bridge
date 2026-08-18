# Web UI Chat Storage — Data Model

Source of truth for the on-disk formats the Web UI (`handoff_webui.py` and its `webui_*.py` modules) reads and
writes: the per-workspace chat log below, and the app-level
["recently-opened" registry](#recently-opened-registry-phase-3) Phase 3
added. This repo has no ADR directory or numbered decision-record series —
plain `docs/*.md`, cross-linked from `docs/index.md`, is the convention
(see `docs/quality-gates.md`, `docs/provider-extensibility.md` for the same
pattern). This doc exists so that convention covers the new local data
model the [Web UI MVP](cli-reference.md#web-ui-mvp) introduced, not just
its CLI usage.

## Location

```
<workspace>/.handoff/webui/chat/YYYY-MM.jsonl        # current month, appendable
<workspace>/.handoff/webui/chat/YYYY-MM.jsonl.gz      # past months, compressed
<workspace>/.handoff/webui/.gitignore                 # "*", see "Git Visibility" below
<workspace>/.handoff/webui/chat/.write.lock            # transient, see "Atomicity"
```

One file per calendar month (`month_key()` = `datetime.strftime("%Y-%m")`,
always UTC). Chosen over a single growing file so archiving (see below) can
operate on whole months at a time without rewriting history, and over
per-day files because a single day's volume doesn't usually justify a
separate file.

## Schema (JSON Lines, one message per line)

```json
{"id": "6425689b5b7f4851bbe59b5cc6a663a3", "ts": "2026-08-04T00:45:55.408354+00:00", "role": "user", "text": "...", "attachments": [{"name": "a.txt", "path": "a.txt", "content": "...", "truncated": false}]}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | `uuid.uuid4().hex`, assigned server-side in `append_chat_message()` |
| `ts` | string | `datetime.isoformat()`, always UTC (`utc_now()`) |
| `role` | `"user"` \| `"system"` \| `"agent"` | validated server-side (`CHAT_ROLES`); anything else is rejected with 400 |
| `text` | string | may be empty if the message is attachments-only |
| `attachments` | array | client-supplied as-is (see "What Gets Persisted" below) |
| `provider` | string \| null | **`agent` role only** — `"codex"`, `"claude"`, or (as of Phase 5) `"gemini"` (never `"auto"`; that's resolved to a real provider before the record exists) |
| `status` | string \| null | **`agent` role only** — `"success"` \| `"handoff"` \| `"fail"`, from `classify_run_status()` |
| `reason` | string \| null | **`agent` role only** — the underlying `handoff_bridge.py` `classify_handoff()` reason string (e.g. `"rate_limit: matched rate_limit signal"`) |

`provider`/`status`/`reason` are only present when `role` is `"agent"` — `user`
and `system` messages keep the original 5-field shape rather than carrying
three always-null fields.

**No version field.** This is an implicit "v1" schema. If the shape changes
in a way that isn't purely additive, add an explicit `schema` field before
that happens and teach `read_month_messages()` to handle both — there is no
migration tooling today, and old `.jsonl[.gz]` files are never rewritten in
place.

## What Gets Persisted

`attachments` is stored exactly as the client (`webui/app.js`) sends it:
`{name, path, content, truncated}`. For files attached from the workspace
tree, `content` is typically omitted/redundant since it's re-fetchable via
`GET /api/file?path=`; for files dragged in from outside the workspace
(`path: null`), `content` is the only place that data exists, so it's kept.
This means a large pasted/dropped file's content can end up duplicated
between the source file and the JSONL log — monthly gzip compression
(below) is the accepted mitigation for that, not content-stripping, per the
original request that introduced this feature.

`attachments` on a `POST /api/run` call also become part of the actual
provider prompt, not just this chat log — `build_run_prompt()`
(`webui_bridge_run.py`) folds each attachment's name/content into the text
written to `--prompt-file` before the provider ever runs. This was a real
gap for one round of review: the client sent `attachments` to `/api/chat`
but not to `/api/run`, so a file the user "attached" was persisted locally
but never actually reached Codex/Claude.

**`agent` role messages** (Phase 1) are never written by the client directly
— `POST /api/run` is the only writer; `POST /api/chat` rejects `role: "agent"`
with 400 (`CLIENT_WRITABLE_CHAT_ROLES = ("user", "system")` in
`webui_chat_storage.py`), even though the shared `append_chat_message()` writer it
calls into would otherwise accept any role in `CHAT_ROLES`. Without that
check a client could POST a fake agent reply straight to `/api/chat` with no
provider having actually run. `run_provider_via_bridge()` shells out
to `handoff_bridge.py run <provider> --execute --auto-fallback`, diffs
`.handoff/state.json`'s `history[]` before/after to find the new record(s)
that run produced (more than one if auto-fallback chained into a second
provider), and calls `append_chat_message(..., role="agent", ...)` once per
record — so a single user turn that triggers a fallback shows up as two
separate agent messages in the thread, one per provider, in the order they
actually ran. `text` comes from that record's `final_text`, falling back to
`f"(exit {exit_code}, no output)"` if empty.

## Atomicity

Every write goes through `handoff_bridge.WriteLock` (reused, not
reimplemented — the same cross-process exclusive-file-creation lock that
guards `.handoff/state.json`), scoped per-workspace at
`.handoff/webui/chat/.write.lock`:

- `append_chat_message()` holds the lock for the `mkdir` + gitignore-check +
  single `open(path, "a")` write.
- `archive_old_months()` holds the *same* lock for its whole
  read-compress-delete pass.

The lock exists specifically so an archive pass (run on server startup and
on every `Open Folder` switch) can never delete a month's `.jsonl` out from
under a concurrent append to that same file. Without it, "read the file to
gzip it, then unlink the original" racing against "open the original in
append mode" is a real corruption/data-loss window, not a theoretical one.

A half-written last line (process killed mid-`write()`) is tolerated on
read: `read_month_messages()` skips any line that fails `json.loads()`
rather than failing the whole read.

## Archiving (Retention Is Not Deletion)

`archive_old_months()` gzip-compresses every `*.jsonl` file whose month
isn't the current one, then deletes the plain copy. This is a **size**
optimization, not a retention policy:

- There is currently no mechanism that ever deletes chat history. It
  accumulates (compressed) forever.
- If retention/expiry is wanted later, it needs a separate, deliberate
  decision (how long, opt-in vs default-on, per-workspace override) --
  not assumed here.

## Git Visibility

Chat history defaults to **not tracked by git**, via
`.handoff/webui/.gitignore` containing a single `*` — this covers the
`chat/` subdirectory and the `.gitignore` file itself (gitignore patterns
match dotfiles). `ensure_chat_gitignore()` writes this file idempotently
and is called proactively on server startup and on every `Open Folder`
switch, not just lazily on first message — so a workspace is protected
before any chat data exists in it, regardless of:

- whether `handoff_bridge.py install` was ever run in that workspace
  (`install_standard_files()` only writes the top-level
  `.handoff/.gitignore` template on a *fresh* install and never refreshes
  an existing one), or
- whether that top-level `.handoff/.gitignore` predates this feature and
  doesn't mention `webui/chat/`.

The top-level `.handoff/.gitignore` template also gained a `webui/chat/`
line for newly-installed workspaces, but the per-directory file above is
the actual guarantee — it doesn't depend on the template being current.
Verified against a real git repository (init, commit, run the server, post
a message, `git status --porcelain` stays empty) in
`tests/test_handoff_webui.py::EnsureChatGitignoreTests`.

This is a default, not a hard rule: chat text can contain pasted secrets or
proprietary code, so if a workspace's chat history is ever force-added
despite the ignore (`git add -f`), `scripts/scan_secrets.py` still applies
to it like any other tracked file — it is deliberately **not** added to
that script's `PATH_ALLOWLIST`.

## Recently-Opened Registry (Phase 3)

Unlike everything above, this file lives **outside any single workspace**
— it's app-level state, not per-project, because its whole purpose is
listing *other* projects the [history drawer](cli-reference.md#web-ui-mvp)
shows alongside the current one:

```
~/Documents/Agent Handoff Bridge/registry.json
```

Same base directory Phase 2 introduced for auto-created workspaces
(`AUTO_WORKSPACE_BASE_DIR`) — reused rather than adding a second,
OS-specific app-data path (`~/Library/Application Support`, `%APPDATA%`,
`~/.config`); see [DEC-09](design-system/flutter-mapping.html#s1c).

**Schema** — a JSON array, most-recently-opened first:

```json
[{"path": "/Users/me/project", "name": "project", "last_opened": "2026-08-04T00:45:55+00:00"}]
```

| Field | Type | Notes |
|---|---|---|
| `path` | string | `str(workspace)` at the moment it was touched — see "Path normalization" below |
| `name` | string | `workspace.name`, cached at write time so the drawer doesn't need to touch the filesystem per entry to render a label |
| `last_opened` | string | `datetime.isoformat()`, UTC — used only to order entries, not displayed as-is |

**Path normalization**: all three writers (`main()` at startup,
`POST /api/open-folder`, `POST /api/chat`'s auto-create path) always pass
an already-`.resolve()`d `Path` — `resolve_startup_workspace()` and
`validate_workspace_candidate()` resolve directly,
`create_workspace_for_first_message()` resolves `AUTO_WORKSPACE_BASE_DIR`
before building the new path (a real bug, caught in a pre-commit
self-review and fixed: `Path.home()` doesn't itself resolve symlinks —
e.g. `~/Documents` under iCloud Desktop & Documents sync — so an
unresolved write could duplicate one physical folder under two path
strings). `touch_registry()` itself does no normalization of its own; it
trusts the caller.

**Cap**: 50 entries (`REGISTRY_MAX_ENTRIES`), LRU — `touch_registry()`
dedupes by exact `path` string match, moves the touched entry to the
front, and truncates. An entry whose folder no longer exists on disk is
never deleted from the file itself; `build_history_drawer()` just skips
it at render time (`workspace.is_dir()` check) — so a `registry.json` can
technically accumulate stale entries past 50 real ones if enough distinct
paths get touched without ever being pruned. Not a correctness problem
(they're invisible either way), just means the file's row count and the
drawer's visible count can diverge.

**Locking**: a plain in-process `threading.Lock` (`_REGISTRY_LOCK`), not
`handoff_bridge.WriteLock` — the contention this guards against is HTTP
request threads within *one* `handoff_webui.py` process, not separate CLI
invocations racing each other the way `WriteLock` exists for. Two
separate `handoff_webui.py` processes touching the registry at the same
moment can still lose one process's update (last-`os.replace()`-wins;
`atomic_write_text()` still guarantees the file itself is never
corrupted, just that an update can be silently dropped) — an accepted
tradeoff for what is app-level LRU-index convenience state, not
durable/authoritative data (`.handoff/state.json` and
`.handoff/current.md`, per `docs/architecture.md`'s "State Boundaries",
remain the durable handoff surface; the registry is not read by
anything outside `handoff_webui.py` itself).

**Failure isolation**: every read/write path treats a missing, corrupt,
type-malformed, or unreadable registry as "empty", not an error —
`read_registry()` catches `OSError`/`UnicodeDecodeError`/
`json.JSONDecodeError` and filters out any entry that isn't a dict with a
string `path`; `touch_registry()` catches `OSError` around the write
itself and only logs a warning. This is deliberate: `touch_registry()` is
always called *after* the real state change it's attached to already
happened (`AppState.workspace` assigned, or the server about to finish
starting), so letting a registry failure propagate would turn a
successful workspace switch/startup into a client-visible error for a
feature that's a convenience index, not the operation itself. Verified in
`tests/test_handoff_webui.py`'s `RegistryTests` (missing/malformed/
unreadable file, write failure) and `HistoryDrawerLiveServerTests::
test_open_folder_still_succeeds_even_if_the_registry_write_fails`.

## Credentials & API-Key Mode (Phase 4)

Also app-level, not per-workspace, same reasoning as the registry above —
one set of saved keys applies regardless of which workspace is open:

```
~/Documents/Agent Handoff Bridge/credentials.json
```

**Schema** — a JSON object keyed by provider name:

```json
{"claude": {"key": "sk-ant-...", "model": "claude-sonnet-5"}, "codex": {"key": "sk-...", "model": "gpt-5.1-codex"}}
```

| Field | Type | Notes |
|---|---|---|
| `key` | string | the raw API key, as pasted into the connection panel (SCR-06/`components.html` §14) |
| `model` | string \| null | **required**, enforced by `POST /api/provider-key` itself (not just "in practice" as before) — `API_KEY_MODE_DEFAULT_MODELS` (`webui_api_key_mode.py`) is deliberately empty for every provider (a hardcoded Claude default existed briefly but was removed: no externally-citable, dated source could back a specific model ID, and a wrong one would silently break every CLI-less user), and `validate_provider_api_key()` (below) has no model to make its verification call with. Without a saved `model`, `run_provider_via_api_key()` returns a clear "model not configured" chat-log error instead of guessing (see [DEC-13](design-system/flutter-mapping.html#s1c)) |

**Saved keys are verified, not just accepted.** `POST /api/provider-key`
previously wrote any non-empty `key` string to disk unconditionally — a
typo or revoked key was only ever discovered the next time the user
actually tried to chat. It now calls `validate_provider_api_key(provider,
key, model)` first: one real, minimal, tool-free HTTP call to the
provider's own API (Anthropic Messages / OpenAI Responses / Gemini
generateContent, no `tools` in the request body at all — no workspace, so
no tool access is granted just to check a key) asking for a one-word
reply. Only on a real `200` does `save_credential()` ever run; a failure
(bad key, wrong model name, network error) is returned as a `400` and
nothing is written. On success the response carries `verified: true` and
`confirmation: "<the actual reply text>"`, which the connection panel now
shows in its success toast instead of an unconditional "저장되었습니다."
A key removal (empty `key`) skips validation entirely — there is nothing
to verify when disconnecting.

**Gemini joined API-key mode as of DEC-25**
(`docs/design-system/flutter-mapping.html#s1c`), resolving DEC-15's
originally-left-open question. `API_KEY_MODE_PROVIDERS` (`webui_credentials.py`)
now reads `("codex", "claude", "gemini")` — kept as its own tuple rather
than an alias for the `PROVIDERS` imported from `handoff_bridge`, so a
*future* provider added there for CLI dispatch still needs its own
deliberate API-key-mode decision, not silent inheritance. `call_gemini_api()`
implements the same `{"ok"/"text"}` / `{"ok"/"message"}` contract and tool-use
turn loop as the other two, translating the shared `{"role", "content"}`
message history into Gemini's `{"role", "parts": [{"text": ...}]}` `Content`
shape (`"model"`, not `"assistant"`, is Gemini's role for a prior turn) and
its function-calling parts (`functionCall`/`functionResponse`, sent back
with `role: "user"`) — request/response shapes confirmed against
[ai.google.dev/api/generate-content](https://ai.google.dev/api/generate-content)
and
[ai.google.dev/gemini-api/docs/generate-content/function-calling](https://ai.google.dev/gemini-api/docs/generate-content/function-calling)
before implementing, not assumed, per this project's existing practice for
the other two providers. Auth is the `x-goog-api-key` header, not the
`?key=` query-string form the same docs also mention — keeps the secret
out of any URL that could end up in a proxy/log line, and matches the
other two providers' own header-based auth.

**Never in git**: same posture as the chat log, but stronger — this file
lives entirely outside any workspace, so it is never even reachable by
`scripts/scan_secrets.py`'s git-diff-based scan (that scan only ever looks
at files staged/tracked inside a workspace). **File permissions**: written
via `atomic_write_text()` then `os.chmod(path, 0o600)` (best-effort — a
`chmod` failure is swallowed, matching `touch_registry()`'s failure-
isolation posture elsewhere in this file, since a permissions failure here
must not break the save the user just triggered).

**Never logged or echoed**: the raw key value necessarily flows through
`save_credential()`/`read_credentials()` and on into
`validate_provider_api_key()`/`run_provider_via_api_key()`/
`call_anthropic_messages_api()`/`call_openai_responses_api()`/
`call_gemini_api()`/`_http_post_json()` to actually send it — but every
function that builds a user-visible error string
(`validate_provider_api_key()`, `call_anthropic_messages_api()`,
`call_openai_responses_api()`, `call_gemini_api()`,
`_api_key_mode_error_record()`) constructs its message only from the HTTP
response body or exception text. The key itself is never interpolated into
anything that could end up in `.handoff/webui/chat/*.jsonl`, the history
drawer, a `POST /api/provider-key` error response, or a toast.

### Custom Providers (DEC-26)

For users who buy API tokens directly rather than installing a vendor CLI,
or who want a model none of codex/claude/gemini cover: an arbitrary
OpenAI-compatible (Chat Completions — OpenRouter, Groq, Together, a local
Ollama/LM Studio server, etc.) or Anthropic-compatible HTTP endpoint,
registered under a user-chosen name. Unlike the fixed three, there can be
any number of these.

Stored in the *same* `credentials.json` (one file, one lock), under a
`custom_providers` key sibling to the fixed-provider entries:

```json
{
  "claude": {"key": "sk-ant-...", "model": "claude-sonnet-5"},
  "custom_providers": {
    "openrouter": {
      "key": "sk-or-...",
      "model": "meta-llama/llama-3",
      "base_url": "https://openrouter.ai/api/v1",
      "api_format": "openai"
    }
  }
}
```

| Field | Type | Notes |
|---|---|---|
| `api_format` | `"openai"` \| `"anthropic"` | picked per entry, not fixed globally — chosen by the user when registering |
| `base_url` | string | **meaning differs by format**, matching each format's own existing caller function's parameter rather than inventing a third convention: for `"openai"`, the root that `/chat/completions` is appended to (the same "OpenAI SDK `base_url`" convention OpenRouter/Groq/Ollama/LM Studio all document); for `"anthropic"`, the *complete* messages-endpoint URL (`call_anthropic_messages_api()`'s `base_url` param is already a full POST target for the real Anthropic case — a custom entry stays consistent with that instead of a base+append pattern) |
| `key`, `model` | string | same meaning as the fixed providers' fields; `model` is required (no default exists for an arbitrary endpoint) |

A custom provider's `provider` value everywhere else in this codebase
(chat records, `POST /api/run`'s `provider` field, `webui_bridge_run.py`'s
dispatch) is `custom:<name>` (`webui_credentials.CUSTOM_PROVIDER_PREFIX`) —
unambiguous against the fixed strings without threading a second "kind"
field through every function that takes `provider: str`. It has no CLI
concept at all: `_run_provider_via_bridge_locked()` checks
`is_custom_provider()` first, before any `cli_available()` call, and
always dispatches straight to `run_provider_via_api_key()`.

`POST /api/custom-provider` mirrors `POST /api/provider-key`'s contract
exactly (validate-then-save on a non-empty key, empty key removes,
`400`/no-write on a failed validation) — see `validate_custom_provider_name()`
for the name rules (1-40 chars, `[A-Za-z0-9_-]`, can't collide with a
fixed provider name).

`call_openai_compatible_chat_api()` (Chat Completions, *not* the Responses
API `call_openai_responses_api()` targets — most third-party/self-hosted
servers only implement the former) and `call_anthropic_messages_api()`
(reused with a custom `base_url`, not duplicated) are the two callers;
which one runs is picked by `api_format` in `run_provider_via_api_key()`.
Same tool-use turn loop, same `{"ok"/"text"}` contract as the fixed three.
Not every custom endpoint will actually support tool calls — one that
ignores `tools` in the request and just replies with plain text is
already handled the same as "no tool calls in the response" (the loop's
existing first-iteration return), no separate code path needed.

### Shared Project Context (DEC-27)

Free-form, per-*workspace* text that reaches every provider call
regardless of mode — CLI (codex/claude/gemini binaries) or API-key mode
(fixed or custom). Lives at:

```
<workspace>/.handoff/shared-context.md
```

Unlike `current.md` (the handoff *log* — what changed, what's next,
machine-appended), this is free-form context the user writes once
("this project uses 4-space indent", "never touch `legacy/`") and is
never auto-generated. Tracked in git like `current.md` (not gitignored) —
meant to travel with the project and be visible to teammates/other
agents, the same reasoning `current.md` already uses.

Two independent read paths reach the same file, because CLI mode and
API-key mode assemble their prompts in fundamentally different ways and
neither reuses the other's machinery:

- **CLI mode**: `handoff_bridge.py`'s `build_prompt()` reads it via
  `read_text(SHARED_CONTEXT_FILE, "")`; if non-empty (after stripping),
  it's folded in as a `## Project Context` section. Absent entirely — not
  an empty placeholder section — when the file doesn't exist or is
  whitespace-only.
- **API-key mode**: `webui_common.read_shared_context(workspace)` reads
  the same file directly and `run_provider_via_api_key()` passes it as
  `system=` to whichever `call_X_api()` runs. Each caller puts it in its
  own vendor-specific system-prompt field/shape — Anthropic's top-level
  `system` string, the Responses API's `instructions` field, Gemini's
  `systemInstruction`, Chat Completions' `{"role": "system", ...}`
  message (its only equivalent, since Chat Completions has no top-level
  system parameter of its own).

`GET`/`POST /api/shared-context` are the panel's read/write endpoints —
`GET` with no workspace open returns `{"text": ""}` (not an error, same
posture as `/api/chat`'s empty-state); `POST` with no workspace returns
`400` (there is nowhere to write it).

One exception text is deliberately **not** forwarded: `http.client` raises
a bare `ValueError` (not `HTTPError`/`URLError`) if a header value
contains characters it rejects — e.g. a saved key with an embedded CR/LF
reaching the `x-api-key`/`Authorization` header unescaped — and that
exception's own message embeds the offending header *value* verbatim,
which here is the key itself. `_http_post_json()` catches this case
specifically and returns a fixed, generic message instead of `str(exc)`,
unlike every other exception type it handles.

**Dispatch**: `_run_provider_via_bridge_locked()` only reaches the
API-key-mode path (`run_provider_via_api_key()`) when a provider's CLI is
genuinely absent (`cli_available()` is `shutil.which()`-based) *and* a key
is saved for it — a CLI-available provider always uses the existing
subprocess path unchanged, even if a key also happens to be saved for it.
For `provider="auto"`, API-key mode is only considered when **no** provider
CLI is available at all; otherwise `auto` keeps its existing CLI-only
`choose_auto_provider()`/`--auto-fallback` behavior untouched.

**Chat-log record shape**: `run_provider_via_api_key()` returns the same
record shape the subprocess path does (see `agent` role fields above), so
it flows into `classify_run_status()`/`append_chat_message()` unchanged,
with two fields always fixed: `session_id: null` (no provider-managed
session exists in this mode) and `run_dir: null` (no local run directory is
created). It also deliberately **never writes to
`.handoff/state.json`/`current.md`** — those remain the CLI-handoff-
specific durable state files (`docs/architecture.md`'s "State Boundaries");
API-key mode started chat-only (DEC-13) and, as of the CFL-17 follow-up
below, now also runs a tool loop, but it still has no CLI session or
cross-provider auto-fallback concept for either of those files to record.

**Conversation continuity**: since neither vendor's direct HTTP API is
session-based (`docs/research-api-key-mode.md`), `build_api_message_history()`
replays the chat log as alternating user/assistant turns on every call,
standing in for what `codex exec resume`/`claude --resume` do for the CLI
path. Two details a naive "just read this month's file" version got wrong,
found in a second review round and fixed before merge:

- **Scans months backward** (same pattern `collect_recent_turns()` already
  established in Phase 3), not just the current month — otherwise the
  first message(s) of a new UTC month would replay with zero prior
  context even if the same conversation has weeks of history in the
  previous month's file. Capped to the most recent
  `API_KEY_MODE_MAX_HISTORY_MESSAGES` raw messages once enough are
  collected, so a long-lived project doesn't need every month read.
- **Merges consecutive same-role entries** instead of mapping the log
  1:1 to alternating turns. Anthropic's Messages API requires strict
  user/assistant alternation, but a single CLI turn can leave two
  consecutive `agent` chat-log entries when `--auto-fallback` chains
  providers (codex fails, claude succeeds) — replaying those as two
  separate `assistant` messages would violate alternation and fail every
  subsequent API-key-mode call in that workspace with a 400. The merge is
  applied generically (including against the final `prompt`), so it also
  covers the more obscure case of two consecutive bare `user` entries
  with no reply in between.

**Tool loop (CFL-17, resolved as
[DEC-21](design-system/flutter-mapping.html#s1c); extended to Gemini by
[DEC-25](design-system/flutter-mapping.html#s1c))**:
`call_anthropic_messages_api()`/`call_openai_responses_api()`/
`call_gemini_api()` all run a full tool-use turn loop instead of a single
stateless call -- `read_file`/`write_file`/`edit_file`/`run_shell`,
declared once in `_TOOL_SPECS` and rendered into each vendor's own schema
shape (`anthropic_tool_definitions()`/`openai_tool_definitions()`/
`gemini_tool_definitions()`) so the three can't drift apart. A response
with no tool-call block returns on the first iteration, so a plain chat
turn is unaffected -- this loop is a strict superset of the earlier
chat-only behavior, not a separate mode.
`execute_tool_call()` never raises (a bad tool name or a malformed
argument from the model degrades to an error string, not a crash), file
tools reuse `safe_join()` for workspace confinement, and `run_shell` runs
with `cwd=workspace` (its starting directory, not a sandbox -- an
absolute path or `..` reaches anywhere the OS user account can, same as
a real terminal or CLI mode's own `codex`/`claude` subprocess) and no
other restriction -- DEC-21's interview chose this over a narrower/
more-restricted first pass, treating it as the same trust level CLI
mode already has, not a new tier. `TOOL_EXEC_TIMEOUT_SECONDS` only
guarantees killing the immediate subprocess, not a whole process tree a
backgrounded/forked command might spawn -- a known, accepted gap, not a
guaranteed sandbox. `MAX_TOOL_ITERATIONS = 15` bounds a single turn so a
confused model can't loop indefinitely. Tool-call activity (what ran,
with what arguments, what it returned) is folded into `final_text` as a
fenced code block -- DEC-03's existing code-block rendering, not a new
message schema -- so it's visible in the persisted chat log even though
DEC-02's single confirm-on-first-send gate means no per-call confirmation
interrupts the turn.

## Known Open Questions

Tracked as
[CFL-15 in flutter-mapping.html](design-system/flutter-mapping.html#s2):
retention/expiry policy, and whether/when a schema version field becomes
necessary.
