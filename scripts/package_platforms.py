#!/usr/bin/env python3
"""Build macOS and Windows source packages for the handoff bridge."""

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "dist"
PACKAGE_NAME = "agent-handoff-bridge"

COMMON_FILES = [
    ".gitignore",
    ".handoff/.gitignore",
    ".handoff/current.md",
    ".handoff/task-template.md",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "handoff_bridge.py",
    "handoff_control.py",
    "handoff_desktop.py",
    "remote_handoff_server.py",
    "remote_handoff_submit.py",
    "schemas/handoff-summary.schema.json",
    "scripts/handoff_hook.py",
    "scripts/validate_handoff.py",
    "scripts/package_platforms.py",
    "examples/claude-settings.handoff.json",
    "examples/codex-hooks.handoff.json",
    "launchers/macos/handoff-bridge.command",
    "launchers/macos/install.sh",
    "launchers/windows/handoff-bridge.cmd",
    "launchers/windows/handoff-bridge.ps1",
    "launchers/windows/install.ps1",
]


START_HERE = {
    "macos": """Agent Handoff Bridge for macOS

1. Open Terminal in this folder.
2. Run:

   ./launchers/macos/install.sh
   ./launchers/macos/handoff-bridge.command

If the GUI cannot start because tkinter is missing, the launcher falls back to
the terminal controller. Install a Python 3 build with Tcl/Tk support for GUI
folder selection.
""",
    "windows": """Agent Handoff Bridge for Windows

1. Open Command Prompt in this folder.
2. Run:

   launchers\\windows\\handoff-bridge.cmd

PowerShell users can run:

   powershell -ExecutionPolicy Bypass -File .\\launchers\\windows\\install.ps1
   .\\launchers\\windows\\handoff-bridge.ps1

If the GUI cannot start because tkinter is missing, the launcher falls back to
the terminal controller. Install a Python 3 build with Tcl/Tk support for GUI
folder selection.
""",
}


def package_files() -> list[Path]:
    files = [ROOT / rel_path for rel_path in COMMON_FILES]
    files.extend(sorted((ROOT / "docs").glob("*.md")))
    return sorted(set(files))


def copy_files(stage: Path) -> None:
    for source in package_files():
        if not source.exists():
            raise FileNotFoundError(source.relative_to(ROOT))
        target = stage / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_start_here(stage: Path, platform_name: str) -> None:
    (stage / f"START_HERE_{platform_name.upper()}.txt").write_text(START_HERE[platform_name], encoding="utf-8")


def add_to_zip(zip_file: zipfile.ZipFile, path: Path, arcname: Path) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname.as_posix())
    mode = path.stat().st_mode
    info.external_attr = (mode & 0xFFFF) << 16
    if path.is_file():
        with path.open("rb") as handle:
            zip_file.writestr(info, handle.read())


def zip_stage(stage: Path, output_zip: Path) -> None:
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_dir():
                continue
            add_to_zip(archive, path, path.relative_to(stage.parent))


def build_package(platform_name: str, output_dir: Path) -> Path:
    stage_root = output_dir / "_stage"
    stage = stage_root / f"{PACKAGE_NAME}-{platform_name}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    copy_files(stage)
    write_start_here(stage, platform_name)
    output_zip = output_dir / f"{PACKAGE_NAME}-{platform_name}.zip"
    if output_zip.exists():
        output_zip.unlink()
    zip_stage(stage, output_zip)
    return output_zip


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build macOS and Windows release zips.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output directory.")
    parser.add_argument("--platform", choices=("macos", "windows", "all"), default="all")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    platforms = ("macos", "windows") if args.platform == "all" else (args.platform,)
    for platform_name in platforms:
        output_zip = build_package(platform_name, output_dir)
        print(output_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
