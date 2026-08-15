"""Foundational helpers shared across the webui_*.py modules: the
subprocess boundary into handoff_bridge.py's CLI, small pure utilities
(utc_now/month_key), and the one exception type (WorkspaceError) every
other module raises for a client-facing 400.

Structure audit (2026-08-15): handoff_webui.py used to be a single
2600+ line file. Split into these modules by domain -- workspace/file
tree, chat storage, credentials, API-key-mode provider calls, and
bridge-subprocess dispatch -- each importing from this one for the
handful of things every domain needs. handoff_webui.py itself keeps
only the HTTP routing layer, AppState/Api, and process entry point.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from handoff_bridge import short_run

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "handoff_bridge.py"


def bridge_command_prefix() -> list[str]:
    """The argv prefix for invoking handoff_bridge.py's CLI as a
    subprocess -- normally `[sys.executable, str(BRIDGE_SCRIPT)]` (plain
    `python3 handoff_bridge.py`), same as this project has always done.

    Phase 7a (DEC-22, docs/research-phase7-framework.md): when this
    module is itself running frozen (PyInstaller, as the Tauri sidecar
    `agent-handoff-bridge-server`), `sys.executable` is the frozen
    *server* binary, not a real Python interpreter -- passing it
    `str(BRIDGE_SCRIPT)` as an argument would not run that script, it
    would just re-launch the server binary with a nonsense argv. A
    second, sibling PyInstaller binary built from handoff_bridge.py
    (`agent-handoff-bridge-cli` in tauri.conf.json's `bundle.externalBin`,
    Tauri places every declared sidecar in the same directory as this
    one at runtime) is invoked directly instead, with no `sys.executable`
    prefix -- it needs no Python interpreter of its own, it *is* one.
    Only this call-site construction changes; handoff_bridge.py's own
    code is untouched.
    """
    if getattr(sys, "frozen", False):
        # PureWindowsPath/PurePosixPath, not the host-native Path: sys.executable
        # always matches sys.platform's flavor for a real frozen build (this
        # branch never runs unfrozen), but picking the pure class explicitly
        # keeps this correct regardless of which OS actually executes the code
        # -- and lets it be unit-tested for either platform from any dev host.
        cli_name = "agent-handoff-bridge-cli.exe" if sys.platform == "win32" else "agent-handoff-bridge-cli"
        pure_path = PureWindowsPath if sys.platform == "win32" else PurePosixPath
        return [str(pure_path(sys.executable).parent / cli_name)]
    return [sys.executable, str(BRIDGE_SCRIPT)]


# Structure audit: this module used to be the one bridge-invoking consumer
# that imported handoff_bridge's pure decision-logic functions
# (check_for_update/choose_auto_provider/next_available_provider) and
# called them in-process, while every other consumer only ever reached
# handoff_bridge.py's behavior through a subprocess call. The three
# wrappers below close that asymmetry by going through
# handoff_bridge.py's own check-update/resolve-auto-provider/next-provider
# subcommands instead -- same bridge_command_prefix()/short_run() pattern
# every other subprocess call in this file already uses.


def _bridge_check_for_update() -> dict:
    exit_code, stdout, _stderr = short_run(bridge_command_prefix() + ["check-update"])
    if exit_code == 0:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return data
    return {"status": "unavailable"}


def _bridge_resolve_auto_provider(workspace: Path) -> str:
    exit_code, stdout, _stderr = short_run(
        bridge_command_prefix() + ["--workspace", str(workspace), "resolve-auto-provider"]
    )
    return stdout.strip() if exit_code == 0 and stdout.strip() else "codex"


def _bridge_next_provider(current: str) -> str:
    exit_code, stdout, _stderr = short_run(bridge_command_prefix() + ["next-provider", current])
    return stdout.strip() if exit_code == 0 and stdout.strip() else current


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


class WorkspaceError(ValueError):
    """A path request fell outside the workspace root or doesn't exist."""


# DEC-05: exact location the wireframe promises the user. Lives here (not
# webui_workspace.py, its most obvious single owner) because
# webui_chat_storage.py's registry_path() and webui_credentials.py's
# credentials_path() both need it too, and webui_workspace.py itself
# already needs to import from webui_chat_storage.py (create_workspace_for_
# first_message() calls ensure_chat_gitignore()) -- putting it there would
# make that a circular import.
AUTO_WORKSPACE_BASE_DIR = Path.home() / "Documents" / "Agent Handoff Bridge"

