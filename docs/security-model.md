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

**Custom providers (DEC-26)** extend this same file/exception with a
user-supplied `base_url` per entry — a deliberate trust boundary, not an
oversight: the user who registers a custom provider is the same person
who chooses what `base_url` and API key to pair it with (this is a local,
single-user app, not a hosted service accepting untrusted input), so
there is no separate validation of the URL beyond requiring an
`http://`/`https://` scheme. The key is still sent only to whatever host
the user configured, over that connection, the same as the fixed three.
See [Web UI Chat Storage § Custom Providers](webui-chat-storage.md#custom-providers-dec-26).

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
  inner one all over again. A single-hop `pkill -P <pid>` (killing only
  *direct* children) isn't enough either: an in-flight provider run makes
  the real tree deeper (a second PyInstaller sidecar,
  `agent-handoff-bridge-cli`, spawned mid-run, which itself re-execs and
  spawns the real `codex`/`claude`/`gemini` subprocess) -- none of those
  are direct children of the tracked PID. On Unix, the fix walks the
  whole descendant tree via repeated `pgrep -P` before killing anything
  (a dead process's children can no longer be found by ppid), then
  signals children before parents in reverse-discovery order --
  gracefully (`SIGTERM`) first, escalating to `SIGKILL` only for whatever
  is still alive after a short grace period, so an in-flight provider CLI
  gets a chance to flush/clean up rather than always being hard-killed
  mid-write. Windows' `taskkill /T` handles the whole tree in one call
  (graceful attempt first, then `/F` force for survivors).
- **Real self-update (DEC-28)**: `tauri-plugin-updater` checks
  `.../releases/latest/download/latest.json` once per app launch and, on
  a confirmed Yes from the user, downloads and installs a new build,
  then restarts the app. This is a genuinely new capability -- the
  Tauri shell can now write and execute a new binary on its own -- so
  what it does and doesn't verify matters more here than for the
  read-only checks elsewhere in this document:
  - **What's verified**: every downloaded update artifact must carry a
    valid signature from this project's own Ed25519 keypair
    (`src-tauri/tauri.conf.json`'s `plugins.updater.pubkey`, checked
    against `latest.json`'s per-platform `signature` field) before
    `download_and_install()` will touch disk at all -- this is not
    optional or best-effort, Tauri's updater has no "skip verification"
    mode. The private half of that keypair exists only as the
    `TAURI_SIGNING_PRIVATE_KEY` GitHub Actions secret
    (`docs/release-process.md`'s "Signing Key" section has the full
    generation/rotation record) -- never on disk in this repo, never in
    a commit.
  - **What's explicitly not changed**: this is unrelated to, and does
    not upgrade, the "distributed installers ship unsigned" decision
    directly below (DEC-24) -- the OS itself (Gatekeeper/SmartScreen)
    still has no opinion about this app's publisher identity, signed or
    not. Tauri's own signature only proves "this update came from
    whoever holds the private key," not "the OS trusts this binary." A
    compromised `TAURI_SIGNING_PRIVATE_KEY` secret would let an attacker
    push a signed, auto-installed update to every existing user -- a
    meaningfully higher-stakes secret than anything else this project
    currently stores in CI, which is why `docs/release-process.md`
    documents treating a suspected leak as urgent, not routine key
    rotation.
  - **Confirmation, not silence**: matches DEC-02's existing "don't act
    without confirmation" posture applied to a new kind of action -- a
    native Yes/No dialog names the version and states the app will
    restart, before anything downloads. A failed *check* (no network,
    `latest.json` temporarily missing) fails silently, same posture as
    `check_for_update()`'s own existing DEC-19 behavior; a failed
    *install* (after the user already said yes) shows an error dialog
    instead, since the user is at that point actively expecting
    something to happen.
  - **Supersedes part of DEC-22**: DEC-22 (Phase 7 kickoff) explicitly
    chose *not* to adopt Tauri's own updater, specifically because "no
    documented private-repo support path exists." That premise no
    longer holds -- this repo has been public since v0.2.0, which is
    Tauri's actual blessed use case for a GitHub-Releases-hosted
    manifest. See DEC-28
    (`docs/design-system/flutter-mapping.html#s1c`) for the full record.
- **Distributed installers (`.dmg`/`.app`, `.msi`/nsis `.exe`,
  `.deb`/`.AppImage`/`.rpm`, built by CI's `installer-build` job) ship
  unsigned, by deliberate, final decision (DEC-24), not a "not done yet."**
  No macOS notarization, no Windows Authenticode signature. Confirmed
  real costs before deciding: macOS notarization requires an Apple
  Developer Program membership ($99/year -- a free Apple ID can sign but
  not notarize, and Gatekeeper refuses to open an unnotarized app at
  all); Windows requires purchasing an OV or EV code-signing certificate
  (EV avoids SmartScreen friction immediately but needs hardware-backed
  key storage -- a physical HSM or USB token, since file-based
  Authenticode certs alone stopped being accepted industry-wide after
  June 2023 -- or a cloud alternative like Azure Trusted Signing).
  Neither cost is justified for this project's current scale (the real
  userbase is still the operator plus a small number of known testers,
  same premise DEC-19 already uses -- the repo itself went public on
  2026-08-06, but that's a visibility change, not a userbase-size one).
  A sibling project by the same operator (`file-converter`, a separate
  repo) hit the identical fork and reached the same conclusion (its own
  DEC-029): ship both platforms unsigned, invest in clear bypass
  instructions instead of a signing budget. This project follows the
  same pattern:
  - **macOS**: the first launch, Gatekeeper blocks the app as being from
    an "unidentified developer" -- this is not a malware warning. In
    Finder, **control+click the app → "Open"** once; this bypasses the
    warning for good after that first confirmation.
  - **Windows**: SmartScreen shows a **blocking** red "Windows protected
    your PC" screen (not a dismissible warning) -- this is the normal,
    expected result of not having a paid code-signing certificate, not a
    malware detection. That first screen has no "Run" button visible;
    click the (non-button) **"More info"** text inside it, and a "Run
    anyway" button appears.
  Revisit this decision only if the premise changes (e.g. real users
  beyond the operator, or the project's distribution scale grows) --
  not on a fixed timeline.
  **Re-reviewed 2026-08-07**: a tester appearing and the repo going
  public (2026-08-06) narrows the gap toward that revisit condition, but
  the operator judged the userbase still small enough that the decision
  stands unchanged -- reaffirmed, not re-decided from scratch. Revisit
  again if the userbase or distribution scale grows further.
  See [Release Process](release-process.md) for
  how installers are built and published.

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
