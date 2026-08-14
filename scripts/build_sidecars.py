#!/usr/bin/env python3
"""Build the four PyInstaller sidecars the Tauri shell needs
(docs/design-system/roadmap.md's "7a 실제로 한 것"/"7b 계획", DEC-22).

Originally scripts/build_phase7a_sidecars.py, macOS-only (a review round
found Phase 7a's builds existed only as interactive shell history, no
committed script). Phase 7b generalized it to run on any of macOS/Windows/
Linux -- the two platform-specific things PyInstaller/Tauri actually care
about (the --add-data path separator, and whether executables get a .exe
suffix) are handled below; everything else about *what* gets bundled is
identical across platforms (docs/research-phase7-framework.md: PyInstaller/
Nuitka don't cross-compile, so this script still only ever builds for the
machine it runs on -- producing all platforms means running it on all
three, e.g. one CI matrix job per OS).

Reuses handoff_bridge.INSTALL_FILES as the single source of truth for
which non-Python data files the CLI sidecar needs bundled, rather than
hardcoding a second copy of that list here -- the two would silently
drift apart otherwise.

Usage: python3 scripts/build_sidecars.py [--target-triple TRIPLE]
  --target-triple: Rust target triple to name the sidecars for (Tauri's
    externalBin convention: <name>-<target-triple>[.exe]). Defaults to
    asking `rustc -vV` for this machine's own host triple -- pass this
    explicitly in a context where rustc isn't installed or isn't the
    right source of truth (e.g. a CI job that knows its own target
    without needing Rust present at all).
Requires: a venv with `pyinstaller` installed (this project's own
tests/CI never depend on it -- it's a build-time-only tool, same
tier of trust as `gh` in docs/release-process.md).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BINARIES_DIR = ROOT / "src-tauri" / "binaries"
WORK_DIR = ROOT / "build" / "pyinstaller"

sys.path.insert(0, str(ROOT))
from handoff_bridge import INSTALL_FILES  # noqa: E402

SIDECAR_NAMES = (
    "agent-handoff-bridge-server",
    "agent-handoff-bridge-cli",
    "agent-handoff-bridge-validate",
    "agent-handoff-bridge-scan",
)


def run(args: list[str]) -> None:
    print(f"$ {' '.join(args)}")
    subprocess.run(args, cwd=ROOT, check=True)


def detect_target_triple() -> str:
    """`rustc -vV`'s `host:` line -- the same value Tauri's own build
    script (src-tauri/build.rs, via TAURI_ENV_TARGET_TRIPLE) uses to
    decide which sidecar filename to look for, confirmed against the
    real error message a review round hit in CI ("resource path
    binaries/agent-handoff-bridge-server-x86_64-unknown-linux-gnu
    doesn't exist"). Requires rustc on PATH -- pass --target-triple
    explicitly to skip this in a context that doesn't have Rust
    installed at all (this script itself never needs Rust; only the
    naming convention it produces does)."""
    # encoding="utf-8": consistent with every other subprocess call in
    # this project (see handoff_bridge.py's run_provider() for the
    # confirmed crash a missing encoding produces on a non-UTF-8-locale
    # Windows machine) -- `rustc -vV` output is normally pure ASCII, but
    # there is no reason for this one call to be the odd one out.
    result = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, encoding="utf-8", check=True)
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError(f"could not find 'host:' in `rustc -vV` output:\n{result.stdout}")


def add_data_arg(src: Path, dest_dir: str) -> list[str]:
    """PyInstaller's --add-data separator is platform-native (';' on
    Windows, ':' everywhere else -- exactly os.pathsep's definition on
    both), unlike the plain path arguments elsewhere in this script
    which PyInstaller/Python handle natively regardless of OS."""
    return ["--add-data", f"{src}{os.pathsep}{dest_dir}"]


def install_files_add_data_args() -> list[str]:
    """One --add-data flag per handoff_bridge.INSTALL_FILES entry, so the
    frozen agent-handoff-bridge-cli's install()/init() can populate a new
    workspace exactly the same set of files the unfrozen CLI does --
    PyInstaller onefile bundles Python code automatically but not
    arbitrary data files."""
    args = []
    for src, dest in INSTALL_FILES:
        dest_dir = str(Path(dest).parent)
        args += add_data_arg(ROOT / src, dest_dir)
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
            *add_data_arg(ROOT / "webui", "webui"),
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


def rename_for_tauri(target_triple: str) -> None:
    """PyInstaller writes plain <name>[.exe]; Tauri's externalBin
    convention (docs/research-phase7-framework.md) needs
    <name>-<target-triple>[.exe] sitting alongside it. Phase 7a did this
    by hand (`cp binary binary-aarch64-apple-darwin`) once per binary --
    automated here so a CI matrix job doesn't need a separate shell step
    per OS to get the naming right."""
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    for name in SIDECAR_NAMES:
        built = BINARIES_DIR / f"{name}{exe_suffix}"
        target = BINARIES_DIR / f"{name}-{target_triple}{exe_suffix}"
        if not built.exists():
            raise FileNotFoundError(f"expected PyInstaller to produce {built}, but it's missing")
        shutil.copy2(built, target)
        print(f"  {target.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--target-triple",
        default=None,
        help="Rust target triple to name the sidecars for (default: this machine's own host triple, via `rustc -vV`).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    target_triple = args.target_triple or detect_target_triple()

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    build_server()
    build_cli()
    build_validate()
    build_scan()

    print(f"\nRenaming for Tauri's sidecar convention (target triple: {target_triple}):")
    rename_for_tauri(target_triple)

    print(f"\nBuilt and renamed 4 sidecars in {BINARIES_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
