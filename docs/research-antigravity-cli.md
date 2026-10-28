# Research: Antigravity CLI (`agy`)

Date: 2026-10-28. Replaces the Gemini CLI provider
([research-gemini-cli.md](research-gemini-cli.md), now historical);
decision record: [DEC-30](design-system/flutter-mapping.html#s1c).

## Why this exists

Google stopped serving Gemini CLI to Google AI Pro, Google AI Ultra and free
individual Code Assist accounts on 2026-06-18 and replaced it with Antigravity
CLI (`agy`, closed source). The old `gemini` CLI stays available for paid
Gemini / Gemini Enterprise Agent Platform API keys and for Code Assist
Standard/Enterprise licences. Whether a *free* AI Studio key or Vertex AI still
works is not stated on Google's pages. Sources: Google Developers Blog
"An important update: transitioning Gemini CLI to Antigravity CLI" (2026-05-19),
the Gemini CLI GitHub discussion #28017 (2026-06-18), Google's Code Assist
consumer deprecation page, and the Antigravity migration page.

`agy` is not Gemini-only: `agy models` (agy 1.4.8) lists Gemini 4 Argon and
Gemini 3.8 Flash, Claude Opus 5.5 and Sonnet 5.5, and GPT-OSS 120B, each with
low/medium/high variants. Usage draws on the account's weekly Antigravity quota.

## What was verified against agy 1.4.8 (a few tiny real calls, 2026-10-28)

| Question | Result |
|---|---|
| One-shot, JSON | `agy -p "..." --output-format json` returns `{conversation_id, status, response, duration_seconds, num_turns, usage{input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}}` |
| Prompt via stdin | `printf '...' \| agy --output-format json` works **without** `-p`. `-p` plus another flag exits 2: `-p` takes the next flag as its prompt |
| Resume | `--conversation <conversation_id>` keeps the same id and increments `num_turns`; works together with stdin |
| File write in print mode | **Denied.** Exit 0, `status: "SUCCESS"`, empty `response`, plus `denied_actions: [{"action": "write_file", ...}]`; stderr explains that headless mode cannot prompt for permission |
| Shell command in print mode | Denied the same way (`action: "command"`) |
| `--mode accept-edits` | Still denied in print mode |
| `--mode auto` | "not available for this account" (falls back to default mode) |
| Auth | No status subcommand. Headless runs use cached credentials; an unauthenticated run exits with `authentication required` |

`--dangerously-skip-permissions` (with or without `--sandbox`) was **not**
exercised: it approves every tool with no scoping, and whether `--sandbox`
actually confines writes to the workspace is unverified.

## What the bridge does

- Command: `agy --output-format json [--conversation <id>] [--model <id>]`, prompt on
  stdin (`provider_command()`).
- `summarize_agy()` parses the envelope. A run with `denied_actions` and an empty
  response is a `tool_failure` (the work was not done), so `classify_handoff()`
  hands it on and the message tells the user to add `permissions.allow` rules,
  e.g. `write_file(<path>)` or `command(<command>)`, to agy's `settings.json`.
- The prompt differs for `agy` in one place. The standard "update
  `.handoff/current.md` before stopping" instruction needs a file write, which
  print mode denies; in a real run (2026-10-28) `agy` spent the whole turn trying
  to write it and finished with an empty reply, so the first turn was always
  recorded as a `tool_failure`. For `agy` the bridge instead asks for the
  Changed/Verified/Remaining/Blocked summary in the final reply (the bridge appends
  every provider's final reply to `current.md` itself) plus a short runtime note
  ("do not retry a denied action; always reply in text"). After that change a real
  first turn returned its answer and saved the conversation id. The same applies
  to the protocol summary and, in `--full-prompt` mode, to a closing "Final
  Instruction" that overrides the docs' own "update `current.md`" lines, so a
  prompt never tells agy both things. A tool denial next to a real reply is a
  `tool_failure` as well (the reply may only propose changes that were not
  applied): the reply is kept for the next provider and a note is added.
- `diagnose` reports whether `agy` is installed; it does not check auth.
- The bridge never passes `--dangerously-skip-permissions`. Adding an explicit
  opt-in for it is a possible follow-up if the default turns out too restrictive.

## Known gaps

- agy's own docs describe `--print-timeout` as defaulting to 5 minutes, while
  `agy --help` shows `0s` (wait until the turn completes); the bridge passes its
  own timeout and does not rely on either.
- The error shape for a stale `--conversation` id, a rate/quota limit, and a
  model unavailable for the account were not triggered; they fall through to
  `classify_handoff()`'s generic text patterns.
