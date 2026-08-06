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

The bridge (`handoff_bridge.py`) shells out to `codex`, `claude`, and (as
of Phase 5) `gemini`; each provider uses its own local auth and
permission model — this is unchanged. Gemini has no free auth-status
command to check (`docs/research-gemini-cli.md`), so `diagnose()`
deliberately does not probe it — see
[CLI Reference § Diagnose](cli-reference.md#diagnose).

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

## Tauri Shell Boundaries (Phase 7a, DEC-22)

The Tauri shell (`src-tauri/`) does not change any trust boundary this
document already establishes -- it wraps the existing Python backend
(PyInstaller sidecar) rather than replacing it, so the backend's own
already-documented posture applies unchanged: loopback-only HTTP with no
authentication, and (per DEC-21, `docs/design-system/flutter-mapping.html`)
a consciously-accepted unrestricted shell-exec tool in API-key mode. This
section covers only what's new because a native shell now exists.

- **The main window always loads the sidecar's real
  `http://127.0.0.1:8787/` URL, never Tauri's own bundled/asset-protocol
  content.** `src-tauri/capabilities/default.json` grants no permissions
  beyond `core:default` -- an earlier draft also granted
  `shell:allow-execute` for window `"main"`, matching Tauri's own
  scaffolding convention, but a review round pointed out that leaving an
  unused grant in place invites a future contributor to misjudge what's
  actually reachable. Tauri's permission/capability system gates IPC
  calls a *webview's own JS* initiates via `invoke(...)`, not calls the
  trusted Rust backend makes directly (`src-tauri/src/lib.rs` calls
  `app.shell().sidecar(...)` straight from Rust in `setup()`, never
  through IPC) -- so the grant was never load-bearing, and removing it
  was verified empirically (rebuilt and relaunched the actual `.app`;
  the sidecar still spawns and the window still renders correctly with
  it gone), not just reasoned about. If a future sub-phase adds a real
  Tauri command invokable *from* the loaded web content (e.g. wiring a
  native folder picker to replace the manual-path fallback -- see
  `docs/design-system/roadmap.md`'s 7a notes), whatever permission that
  needs should be added deliberately and scoped to exactly that command,
  not restored from here.
- **`tauri.conf.json`'s `"security": {"csp": null}` is similarly a
  no-op today**, not a deliberately widened attack surface: Tauri's CSP
  injection applies to responses served through its own asset/IPC
  protocol, not to arbitrary external `http://` content the window
  navigates to. Revisit this the same time the capability grant above
  gets revisited -- both assumptions hold only as long as the window's
  content is exactly "the same local Python server this project already
  runs and has already reasoned about," and no more.
- `tauri-plugin-dialog` is registered only for a fatal-startup-error
  path (a blocking native dialog if the sidecar dies before the window
  is ever created) -- it exposes no new command surface reachable from
  the frontend.
- **Sidecar process cleanup on app quit (Phase 7b M6).** Verified
  empirically that this was actually broken through 7a and 7b M1-M4: the
  `CommandChild` returned by spawning the sidecar was dropped immediately
  with no cleanup, so quitting the app left the sidecar running forever
  as a process reparented to `launchd`/init -- discovered via a real
  leftover orphan, still holding port 8787 hours after its parent app
  had exited. Fixed in `src-tauri/src/lib.rs`: the child is now kept in
  managed state and explicitly killed on `RunEvent::Exit` (not
  `ExitRequested`, which doesn't fire on a normal quit -- verified by
  logging every event a real quit actually produces). Killing just the
  tracked PID isn't enough either -- PyInstaller's onefile bootloader
  re-execs into a second process, and `CommandChild::kill()` (SIGKILL/
  TerminateProcess) only reaches the outer one, immediately orphaning the
  inner one all over again -- so the fix explicitly kills the whole
  process tree (`pkill -P`/`taskkill /T`) instead of relying on the
  bootloader to forward the signal itself.
- **Distributed installers (`.dmg`/`.app`, `.msi`/nsis `.exe`,
  `.deb`/`.AppImage`/`.rpm`, built by CI's `installer-build` job) are
  currently unsigned.** No macOS notarization, no Windows Authenticode
  signature -- code signing is deliberately deferred to a later "Phase
  7c" sub-phase (DEC-22, reaffirmed by DEC-23), since it introduces a
  new recurring cost (Apple Developer Program, $99/year+) and process
  this project has never had. Practically: macOS Gatekeeper will refuse
  to open the `.app` normally (right-click → Open, or `xattr -d
  com.apple.quarantine`, is required the first time) and Windows
  SmartScreen will show an "unknown publisher" warning. This is expected
  today, not a bug -- see [Release Process](release-process.md) for how
  installers are built and published, and `docs/design-system/
  roadmap.md`'s Phase 7 plan for when signing is expected to land.

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
