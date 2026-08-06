#!/usr/bin/env python3
"""Build the four PyInstaller sidecars Phase 7a's Tauri shell needs
(docs/design-system/roadmap.md's "7a 실제로 한 것", DEC-22).

A review round found these builds existed only as interactive shell
history from whoever first got them working -- no committed script
captured the exact flags, so nobody else could reproduce a working
sidecar build. This is that script. It is deliberately dev-only tooling
(like scripts/package_platforms.py), not something end users run, and
only targets this machine's own platform -- PyInstaller/Nuitka don't
cross-compile (docs/research-phase7-framework.md), so a real
cross-platform release still needs 7b's scope (Windows/Linux builds,
likely via per-OS CI runners).

Reuses handoff_bridge.INSTALL_FILES as the single source of truth for
which non-Python data files the CLI sidecar needs bundled, rather than
hardcoding a second copy of that list here -- the two would silently
drift apart otherwise.

Usage: python3 scripts/build_phase7a_sidecars.py
Requires: a venv with `pyinstaller` installed (this project's own
tests/CI never depend on it -- it's a build-time-only tool, same
tier of trust as `gh` in docs/release-process.md).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARIES_DIR = ROOT / "src-tauri" / "binaries"
WORK_DIR = ROOT / "build" / "pyinstaller"

sys.path.insert(0, str(ROOT))
from handoff_bridge import INSTALL_FILES  # noqa: E402


def run(args: list[str]) -> None:
    print(f"$ {' '.join(args)}")
    subprocess.run(args, cwd=ROOT, check=True)


def install_files_add_data_args() -> list[str]:
    """One --add-data flag per handoff_bridge.INSTALL_FILES entry, so the
    frozen agent-handoff-bridge-cli's install()/init() can populate a new
    workspace exactly the same set of files the unfrozen CLI does --
    PyInstaller onefile bundles Python code automatically but not
    arbitrary data files."""
    args = []
    for src, dest in INSTALL_FILES:
        dest_dir = str(Path(dest).parent)
        args += ["--add-data", f"{ROOT / src}:{dest_dir}"]
    return args


def build_server() -> None:
    """handoff_webui.py -- spawned directly by src-tauri/src/lib.rs.
    Needs webui/ bundled (the frontend it serves at GET /), but not
    INSTALL_FILES -- it never calls install_standard_files() itself,
    only the CLI sidecar does (via bridge_command_prefix())."""
    run(
        [
            "pyinstaller",
            "--onefile",
            "--name",
            "agent-handoff-bridge-server",
            "--distpath",
            str(BINARIES_DIR),
            "--workpath",
            str(WORK_DIR),
            "--specpath",
            str(WORK_DIR),
            "--add-data",
            f"{ROOT / 'webui'}:webui",
            "--clean",
            "--noconfirm",
            str(ROOT / "handoff_webui.py"),
        ]
    )


def build_cli() -> None:
    """handoff_bridge.py -- spawned by the server sidecar's
    bridge_command_prefix() for init/run when frozen. Needs every
    INSTALL_FILES entry bundled so a frozen `init` can actually populate
    a new workspace, plus tests/ for check()'s check_tests() step in the
    (rare, unfrozen-only -- see check_tests()'s own docstring) case
    someone runs the unfrozen validator against a workspace this sidecar
    itself installed into."""
    run(
        [
            "pyinstaller",
            "--onefile",
            "--name",
            "agent-handoff-bridge-cli",
            "--distpath",
            str(BINARIES_DIR),
            "--workpath",
            str(WORK_DIR),
            "--specpath",
            str(WORK_DIR),
            *install_files_add_data_args(),
            "--clean",
            "--noconfirm",
            str(ROOT / "handoff_bridge.py"),
        ]
    )


def build_validate() -> None:
    """scripts/validate_handoff.py -- spawned by the CLI sidecar's
    check() when frozen. --hidden-import flags below are stdlib
    submodules only reachable through check_tests()'s dynamic
    unittest.discover() of tests/*.py, not through validate_handoff.py's
    own static imports -- PyInstaller's dependency analysis only sees
    the latter, so these have to be listed explicitly or the frozen
    validator crashes with ModuleNotFoundError the first time an
    unfrozen check_tests() run (this script's own docstring: check_tests()
    skips entirely when frozen) would have needed them."""
    run(
        [
            "pyinstaller",
            "--onefile",
            "--name",
            "agent-handoff-bridge-validate",
            "--distpath",
            str(BINARIES_DIR),
            "--workpath",
            str(WORK_DIR),
            "--specpath",
            str(WORK_DIR),
            "--hidden-import",
            "unittest.mock",
            "--hidden-import",
            "shlex",
            "--hidden-import",
            "http.server",
            "--hidden-import",
            "io",
            "--hidden-import",
            "urllib.error",
            "--hidden-import",
            "urllib.request",
            "--hidden-import",
            "threading",
            "--hidden-import",
            "tempfile",
            "--hidden-import",
            "datetime",
            "--hidden-import",
            "subprocess",
            "--hidden-import",
            "shutil",
            "--hidden-import",
            "json",
            "--hidden-import",
            "os",
            "--clean",
            "--noconfirm",
            str(ROOT / "scripts" / "validate_handoff.py"),
        ]
    )


def build_scan() -> None:
    """scripts/scan_secrets.py -- spawned by the validate sidecar's
    check_secrets() when frozen. No extra data/hidden-imports needed --
    it only shells out to `git`, a real external binary, not another
    Python script (see check_secrets()'s own reasoning)."""
    run(
        [
            "pyinstaller",
            "--onefile",
            "--name",
            "agent-handoff-bridge-scan",
            "--distpath",
            str(BINARIES_DIR),
            "--workpath",
            str(WORK_DIR),
            "--specpath",
            str(WORK_DIR),
            "--clean",
            "--noconfirm",
            str(ROOT / "scripts" / "scan_secrets.py"),
        ]
    )


def main() -> int:
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    build_server()
    build_cli()
    build_validate()
    build_scan()
    print(f"\nBuilt 4 sidecars in {BINARIES_DIR}")
    print("Copy each to <name>-<rust-target-triple> (e.g. `rustc -vV`'s `host:` "
          "line) before `cargo tauri build` -- Tauri's sidecar naming convention "
          "(docs/research-phase7-framework.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
