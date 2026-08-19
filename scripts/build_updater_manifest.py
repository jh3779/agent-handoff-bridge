#!/usr/bin/env python3
"""Assemble latest.json, the static update manifest Tauri's official
updater plugin (DEC-28, docs/design-system/flutter-mapping.html#s1c)
polls at
https://github.com/jh3779/agent-handoff-bridge/releases/latest/download/latest.json.

Schema and platform-key format (OS-ARCH, e.g. "windows-x86_64") confirmed
against https://v2.tauri.app/plugin/updater/ before implementing, not
assumed. Only one artifact per OS is included -- the same one Tauri's
`createUpdaterArtifacts=true` actually signs for that platform (the nsis
.exe on Windows, a separate .app.tar.gz -- not the .dmg itself -- on
macOS, the .AppImage on Linux); .msi is also signed but deliberately not
included here, matching this project's existing "one representative
installer per OS" precedent for what actually needs updater coverage
(docs/release-process.md step 7).

Run after `gh run download <run-id> --dir <dir>` (docs/release-process.md
step 6) has already pulled every `installers-<target-triple>` artifact
locally -- this script only reads already-downloaded files, it never
talks to GitHub itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handoff_bridge import BRIDGE_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GITHUB_REPO = "jh3779/agent-handoff-bridge"

# platform key (Tauri's OS-ARCH convention) -> (artifact subdir under
# installers-<target-triple>/, glob for the actual bundle file, matching
# release-process.md's per-OS target-triple list). The .sig file is
# always <bundle file>.sig, sitting in the same directory.
PLATFORMS: dict[str, tuple[str, str, str]] = {
    "windows-x86_64": ("x86_64-pc-windows-msvc", "nsis", "*.exe"),
    "darwin-aarch64": ("aarch64-apple-darwin", "macos", "*.app.tar.gz"),
    "linux-x86_64": ("x86_64-unknown-linux-gnu", "appimage", "*.AppImage"),
}


def find_one(pattern_dir: Path, glob: str) -> Path:
    matches = sorted(pattern_dir.glob(glob))
    if not matches:
        raise FileNotFoundError(f"no file matching {glob!r} in {pattern_dir}")
    if len(matches) > 1:
        raise RuntimeError(f"expected exactly one {glob!r} match in {pattern_dir}, found {len(matches)}: {matches}")
    return matches[0]


def build_manifest(installers_dir: Path, version: str, notes: str, tag: str) -> dict:
    platforms: dict[str, dict] = {}
    for platform_key, (target_triple, subdir, glob) in PLATFORMS.items():
        bundle_dir = installers_dir / f"installers-{target_triple}" / subdir
        if not bundle_dir.is_dir():
            raise FileNotFoundError(
                f"{bundle_dir} does not exist -- did `gh run download` actually pull "
                f"the installers-{target_triple} artifact?"
            )
        bundle_path = find_one(bundle_dir, glob)
        sig_path = bundle_path.with_name(bundle_path.name + ".sig")
        if not sig_path.exists():
            raise FileNotFoundError(
                f"{sig_path} does not exist -- was this build actually signed? "
                "(TAURI_SIGNING_PRIVATE_KEY missing in CI would still produce the "
                "unsigned bundle but no .sig file)"
            )
        signature = sig_path.read_text(encoding="utf-8").strip()
        download_url = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{bundle_path.name}"
        platforms[platform_key] = {"signature": signature, "url": download_url}
    return {
        "version": version,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installers-dir",
        default="/tmp/agent-handoff-bridge-installers",
        help="Directory `gh run download` saved the installers-<target-triple> artifacts into.",
    )
    parser.add_argument("--version", default=BRIDGE_VERSION, help="Version string for the manifest (default: BRIDGE_VERSION).")
    parser.add_argument("--tag", default=None, help="Git tag the release assets live under (default: v<version>).")
    parser.add_argument("--notes", default="", help="Short release notes shown in the update dialog.")
    parser.add_argument("--output", default=str(ROOT / "dist" / "latest.json"), help="Where to write the manifest.")
    args = parser.parse_args()

    tag = args.tag or f"v{args.version}"
    manifest = build_manifest(Path(args.installers_dir), args.version, args.notes, tag)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
