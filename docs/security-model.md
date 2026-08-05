# Security Model

This project is designed around conservative defaults. The bridge should make
handoffs easier without hiding provider permissions or exposing credentials.

## Default Safety Properties

- Provider runs are preview-only unless `--execute` is present.
- HTTP remote execution is disabled unless the server starts with
  `--allow-execute`.
- Existing support files are not overwritten by `install` unless `--force` is
  provided.
- Tracked files are scanned for likely secrets by `scripts/scan_secrets.py`,
  run automatically in `handoff_bridge.py check`, the `.githooks/pre-commit`
  hook, and CI. See [Quality Gates](quality-gates.md).
- Runtime state files are ignored by git.
- Raw provider logs are stored under `.handoff/runs/` and ignored by git.
- Remote server task state and generated mobile prompt files are ignored by git.

## Credential Boundaries

Never commit:

- `~/.codex/auth.json`;
- Claude Code auth files;
- API keys;
- browser cookies;
- `.handoff/runs/`;
- `.handoff/remote/`;
- `.handoff/state.json`;
- `.handoff/next-prompt.md`;
- `.handoff/mobile-*-instruction.txt`;
- `~/Documents/Agent Handoff Bridge/credentials.json`.

The bridge (`handoff_bridge.py`) shells out to `codex` and `claude`; each
provider uses its own local auth and permission model — this is unchanged.

**Exception, deliberate**: the Web UI's Phase 4 API-key mode
(`handoff_webui.py`, [CLI Reference § Web UI](cli-reference.md#web-ui-mvp))
is the one place this project stores a provider credential itself, for a
provider whose CLI isn't installed. It's a real, documented departure from
"each provider manages its own auth," not an oversight:

- stored at `~/Documents/Agent Handoff Bridge/credentials.json`
  (`0600` permissions), never inside a git-tracked workspace, so it's
  outside `scripts/scan_secrets.py`'s scan scope by construction, not
  because it's exempted from it;
- **plaintext at rest** — not OS-keychain-encrypted; a deliberate
  build-vs-buy tradeoff (see
  [Research: API-Key Mode](research-api-key-mode.md) "Credential
  Storage") to avoid three separate per-OS code paths and a new
  third-party dependency (`keyring`), which this project has
  consistently avoided;
- never appears in any chat-log entry, error message, or toast — every
  API-key-mode failure path builds its message only from the HTTP
  response body or exception text, verified by tests
  (`tests/test_handoff_webui.py`'s `CallProviderApiTests`/
  `HttpPostJsonTests`);
- a permissions/write failure while saving is surfaced as a normal `400`
  to that one request, not silently swallowed the way best-effort state
  like the registry is — saving a credential is a user-initiated action
  with an immediate result to react to, unlike `touch_registry()`'s
  after-the-fact bookkeeping.

Full schema, dispatch priority (a detected CLI always wins over a saved
key), and removal semantics:
[Web UI Chat Storage § Credentials & API-Key Mode](webui-chat-storage.md#credentials--api-key-mode-phase-4).

## Mobile Remote Boundaries

Official mobile remote surfaces are preferred:

- ChatGPT mobile **Remote** for Codex;
- Claude app **Code** for Claude Code.

The phone sends prompts and approvals, while the connected host provides files,
credentials, local tools, MCP servers, and shell access. Keep the host awake,
signed in, and trusted.

## Custom HTTP Remote Boundaries

`remote_handoff_server.py` is optional and intended for trusted automation.

Recommended defaults:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765
```

Avoid binding to public interfaces. If a non-local interface is required:

- use a strong token;
- restrict `--allow-root`;
- avoid `--allow-execute` unless the caller is trusted;
- prefer SSH tunnels or VPN access;
- monitor `.handoff/remote/tasks/`.

The server refuses `--no-auth` on non-local hosts.

## Workspace Safety

Before starting work:

- inspect `git status --short`;
- read `.handoff/current.md`;
- verify the target provider/model header;
- avoid broad refactors and unrelated edits;
- preserve user changes.

## Incident Response

If a secret is accidentally written:

1. Stop provider execution.
2. Remove the secret from the workspace.
3. Rotate the credential.
4. Check `git status` and staged content.
5. Inspect `.handoff/runs/` and delete local raw logs if they contain secrets.
6. Do not push until the history is clean.
