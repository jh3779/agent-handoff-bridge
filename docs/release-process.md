# Release Process

*([한글 번역](release-process.ko.md) available.)*

How to cut a tagged release of this repo. Since Phase 7b (DEC-23, resolving
CFL-09), a release ships **two parallel packaging tracks** attached to the
same version tag and the same GitHub Release:

1. **Source zips** (`scripts/package_platforms.py`) — git-free, but still
   requires the user's own Python 3. For terminal/CLI-only use: running
   `handoff_bridge.py` directly, scripting, headless environments. This is
   the original distribution model and stays exactly as it was.
2. **Desktop installers** (Tauri, `cargo tauri build`) — `.dmg`/`.app`
   (macOS, **Apple Silicon only** — the CI matrix has no
   `x86_64-apple-darwin` leg, so Intel Macs get no native installer today;
   this was left open during 7b M1's planning and still is, see
   `docs/design-system/roadmap.md`'s 7b plan item 2), `.msi`/nsis `.exe`
   (Windows), `.deb`/`.AppImage`/`.rpm` (Linux). Bundles Python via
   PyInstaller sidecars, so end users need no Python installation at all.
   For desktop GUI use. **Currently unsigned** (code signing is Phase 7c,
   a separate decision gate per DEC-22/DEC-23) — expect Gatekeeper ("could
   not verify")/SmartScreen ("unknown publisher") warnings until then; see
   [Security Model](security-model.md).

Neither track replaces the other — see DEC-23
(`docs/design-system/flutter-mapping.html#s1c`) for why both are kept.

## 1. Bump The Version

Edit `BRIDGE_VERSION` in `handoff_bridge.py` (single source of truth; read
by `--version`, `diagnose`, and `scripts/package_platforms.py`'s
`START_HERE_*.txt`). Also update `"version"` in `src-tauri/tauri.conf.json`
to match — Tauri doesn't read `BRIDGE_VERSION` automatically, so the two
have to be kept in sync by hand or the desktop app and the CLI zip will
report different version numbers for the same release.

## 2. Update Release Notes

Move the `## Unreleased` bullets in `docs/release-notes.md` under a new
`## vX.Y.Z — YYYY-MM-DD` heading, and leave `## Unreleased` empty above it for
whatever comes next.

## 3. Run The Full Validation Suite

```bash
python3 handoff_bridge.py check
```

Do not proceed if this fails — it is the same check CI runs on every pull
request. See [Quality Gates](quality-gates.md).

## 4. Build The Source Zips

```bash
python3 scripts/package_platforms.py
```

Produces `dist/agent-handoff-bridge-macos.zip` and
`dist/agent-handoff-bridge-windows.zip`. Sanity-check at least one of them
before publishing — extract it somewhere outside the repo and confirm it
runs standalone (no git repo, no reliance on files outside the zip):

```bash
cd /tmp && unzip -q /path/to/repo/dist/agent-handoff-bridge-macos.zip
cd agent-handoff-bridge-macos
python3 handoff_bridge.py --version
python3 handoff_bridge.py check
```

Both commands must pass with no provider tokens spent and no git repo
present. If `check` fails here but passed in step 3, a file used by `check`
is missing from `COMMON_FILES` in `scripts/package_platforms.py` — add it
and rebuild.

## 5. Commit, Tag, Push

```bash
git add -A
git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

Do this **before** building the desktop installers (step 6) — the
`installer-build` job builds whatever commit it's pointed at, so triggering
it against an unpushed working tree would silently ship installers for the
*previous* version.

## 6. Build The Desktop Installers

The `installer-build` CI job (`.github/workflows/ci.yml`) produces real,
per-OS installers, but it's deliberately gated to manual trigger
(`workflow_dispatch`) only — not on every PR/push like the rest of CI.
This was originally to avoid GitHub's private-repo Actions billing (10x
for macOS runners, 2x for Windows) on every push; the repo is public now,
so GitHub-hosted runner minutes are free, but a real bundle build is
still comparatively expensive in wall-clock time (WiX/NSIS downloads,
DMG creation), so it stays manual-trigger-only for now. Trigger it
against the tag just pushed in step 5, so the installers are built from
exactly the tagged commit:

```bash
gh workflow run ci.yml --ref vX.Y.Z
```

`gh workflow run` doesn't print the new run's ID, and it can take a few
seconds to appear — poll for it rather than grabbing whatever `gh run list`
returns immediately. `--limit 1` alone isn't safe here: if any *other*
manual `workflow_dispatch` run (unrelated to this release) landed more
recently, it would be the only one in a 1-row window and the tag match
would find nothing, every single poll, forever. Widen the window so this
release's run is still in it even if something else raced ahead, and bound
the retry so a genuine miss fails loudly instead of hanging:

```bash
run_id=""
for attempt in $(seq 1 30); do
  run_id=$(gh run list --workflow=ci.yml --event=workflow_dispatch --limit 20 \
    --json databaseId,headBranch,createdAt \
    -q '[.[] | select(.headBranch == "vX.Y.Z")] | sort_by(.createdAt) | last | .databaseId // empty')
  [ -n "$run_id" ] && break
  sleep 3
done
if [ -z "$run_id" ]; then
  echo "could not find the workflow_dispatch run for tag vX.Y.Z after 30 attempts -- check manually:"
  gh run list --workflow=ci.yml --event=workflow_dispatch --limit 20
  exit 1
fi
gh run watch "$run_id"
```

Download the artifacts once it's green:

```bash
gh run download "$run_id" --dir /tmp/agent-handoff-bridge-installers
```

`actions/upload-artifact@v4` preserves each format's subdirectory under the
matched files' common ancestor, so this produces
`installers-<target-triple>/<format>/<file>` (e.g.
`installers-aarch64-apple-darwin/dmg/agent-handoff-bridge_X.Y.Z_aarch64.dmg`),
not a flat directory — the format subdirectory (`dmg`, `macos`, `msi`,
`nsis`, `deb`, `appimage`, `rpm`) matches `.github/workflows/ci.yml`'s own
`src-tauri/target/release/bundle/<format>/` upload paths. Sanity-check at
least one installer by actually running it (install + launch the app,
confirm the titlebar update-check badge and a basic chat round-trip work)
— this can't be scripted the way the zip check in step 4 can, so do it
manually before publishing.

If any matrix leg fails, check the run's logs directly
(`gh run view "$run_id" --log-failed`) rather than guessing — Windows/Linux
bundling has real, previously-hit platform-specific failure modes (see
`docs/design-system/roadmap.md`'s "7b M3 실제로 한 것" for what's already
been worked around, e.g. a known upstream `linuxdeploy`/AppImage issue on
`ubuntu-latest`, tauri-apps/tauri#14796).

## 7. Publish The GitHub Release

```bash
gh release create vX.Y.Z \
  dist/agent-handoff-bridge-macos.zip \
  dist/agent-handoff-bridge-windows.zip \
  /tmp/agent-handoff-bridge-installers/installers-aarch64-apple-darwin/dmg/*.dmg \
  /tmp/agent-handoff-bridge-installers/installers-x86_64-pc-windows-msvc/nsis/*.exe \
  /tmp/agent-handoff-bridge-installers/installers-x86_64-unknown-linux-gnu/appimage/*.AppImage \
  --title "vX.Y.Z" \
  --notes-file <(sed -n "/## vX.Y.Z/,/## /p" docs/release-notes.md | sed '$d')
```

Attaching one installer per OS (`.dmg`, nsis `.exe`, `.AppImage`) keeps the
release page from being cluttered with near-duplicate formats — `.msi`,
`.app`, `.deb`, `.rpm` are also produced and can be attached too
(`gh release upload vX.Y.Z <file>`) if a user specifically asks for one of
those formats. Note in the release notes that installers are unsigned and
what warning each OS shows (see step 6's intro above).

The `--notes-file` command extracts just the new version's section out of
`docs/release-notes.md` so the release body and the changelog never drift
apart. Check the rendered notes with `gh release view vX.Y.Z --web` and edit
via `gh release edit vX.Y.Z --notes-file <file>` if the extraction looks
wrong (the `sed` range match is best-effort, not foolproof — verify before
trusting it for release notes with unusual heading text).

## 8. Verify

```bash
gh release view vX.Y.Z
```

Confirm the zip assets and at least one installer per OS are attached, and
the download links work — the repo is public, so this no longer requires
an account with repo access (see [Security Model](security-model.md)).

## Notes

- `dist/` and `src-tauri/target/` are both gitignored; only the GitHub
  Release carries the built zips and installers.
- Never rebuild and re-upload assets under an existing tag — cut a new patch
  version instead, so a version number always means one exact set of files.
- Both packaging tracks stay supported deliberately (DEC-23) — the source
  zip for terminal/scriptable use, the installers for desktop GUI use. Do
  not drop either without a fresh decision recorded the same way.
- **This runbook's installer track was run for real for the first time
  cutting v0.2.0** — a few things worth knowing before the next release:
  - `gh workflow run` actually printed the new run's URL directly on the
    `gh` version used, making the bounded polling loop in step 6
    unnecessary that time — but it's kept as documented since this isn't
    guaranteed across `gh` versions; if a bare URL comes back, just
    extract the run ID from it instead of polling.
  - The Windows `installer-build` leg hit its 30-minute job timeout
    during a post-job step (`Swatinem/rust-cache`'s cache-save cleanup)
    that runs *after* the actual build, verification, and artifact
    upload already completed successfully — so the job's reported
    `conclusion` was `cancelled`, not `success`, even though the real
    installer was already fully uploaded and downloadable. Don't treat a
    `cancelled` conclusion as an automatic failure without checking
    whether the artifact actually exists first
    (`gh api repos/<owner>/<repo>/actions/runs/<run_id>/artifacts`) — it
    might just be a timeout hitting during cleanup, not the build itself
    failing. If this keeps happening, `timeout-minutes` on that job may
    need raising.
  - `gh release create`/`gh release upload` both worked exactly as
    documented against the real downloaded artifacts.
