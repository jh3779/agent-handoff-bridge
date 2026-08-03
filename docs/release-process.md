# Release Process

How to cut a downloadable release of this repo. A release is a git tag plus
a GitHub Release with the macOS/Windows zips attached — nothing more.

## 1. Bump The Version

Edit `BRIDGE_VERSION` in `handoff_bridge.py` (single source of truth; read by
`--version`, `diagnose`, and `scripts/package_platforms.py`'s
`START_HERE_*.txt`).

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

## 4. Build The Packages

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

## 6. Publish The GitHub Release

```bash
gh release create vX.Y.Z \
  dist/agent-handoff-bridge-macos.zip \
  dist/agent-handoff-bridge-windows.zip \
  --title "vX.Y.Z" \
  --notes-file <(sed -n "/## vX.Y.Z/,/## /p" docs/release-notes.md | sed '$d')
```

The `--notes-file` command extracts just the new version's section out of
`docs/release-notes.md` so the release body and the changelog never drift
apart. Check the rendered notes with `gh release view vX.Y.Z --web` and edit
via `gh release edit vX.Y.Z --notes-file <file>` if the extraction looks
wrong (the `sed` range match is best-effort, not foolproof — verify before
trusting it for release notes with unusual heading text).

## 7. Verify

```bash
gh release view vX.Y.Z
```

Confirm both zip assets are attached and the download links work for an
account with repo access (this repo is private — see
[Security Model](security-model.md)).

## Notes

- `dist/` is gitignored; only the GitHub Release carries the built zips.
- Never rebuild and re-upload assets under an existing tag — cut a new patch
  version instead, so a version number always means one exact set of files.
