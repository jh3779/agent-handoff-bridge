# Web UI Chat Storage — Data Model

Source of truth for the on-disk format `handoff_webui.py` reads and writes.
This repo has no ADR directory or numbered decision-record series — plain
`docs/*.md`, cross-linked from `docs/index.md`, is the convention (see
`docs/quality-gates.md`, `docs/provider-extensibility.md` for the same
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
| `provider` | string \| null | **`agent` role only** — `"codex"` or `"claude"` (never `"auto"`; that's resolved to a real provider before the record exists) |
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

**`agent` role messages** (Phase 1) are never written by the client directly
— `POST /api/run` is the only writer; `POST /api/chat` rejects `role: "agent"`
with 400 (`CLIENT_WRITABLE_CHAT_ROLES = ("user", "system")` in
`handoff_webui.py`), even though the shared `append_chat_message()` writer it
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

## Known Open Questions

Tracked as
[CFL-15 in flutter-mapping.html](design-system/flutter-mapping.html#s2):
retention/expiry policy, and whether/when a schema version field becomes
necessary.
