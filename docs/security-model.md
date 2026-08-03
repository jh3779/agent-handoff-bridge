# Security Model

This project is designed around conservative defaults. The bridge should make
handoffs easier without hiding provider permissions or exposing credentials.

## Default Safety Properties

- Provider runs are preview-only unless `--execute` is present.
- HTTP remote execution is disabled unless the server starts with
  `--allow-execute`.
- Existing support files are not overwritten by `install` unless `--force` is
  provided.
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
- `.handoff/mobile-*-instruction.txt`.

The bridge shells out to `codex` and `claude`; each provider uses its own local
auth and permission model.

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
