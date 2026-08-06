# Research: Framework Migration (Phase 7, DEC-01, resolves CFL-14; CFL-09 later resolved by DEC-23 in Phase 7b M4)

Date: 2026-08-05

## Bottom Line

Both Tauri and Electron can, per their own official docs, keep this
project's ~2200-line Python backend (`handoff_webui.py`) unchanged and run
it as a subprocess ("sidecar" in Tauri's vocabulary), and both can keep the
existing vanilla JS/CSS/HTML frontend (`webui/`) near-verbatim as a first
pass. The real divergence is packaging maturity for exactly this shape:
**Tauri has an official, first-class, config-driven sidecar mechanism
(`externalBin`) whose own docs name "Python CLI applications or API servers
bundled using pyinstaller" as a primary use case.** Electron has no
equivalent *core* concept — the community-standard path (electron-builder's
`extraResources` + `child_process`/`utilityProcess`) works and is
well-trodden, but is third-party tooling layered on top, not something
Electron itself ships or documents as "the" way to do it.

On auto-update, the comparison flips: this project's current
`check_for_update()` reads a **private** GitHub repo by reusing the
operator's own local `gh` CLI auth (DEC-19). **Neither framework's official
updater reproduces that trick, and Tauri's has no documented private-repo
story at all** (community workarounds are proxy servers or a
separate-public-repo hack). Electron's ecosystem updater (`electron-updater`)
does document a private-repo mode (`private: true` + a `GH_TOKEN`), but its
own docs call it "only for very special cases" and it requires embedding a
token in every installed client plus accepting GitHub API rate limits — a
real, unresolved design question either way, not a "just enable it" switch.

Code signing/notarization (macOS: Apple Developer Program, $99/yr, plus
notarization; Windows: a code-signing cert, EV or Azure Trusted Signing to
avoid SmartScreen warnings on day one) is a **brand-new recurring cost and
process this project has never had** (current zips are unsigned raw Python
source). This cost is identical regardless of which framework is chosen —
the framework only changes how the build pipeline invokes signing, not
whether it's required for a clean install experience.

## Sidecar: Can The Existing Python Backend Survive The Migration?

Yes, per both vendors' own docs, without a rewrite:

- **Tauri** (`v2.tauri.app/develop/sidecar/`): declare the binary under
  `bundle.externalBin` in `tauri.conf.json`, name one build per OS/arch
  target-triple (e.g. `agent-handoff-bridge-x86_64-pc-windows-msvc.exe`),
  spawn it via `tauri_plugin_shell`. This is architecturally close to what
  `handoff_webui.py` already does today when it shells out to
  `handoff_bridge.py` as a subprocess to avoid `os.chdir()` races — except
  the *whole* HTTP server becomes the sidecar, spawned once at app start
  instead of per-request.
- **Electron**: no first-class "sidecar" feature in Electron core.
  electron-builder's `extraResources` (third-party but ecosystem-standard)
  copies the binary into the packaged app; the app then `spawn()`s it via
  `child_process` or Electron's own `utilityProcess` API. Achievable and
  common, but assembled from two separate tools rather than one documented
  feature the way Tauri's `externalBin` is.

**Producing the sidecar binary itself**: PyInstaller (`--onefile`, "freezes
Python programs into stand-alone executables... run with a simple
double-click, without requiring Python to be installed") or Nuitka
(`--mode=standalone`). Neither cross-compiles — each target OS needs its own
build, same constraint `scripts/package_platforms.py` already has today
building separate per-OS zips. Known friction: PyInstaller's own issue
tracker documents recurring **antivirus false-positive flags** on
`--onefile` output (the maintainers re-sign the bootloader each release but
this isn't fully solved) — worth testing early if this path is chosen,
since it directly affects the "no scary warnings" goal signing is otherwise
meant to achieve.

## Auto-Update: The Private-Repo Gap

- **Tauri's updater plugin** (`v2.tauri.app/plugin/updater/`): mandatory
  signature verification (a keypair from `tauri signer generate`; losing
  the private key permanently breaks pushing updates to existing installs),
  an `endpoints` array pointing at a manifest URL. Confirmed via Tauri's
  own GitHub Discussions (#7553, #2776) that **private-repo release assets
  cannot be authenticated inline through this mechanism** — every
  workaround in the community is a self-hosted proxy or a separate public
  repo just for update artifacts.
- **Electron / electron-updater**: Electron's own docs recommend
  `update-electron-app` on the free `update.electronjs.org` service
  (macOS-signing required, no Windows signing requirement stated) but don't
  mention `electron-updater` at all, even though it's the ecosystem's de
  facto standard. `electron-updater`'s own docs (electron.build) **do**
  document `private: true` + a `GH_TOKEN` env var for private-repo support,
  but flag it as "only for very special cases" and note the GitHub API rate
  limit (5000 req/hr, ~3 requests per check) as a real constraint. This
  requires a token reachable by every end-user's installed app — arguably a
  weaker trust model than today's "reuse the operator's own already
  authenticated `gh`."

Net: adopting either framework's *own* updater as designed most likely
means either making releases public, or building/hosting a small custom
relay that holds credentials server-side — not something either vendor
supports out of the box for a project that wants to stay private.

## Code Signing / Notarization

Identical requirements regardless of framework (Gatekeeper/SmartScreen
react to the binary, not the shell that produced it):

- **macOS**: Apple Developer Program account ($99/yr) — a free Apple ID can
  sign but not notarize, and Gatekeeper blocks unsigned/unnotarized apps
  outright ("broken and can not be started"). Sign with a Developer ID
  Application cert, then submit for notarization (Apple ID + app-specific
  password + Team ID, or App Store Connect API credentials). Requires an
  actual Mac to sign (both frameworks' docs note this).
- **Windows**: traditional OV certs are cheaper but still trigger
  SmartScreen warnings until the binary earns "reputation"; EV certs avoid
  this immediately but require hardware-backed key storage (a physical HSM
  or token) since simple file-based Authenticode certs stopped being
  sufficient industry-wide after June 2023. Both vendors' current docs
  point to Azure Trusted Signing/Azure Artifact Signing as a newer,
  cloud-based alternative to owning HSM hardware (geographic eligibility
  restrictions apply per Tauri's docs).

This project has neither cost today. Whichever framework is chosen, this is
a new decision: pay for signing now, or ship unsigned first and accept the
OS warnings while deferring signing to a later sub-phase.

## Frontend: Is The Framework Choice Separable From A Frontend Rewrite?

Technically yes, per both vendors' own getting-started docs — confirmed,
not assumed:

- **Tauri**'s official scaffolding tool (`create-tauri-app`) offers a
  vanilla HTML/CSS/JS template as a first-class option alongside
  React/Svelte/Vue, recommended for projects "unfamiliar with web
  development or [without] a favorite frontend stack." Native APIs are
  reachable from vanilla JS via `@tauri-apps/api` imports or a
  `window.__TAURI__` global.
- **Electron**'s minimal tutorial app is exactly
  `BrowserWindow.loadFile('index.html')` pointed at a plain folder — no
  framework implied. IPC (`ipcRenderer`/`ipcMain`) and native dialogs are
  plain JS APIs, callable the same way `webui/app.js` already calls
  `fetch()` against this project's own HTTP endpoints today.

**Practical caveat, not a documented fact — a judgment call**: DEC-01's
original stated reason for wanting a framework migration at all was native
animation/interaction quality (message bubbles, drawers, drag-over states)
that vanilla DOM manipulation was already struggling with in this codebase.
Projects that adopt Tauri/Electron *specifically* for that reason often end
up reaching for a frontend framework (React/Vue/Svelte) anyway, since the
underlying pain point (vanilla JS state management for nuanced transitions)
doesn't go away just because the window chrome changed. Whether this phase
does both migrations at once, or ships the shell/packaging change first and
defers a frontend framework to a later sub-phase, is a real open question.

## Open Decisions (Blocks Design Interview)

1. **Tauri vs Electron.** Tauri: official first-class Python-sidecar
   support (matches this project's actual shape), smaller/lighter, younger
   (v2 stable since 2024-10-02), no first-party
   private-repo updater story. Electron: a decade of production maturity
   (VS Code, Slack, Figma, 1Password, Claude desktop, per Electron's own
   homepage), documented (if awkward) private-repo updater path via
   `electron-updater`, larger bundle/heavier runtime, sidecar support is
   third-party-assembled rather than a core feature.
2. **Keep the Python backend as a sidecar, or rewrite backend logic
   natively (Rust for Tauri / Node.js for Electron)?** Sidecar preserves
   ~2200 lines of working, tested (353 tests) logic and this project's
   established "avoid new heavy dependencies" posture; a native rewrite is
   a dramatically larger, riskier undertaking with no clear benefit
   identified in this research.
3. **Auto-update strategy given the private-repo gap.** Make releases
   public, build a small self-hosted relay, or keep the current
   custom-`gh`-based check indefinitely regardless of which shell
   framework is adopted.
4. **Frontend scope for this phase.** Carry `webui/` over close to
   verbatim as a first pass (defer a frontend framework to later), or
   adopt a frontend framework in the same phase to actually deliver the
   native animation quality DEC-01 originally wanted.
5. **Code signing timing.** Accept the new recurring cost now, or ship
   unsigned first and accept OS warnings, deferring signing to a later
   sub-phase.

Secondary, not blocking initial design: CI needs macOS + Windows build
capability either way (already true today via `package_platforms.py`'s
per-OS zips); this research did not benchmark actual sidecar startup
latency for this specific codebase, only general PyInstaller/Nuitka
behavior.

## Sources

- [Tauri — Embedding External Binaries (Sidecar), v2 docs](https://v2.tauri.app/develop/sidecar/)
- [Tauri — Updater plugin, v2 docs](https://v2.tauri.app/plugin/updater/)
- [Tauri — Core Architecture, v2 docs](https://v2.tauri.app/concept/architecture/)
- [Tauri — What is Tauri? / Start, v2 docs](https://v2.tauri.app/start/)
- [Tauri — Create a Project, v2 docs](https://v2.tauri.app/start/create-project/)
- [Tauri — macOS code signing, v2 docs](https://v2.tauri.app/distribute/sign/macos/)
- [Tauri — Windows code signing, v2 docs](https://v2.tauri.app/distribute/sign/windows/)
- [Tauri 2.0 Stable Release announcement](https://v2.tauri.app/blog/tauri-20/)
- [Tauri GitHub Discussion #7553 — Private updates with GitHub Releases](https://github.com/orgs/tauri-apps/discussions/7553)
- [Tauri GitHub Discussion #2776 — Simple guide on setting up auto updater](https://github.com/tauri-apps/tauri/discussions/2776)
- [Electron — Homepage](https://www.electronjs.org/)
- [Electron — Updating Applications (autoUpdater, update-electron-app)](https://www.electronjs.org/docs/latest/tutorial/updates)
- [Electron — Code Signing](https://www.electronjs.org/docs/latest/tutorial/code-signing)
- [Electron — utilityProcess API](https://www.electronjs.org/docs/latest/api/utility-process)
- [Electron — Building your First App (loadFile/BrowserWindow)](https://www.electronjs.org/docs/latest/tutorial/tutorial-first-app)
- [electron-builder — Auto Update docs](https://www.electron.build/docs/features/auto-update/)
- [electron-builder — Publish docs](https://www.electron.build/docs/publish/)
- [electron-builder — Application Contents (extraResources)](https://www.electron.build/docs/contents/)
- [electron-userland/electron-builder Issue #2314 — private repo auto-update](https://github.com/electron-userland/electron-builder/issues/2314)
- [Microsoft Learn — Distribute your app and the WebView2 Runtime](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)
- [Microsoft Learn — Evergreen vs. fixed version of the WebView2 Runtime](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/evergreen-vs-fixed-version)
- [PyInstaller — What PyInstaller Does and How It Does It](https://pyinstaller.org/en/stable/operating-mode.html)
- [PyInstaller GitHub Issue #6754 — antivirus false positives](https://github.com/pyinstaller/pyinstaller/issues/6754)
- [Nuitka — User Manual](https://nuitka.net/user-documentation/user-manual.html)
- [Nuitka GitHub — cross-compilation discussion, Issue #43](https://github.com/Nuitka/Nuitka/issues/43)
- Bundle-size/memory ballpark figures only (non-authoritative — neither
  vendor publishes a head-to-head number): [PkgPulse — Electron vs Tauri
  2026](https://www.pkgpulse.com/guides/electron-vs-tauri-2026),
  [tech-insider.org — Tauri vs Electron
  2026](https://tech-insider.org/tauri-vs-electron-2026/)
