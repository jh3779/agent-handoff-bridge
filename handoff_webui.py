#!/usr/bin/env python3
"""MVP web UI: browse a workspace's files, switch workspace like VS Code's
"Open Folder", and keep a local, per-workspace chat draft history. No
provider is called from here -- see docs/provider-extensibility.md and
docs/design-system/ for the full chat-redesign this is the first slice of.

Serves the static app in webui/ plus a small JSON API scoped to the active
workspace (mutable at runtime via "Open Folder", not just --workspace at
startup). Stdlib only, local-only by default (127.0.0.1) -- consistent with
remote_handoff_server.py's posture, but with a much smaller write surface:
the only thing this can write is its own chat log under
<workspace>/.handoff/webui/chat/.
"""

from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qs, urlparse

from handoff_bridge import (
    HANDOFF_DIR,
    PROVIDERS,
    WriteLock,
    atomic_write_text,
    check_for_update,
    choose_auto_provider,
    next_available_provider,
    normalize_path,
    short_run,
)

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "handoff_bridge.py"

# Phase 7b M6: a stable, OS-independent stderr marker for "the port is
# already in use" -- src-tauri/src/lib.rs used to detect this by matching
# raw OSError text (POSIX's "Address already in use" vs. Windows'
# WSAEADDRINUSE wording, which can itself be localized per system
# language), which a review round pointed out was fragile. Printing this
# fixed string ourselves, once, means the Rust side never has to guess at
# what a given Python/OS combination's exception text looks like.
PORT_CONFLICT_MARKER = "AHB_PORT_CONFLICT"


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

try:
    import webview  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - depends on optional local install
    webview = None  # type: ignore[assignment]
    WEBVIEW_IMPORT_ERROR = exc
else:
    WEBVIEW_IMPORT_ERROR = None

WEBUI_ROOT = Path(__file__).resolve().parent / "webui"

# Directories never worth showing in the tree: huge, binary, or noise.
EXCLUDED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv"}

# Refuse to read/return file content above this size; still listable, just
# not previewable/attachable-with-content in this MVP.
MAX_FILE_BYTES = 256_000

# Chat history lives inside the workspace itself (like .handoff/current.md)
# so it travels with the project when copied, synced, or committed
# elsewhere -- not in some separate machine-global app-data folder.
CHAT_DIR_RELATIVE = Path(".handoff") / "webui" / "chat"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


class WorkspaceError(ValueError):
    """A path request fell outside the workspace root or doesn't exist."""


def safe_join(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` under `root`, refusing any escape.

    Rejects absolute paths, `..` traversal, and symlinks that resolve
    outside `root` -- checked against the *resolved* (symlink-following)
    root and target, not the literal string, so `../` tricks and symlink
    escapes are both caught.
    """
    if rel_path is None:
        rel_path = ""
    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise WorkspaceError(f"absolute paths are not allowed: {rel_path}")
    resolved_root = root.resolve()
    resolved_target = (resolved_root / candidate).resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise WorkspaceError(f"path escapes workspace: {rel_path}")
    return resolved_target


def is_probably_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def list_tree_entries(root: Path, rel_path: str) -> list[dict]:
    target = safe_join(root, rel_path)
    if not target.exists():
        raise WorkspaceError(f"does not exist: {rel_path}")
    if not target.is_dir():
        raise WorkspaceError(f"not a directory: {rel_path}")

    dirs: list[dict] = []
    files: list[dict] = []
    for entry in target.iterdir():
        if entry.name in EXCLUDED_DIR_NAMES:
            continue
        entry_rel = entry.relative_to(root.resolve()).as_posix()
        if entry.is_dir():
            dirs.append({"name": entry.name, "path": entry_rel, "type": "dir"})
        elif entry.is_file():
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            files.append({"name": entry.name, "path": entry_rel, "type": "file", "size": size})
    dirs.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["name"].lower())
    return dirs + files


def read_file_preview(root: Path, rel_path: str) -> dict:
    target = safe_join(root, rel_path)
    if not target.exists() or not target.is_file():
        raise WorkspaceError(f"not a file: {rel_path}")
    size = target.stat().st_size
    with target.open("rb") as handle:
        sample = handle.read(min(size, MAX_FILE_BYTES))
    if is_probably_binary(sample):
        raise WorkspaceError(f"binary file, no preview: {rel_path}")
    truncated = size > MAX_FILE_BYTES
    content = sample.decode("utf-8", errors="replace")
    return {
        "name": target.name,
        "path": Path(rel_path).as_posix(),
        "size": size,
        "content": content,
        "truncated": truncated,
    }


def validate_workspace_candidate(raw_path: str) -> Path:
    """Validate a user-supplied absolute folder path for "Open Folder".

    Unlike safe_join(), this is *meant* to point outside the current
    workspace -- it's choosing a new root, not resolving inside one. Still
    rejects the obviously wrong shapes (empty, not absolute, not a real
    directory) so a bad client can't wedge the server into a broken state.
    """
    if not raw_path or not raw_path.strip():
        raise WorkspaceError("no path given")
    # The is_absolute() check happens before normalize_path()'s resolve()
    # step (not after) so a relative path is rejected on its own terms,
    # with the original raw_path in the error message, rather than
    # silently resolved against this process's cwd first.
    if not Path(raw_path).expanduser().is_absolute():
        raise WorkspaceError(f"must be an absolute path: {raw_path}")
    resolved = normalize_path(raw_path)
    if not resolved.exists():
        raise WorkspaceError(f"does not exist: {raw_path}")
    if not resolved.is_dir():
        raise WorkspaceError(f"not a directory: {raw_path}")
    return resolved


# ---------------------------------------------------------------------------
# Phase 2 (docs/design-system/roadmap.md, SCR-05): auto-create a workspace
# under ~/Documents/Agent Handoff Bridge/ when none is selected, instead of
# forcing a folder pick before the user can send a single message. Design
# decisions (DEC-04~07) recorded in
# docs/design-system/flutter-mapping.html#s1c -- resolved via a pre-
# implementation interview, not invented here.
# ---------------------------------------------------------------------------

# DEC-05: exact location the wireframe promises the user.
AUTO_WORKSPACE_BASE_DIR = Path.home() / "Documents" / "Agent Handoff Bridge"

_SLUG_NON_WORD_RE = re.compile(r"[^\w]+")
MAX_SLUG_LENGTH = 40


def has_handoff_marker(path: Path) -> bool:
    """True if `path` already looks like an initialized handoff workspace.

    DEC-04: the no-flag default (cwd) only opens directly if this is true;
    otherwise the "no workspace" flow (this section) takes over instead of
    assuming a random directory is the intended project.
    """
    return (path / HANDOFF_DIR).is_dir()


def resolve_startup_workspace(raw_arg: str | None, cwd: Path) -> tuple[Path | None, str | None]:
    """Implements DEC-04. Returns (workspace, error) -- exactly one is set.

    An explicitly-given `--workspace` is validated exactly as before (a
    typo'd path fails loudly, never silently falls into auto-create --
    an explicit path means the user is confident about where they meant to
    point, so a mistake there should be surfaced, not "fixed" for them).
    The no-flag default only resolves to cwd if it already looks like an
    initialized workspace (`has_handoff_marker`); otherwise this returns
    (None, None) so the caller starts in the "no workspace" state instead
    of assuming the process's cwd -- often just wherever a launcher was
    double-clicked from -- is the intended project.
    """
    if raw_arg is not None:
        candidate = Path(raw_arg).expanduser().resolve()
        if not candidate.exists() or not candidate.is_dir():
            return None, f"workspace does not exist or is not a directory: {candidate}"
        return candidate, None
    candidate = cwd.expanduser().resolve()
    if has_handoff_marker(candidate):
        return candidate, None
    return None, None


def slugify_for_folder_name(text: str) -> str:
    """Local-only, no-token folder-name slug (DEC-05) -- deliberately not a
    provider-generated summary, so opening a fresh chat never spends
    tokens just to name its own folder. `\\w` is Unicode-aware, so this
    preserves non-ASCII text (Hangul etc.) instead of stripping it the way
    a typical ASCII-only slugify would -- the wireframe's own example
    keeps Korean text in the folder name.
    """
    collapsed = _SLUG_NON_WORD_RE.sub("-", text.strip()).strip("-")
    if not collapsed:
        return "untitled"
    return collapsed[:MAX_SLUG_LENGTH].strip("-") or "untitled"


def resolve_first_message_summary_source(text: str, attachments: list[dict]) -> str:
    """Shared by the folder-name slug and the recorded task (below) so an
    attachments-only first message (composer allows sending with no typed
    text) gets a meaningful task instead of the two drifting independently."""
    return text.strip() or next((a.get("name") for a in attachments if a.get("name")), "")


def build_auto_workspace_name(text: str, attachments: list[dict], now: datetime) -> str:
    summary_source = resolve_first_message_summary_source(text, attachments)
    return f"{now.strftime('%Y-%m-%d')}-{slugify_for_folder_name(summary_source)}"


def resolve_task_for_first_message(text: str, attachments: list[dict]) -> str:
    """The folder name can fall back to an attachment's name (above), but
    that alone used to never reach `.handoff/state.json`'s `task` -- an
    attachments-only first message got the generic "Continue the current
    handoff task." placeholder there instead, weakening every future
    prompt's "## Task" section (docs/architecture.md: state.json's task is
    durable context) even though the folder name itself was meaningful.
    """
    stripped = text.strip()
    if stripped:
        return stripped
    summary_source = resolve_first_message_summary_source(text, attachments)
    if summary_source:
        return f"Review attached file: {summary_source}"
    return "Continue the current handoff task."


# Guards the check-then-create in do_POST's /api/chat handler: without
# this, two near-simultaneous first messages (a double-clicked Send, two
# browser tabs against the same server) can both observe
# AppState.workspace is None and both call create_workspace_for_first_message()
# -- confirmed by reproduction, not theoretical: two real folders get
# created and one request's persisted chat message ends up orphaned in
# whichever folder AppState.workspace didn't end up pointing at. A plain
# threading.Lock, not handoff_bridge.WriteLock or _RUN_LOCK -- this is a
# separate, narrow critical section (mkdir + a sub-second init subprocess),
# not the provider-call-duration concern _RUN_LOCK exists for.
_WORKSPACE_CREATE_LOCK = threading.Lock()


def create_workspace_for_first_message(text: str, attachments: list[dict]) -> Path:
    """DEC-05/06: create the new workspace directory (numeric-suffixing on
    a name collision) and scaffold it exactly like a manually-picked folder
    would be -- `handoff_bridge.py init` (which installs the standard
    files first unless told not to) run as a subprocess for the same
    chdir-safety reason `run_provider_via_bridge()` shells out instead of
    calling in-process. `resolve_task_for_first_message()`'s result becomes
    the recorded task, so it also feeds the "## Task" section of every
    future prompt in this workspace.
    """
    base_name = build_auto_workspace_name(text, attachments, utc_now())
    try:
        AUTO_WORKSPACE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        # .resolve() to match the other two ways AppState.workspace ever
        # gets set (resolve_startup_workspace(), validate_workspace_candidate())
        # -- Path.home() doesn't itself resolve symlinks (e.g. ~/Documents
        # under iCloud Desktop & Documents sync), so without this, the same
        # physical folder reached different ways (auto-create now, Open
        # Folder or plain --workspace startup later) could stringify
        # differently and show up as two separate entries in the Phase 3
        # history registry instead of deduping to one.
        base_dir = AUTO_WORKSPACE_BASE_DIR.resolve()
        candidate_name = base_name
        suffix = 2
        while (base_dir / candidate_name).exists():
            candidate_name = f"{base_name}-{suffix}"
            suffix += 1
        new_workspace = base_dir / candidate_name
        new_workspace.mkdir()
    except OSError as exc:
        # e.g. ~/Documents/Agent Handoff Bridge exists as a *file*, a
        # permissions error, or a full disk -- must become the same clean
        # WorkspaceError -> 400 JSON the do_POST handler already expects,
        # not an uncaught exception that breaks the HTTP response.
        raise WorkspaceError(f"failed to create new workspace directory: {exc}") from exc

    task = resolve_task_for_first_message(text, attachments)
    # short_run(), not a direct subprocess.run() call: normalizes both a
    # missing binary (FileNotFoundError -> exit 127, previously uncaught
    # here) and a timeout (-> exit 124) into a plain (exit_code, stdout,
    # stderr) tuple, the same UTF-8-safe wrapper handoff_bridge.py's own
    # git/gh calls use -- see short_run()'s own docstring for why this
    # consolidation happened (a structure audit found this exact wrapper
    # reimplemented independently in several files).
    try:
        exit_code, _stdout, stderr = short_run(
            # "--" guarantees `task` is always treated as the positional
            # argument, even if the user's first message happens to be (or
            # start with) something that looks like one of init's own
            # flags, e.g. a literal "--no-install" or "-h" -- without it,
            # argparse would consume that as an option instead and fail
            # with "the following arguments are required: task".
            bridge_command_prefix() + ["--workspace", str(new_workspace), "init", "--", task],
            timeout=30,
        )
    except OSError as exc:
        shutil.rmtree(new_workspace, ignore_errors=True)
        raise WorkspaceError(f"failed to scaffold new workspace: {exc}") from exc
    if exit_code != 0:
        shutil.rmtree(new_workspace, ignore_errors=True)
        stderr_tail = stderr.strip()[-500:]
        raise WorkspaceError(f"failed to scaffold new workspace (exit {exit_code}): {stderr_tail}")

    # Defense in depth beyond the exit code: `init` succeeding is *supposed*
    # to mean these two files exist (handoff_bridge.init_handoff() writes
    # both unconditionally on success) -- don't let a workspace get
    # confirmed as real (state.workspace assigned, 200 returned) on the
    # strength of an exit code alone if the durable handoff surface
    # (docs/architecture.md) it's supposed to guarantee isn't actually there.
    if not (new_workspace / HANDOFF_DIR / "state.json").exists() or not (new_workspace / HANDOFF_DIR / "current.md").exists():
        shutil.rmtree(new_workspace, ignore_errors=True)
        raise WorkspaceError("workspace scaffolding did not produce the expected .handoff/ files")

    ensure_chat_gitignore(new_workspace)
    return new_workspace


# ---------------------------------------------------------------------------
# Chat history: one JSONL file per calendar month, gzip-compressed once a
# month is no longer the current one. Lives at
# <workspace>/.handoff/webui/chat/YYYY-MM.jsonl[.gz].
# ---------------------------------------------------------------------------


def chat_dir(workspace: Path) -> Path:
    return workspace / CHAT_DIR_RELATIVE


def chat_lock_path(workspace: Path) -> Path:
    return chat_dir(workspace) / ".write.lock"


def ensure_chat_gitignore(workspace: Path) -> None:
    """Make sure chat history is invisible to git no matter what.

    `handoff_bridge.py install` only writes the top-level
    `.handoff/.gitignore` template on a *fresh* install -- an
    already-installed workspace never gets it refreshed
    (`install_standard_files()` skips existing files unless `--force`), and
    a workspace that never ran `install` at all has no `.handoff/.gitignore`
    whatsoever. So this doesn't rely on that file being present or current:
    a single `*` here hides `.handoff/webui/` (including this file itself --
    `*` matches dotfiles in gitignore syntax) independent of it. Called
    proactively on startup and on every folder switch, not just lazily on
    first message, so the workspace is protected before any chat data
    exists in it.
    """
    gitignore_path = chat_dir(workspace).parent / ".gitignore"
    if gitignore_path.exists():
        return
    gitignore_path.parent.mkdir(parents=True, exist_ok=True)
    gitignore_path.write_text("*\n", encoding="utf-8")


CHAT_ROLES = ("user", "system", "agent")
# POST /api/chat is a direct client write; "agent" is reserved for messages
# POST /api/run appends itself right after a real provider call, so it's
# excluded here even though append_chat_message() (the shared writer) allows it.
CLIENT_WRITABLE_CHAT_ROLES = ("user", "system")


def append_chat_message(
    workspace: Path,
    role: str,
    text: str,
    attachments: list[dict],
    now: datetime,
    provider: str | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> dict:
    if role not in CHAT_ROLES:
        raise WorkspaceError(f"invalid role: {role}")
    message = {
        "id": uuid.uuid4().hex,
        "ts": now.isoformat(),
        "role": role,
        "text": text,
        "attachments": attachments or [],
    }
    # Only "agent" messages carry provider/status/reason -- keep the other
    # two roles' JSON shape exactly as it was (no null-field noise).
    if role == "agent":
        message["provider"] = provider
        message["status"] = status
        message["reason"] = reason
    target_dir = chat_dir(workspace)
    with WriteLock(chat_lock_path(workspace)):
        target_dir.mkdir(parents=True, exist_ok=True)
        ensure_chat_gitignore(workspace)
        path = target_dir / f"{month_key(now)}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")
    return message


def read_month_messages(workspace: Path, month: str) -> list[dict]:
    target_dir = chat_dir(workspace)
    plain = target_dir / f"{month}.jsonl"
    archived = target_dir / f"{month}.jsonl.gz"
    try:
        if plain.exists():
            text = plain.read_text(encoding="utf-8")
        elif archived.exists():
            with gzip.open(archived, "rt", encoding="utf-8") as handle:
                text = handle.read()
        else:
            return []
    except FileNotFoundError:
        # TOCTOU race with archive_old_months(), which compresses and then
        # unlink()s the plain file under WriteLock while this function reads
        # it lock-free -- a file that disappeared between exists() and
        # read_text()/gzip.open() is benign (archival deletion, not a real
        # error) and should be treated the same as "doesn't exist yet".
        return []
    messages = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written last line shouldn't sink the whole read
    return messages


def list_available_months(workspace: Path) -> list[str]:
    target_dir = chat_dir(workspace)
    if not target_dir.exists():
        return []
    months = set()
    for entry in target_dir.iterdir():
        if entry.name.endswith(".jsonl"):
            months.add(entry.name[: -len(".jsonl")])
        elif entry.name.endswith(".jsonl.gz"):
            months.add(entry.name[: -len(".jsonl.gz")])
    return sorted(months)


def archive_old_months(workspace: Path, now: datetime) -> list[str]:
    """Gzip-compress every past month's plain .jsonl file, freeing it up.

    The current month is left uncompressed and appendable. Safe to call
    often (e.g. on startup and on every workspace switch) -- a month with
    no plain file left is simply a no-op. Uses the same per-workspace
    WriteLock as append_chat_message() so a startup/folder-switch archive
    pass can never read-compress-delete a month file while a message is
    mid-append to it.
    """
    target_dir = chat_dir(workspace)
    if not target_dir.exists():
        return []
    current = month_key(now)
    archived: list[str] = []
    with WriteLock(chat_lock_path(workspace)):
        for path in sorted(target_dir.glob("*.jsonl")):
            month = path.name[: -len(".jsonl")]
            if month == current:
                continue
            gz_path = path.parent / f"{path.name}.gz"
            with path.open("rb") as source, gzip.open(gz_path, "wb") as dest:
                dest.writelines(source)
            path.unlink()
            archived.append(month)
    return archived


# ---------------------------------------------------------------------------
# Phase 3 (docs/design-system/roadmap.md, SCR-03): multi-project history
# drawer. Design decisions (DEC-08~12) recorded in
# docs/design-system/flutter-mapping.html#s1c -- resolved via a pre-
# implementation interview, not invented here.
# ---------------------------------------------------------------------------

# DEC-09: reuses the location Phase 2 already established as "the app owns
# this", rather than an OS-specific app-data path (~/Library/Application
# Support, %APPDATA%, ~/.config).
REGISTRY_MAX_ENTRIES = 50  # DEC-09
HISTORY_TURNS_PER_WORKSPACE = 5  # DEC-11

# HTTP-thread contention in one process, not separate CLI processes --
# same reasoning as _WORKSPACE_CREATE_LOCK, not handoff_bridge.WriteLock.
_REGISTRY_LOCK = threading.Lock()


def registry_path() -> Path:
    # A function, not a module-level constant bound once at import time --
    # tests patch AUTO_WORKSPACE_BASE_DIR to a tempdir (never touch the
    # real ~/Documents/), and a constant computed at import wouldn't see
    # that patch since it'd already be bound to the real path by then.
    return AUTO_WORKSPACE_BASE_DIR / "registry.json"


def read_registry() -> list[dict]:
    """Entries ordered most-recently-opened first. Never raises -- a
    missing, corrupt, or unreadable (permissions) registry file just
    means an empty "recently opened" list, same posture as
    read_state_history()."""
    path = registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("path"), str)]


def touch_registry(workspace: Path, now: datetime) -> None:
    """DEC-10: called at every point AppState.workspace gets set (Open
    Folder, auto-create, and plain CLI startup with an existing
    workspace) so the drawer reflects projects opened from other
    terminals too, not just explicit in-app actions.

    Moves `workspace` to the front (most-recently-opened), dedupes by
    path, and evicts the oldest entries past REGISTRY_MAX_ENTRIES
    (DEC-09). Entries for folders that no longer exist are pruned lazily
    at read time (build_history_drawer()), not here.

    Best-effort: this is a Phase 3 convenience index, not durable state
    (docs/architecture.md's "State Boundaries" -- .handoff/state.json and
    .handoff/current.md are the durable handoff surface, not this). A
    write failure here (~/Documents/Agent Handoff Bridge unreadable,
    exists as a file, a full disk, a permissions error) must never break
    the workspace switch/auto-create/startup it's attached to -- all
    three call sites already changed real state (AppState.workspace,
    or in main()'s case the whole server is about to come up) by the time
    this runs, so raising here would desync what the server just did from
    what the client/operator sees happen.
    """
    try:
        with _REGISTRY_LOCK:
            path_str = str(workspace)
            entries = [e for e in read_registry() if e.get("path") != path_str]
            entries.insert(0, {"path": path_str, "name": workspace.name or path_str, "last_opened": now.isoformat()})
            atomic_write_text(
                registry_path(), json.dumps(entries[:REGISTRY_MAX_ENTRIES], ensure_ascii=False, indent=2)
            )
    except OSError as exc:
        print(f"[webui] warning: failed to update recent-workspaces registry: {exc}", file=sys.stderr)


def pair_messages_into_turns(messages: list[dict]) -> list[dict]:
    """DEC-08: one drawer item = one user message + whichever agent
    message(s) followed it, not a raw 1:1 mapping of chat log lines.
    "system" messages (e.g. workspace-switch notices) don't start a turn.
    DEC-12: when auto-fallback produced more than one agent reply for the
    same turn, the *last* one's provider/status wins -- overwriting as
    each subsequent agent message is seen naturally implements that.
    """
    turns: list[dict] = []
    current: dict | None = None
    for message in messages:
        role = message.get("role")
        if role == "user":
            current = {
                "ts": message.get("ts"),
                "text": message.get("text") or "",
                "provider": None,
                "status": None,
            }
            turns.append(current)
        elif role == "agent" and current is not None:
            current["provider"] = message.get("provider")
            current["status"] = message.get("status")
    return turns


def collect_recent_turns(workspace: Path, limit: int = HISTORY_TURNS_PER_WORKSPACE) -> list[dict]:
    """Newest-first, scanning months backward only as far as needed to
    fill `limit` -- DEC-11's "그룹당 최근 5개" doesn't require reading
    every month a long-lived project has ever had.

    Pairs across the merged, chronologically-ordered messages from every
    month scanned so far in each pass -- pairing each month's file in
    isolation would silently drop or misattribute a turn whose agent
    reply landed in the *next* month's file (e.g. a message sent right
    before a UTC month boundary): the user message would show up with no
    provider/status, and the agent reply would be dropped outright, since
    pair_messages_into_turns() resets its "current turn" per call.
    """
    messages: list[dict] = []
    turns: list[dict] = []
    for month in reversed(list_available_months(workspace)):
        messages = read_month_messages(workspace, month) + messages
        turns = pair_messages_into_turns(messages)
        if len(turns) >= limit:
            break
    return list(reversed(turns))[:limit]


def build_history_drawer(current_workspace: Path | None) -> list[dict]:
    """DEC-09/11: the current workspace (if any) is pinned first
    regardless of recency, then the rest of the registry ordered
    most-recently-opened first. A registry entry whose folder no longer
    exists is silently skipped (DEC-09) -- not surfaced as an error, since
    "some project you opened once got deleted" isn't actionable here."""
    groups: list[dict] = []
    seen_paths: set[str] = set()

    def add_group(path_str: str, name: str) -> None:
        if path_str in seen_paths:
            return
        workspace = Path(path_str)
        if not workspace.is_dir():
            return
        seen_paths.add(path_str)
        groups.append(
            {
                "path": path_str,
                "name": name,
                "current": current_workspace is not None and workspace == current_workspace,
                "turns": collect_recent_turns(workspace),
            }
        )

    if current_workspace is not None:
        add_group(str(current_workspace), current_workspace.name or str(current_workspace))
    for entry in read_registry():
        path_str = entry.get("path")
        if path_str:
            add_group(path_str, entry.get("name") or Path(path_str).name)
    return groups


# ---------------------------------------------------------------------------
# Provider runs (Phase 1, docs/design-system/roadmap.md). Shells out to
# handoff_bridge.py -- the same CLI a human would type -- rather than
# importing and calling its functions in-process. Those functions resolve
# paths like .handoff/state.json relative to the *process* cwd (via
# chdir_workspace()), which is fine for a one-shot CLI invocation but not
# safe to call in-process from a ThreadingHTTPServer handler: os.chdir() is
# process-wide, so one request's chdir would race every other in-flight
# request's thread. A subprocess per run keeps each invocation's cwd
# private, exactly like handoff_desktop.py already does for the same
# reason (see run_bridge() there).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API-key mode (Phase 4, docs/design-system/roadmap.md, SCR-06/components.html
# §14, resolves CFL-12). Scope decided with the user up front and recorded as
# DEC-13 in flutter-mapping.html: chat-only for this phase -- a provider with
# no local CLI can be reached over its vendor HTTP API directly instead, but
# it only exchanges text. It does not read/write workspace files or run
# shell commands the way `codex exec`/`claude -p` do; docs/research-api-key-
# mode.md found neither vendor exposes that behind a plain API-key call
# without this project building its own tool-use loop, which is deliberately
# deferred (CFL-17) rather than attempted here.
#
# Credentials live in AUTO_WORKSPACE_BASE_DIR/credentials.json (DEC-14) --
# the same "the app owns this" location Phase 3 established for
# registry.json, not an OS keychain (would need three different code paths,
# one of them -- Linux secret-tool -- not reliably present) and not the
# third-party `keyring` package (a new dependency this project has
# consistently avoided). File mode is restricted to 0600 on write. This file
# is never inside a git-tracked workspace, so scripts/scan_secrets.py's
# git-diff-based commit scan never sees it either way.
# ---------------------------------------------------------------------------

_CREDENTIALS_LOCK = threading.Lock()

# DEC-15 (Phase 4) deliberately scoped this to codex/claude only,
# explicitly leaving "should Gemini get API-key mode too" as its own,
# separately-decided question (not silently inherited from PROVIDERS
# growing to include Gemini in Phase 5) -- see flutter-mapping.html's
# DEC-15 row. DEC-25 resolves that question: yes, via call_gemini_api()
# below. Kept as its own tuple rather than an alias for `PROVIDERS`
# (imported from handoff_bridge above) even though the two now happen to
# be equal -- a *future* provider added to PROVIDERS for CLI dispatch
# must not silently gain API-key mode without its own deliberate decision
# the same way Gemini just did. `cli_available()`-based dispatch and the
# Diagnose panel's CLI-detection badges use the full `PROVIDERS`;
# credential storage, `/api/provider-key`, and `API_KEY_MODE_DEFAULT_MODELS`
# use this one instead.
API_KEY_MODE_PROVIDERS = ("codex", "claude", "gemini")

# Deliberately empty for *both* providers, not just OpenAI/Codex: an
# earlier version hardcoded a Claude default on the reasoning that model
# IDs from this session's own environment context were "safe to default
# to" -- a real review round pointed out that's an internal, undated
# assumption, not a citable, externally-verifiable source the way
# docs/research.md/docs/research-api-key-mode.md's other claims are, and
# a wrong/deprecated default would silently break every CLI-less Claude
# user with no clear model config error to point at. Requiring an
# explicit model from both providers (via POST /api/provider-key) is the
# more honest, defensible choice than guessing one that can't be sourced.
API_KEY_MODE_DEFAULT_MODELS: dict[str, str] = {}

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# {model} is the bare model ID (e.g. "gemini-2.5-flash"), no "models/"
# prefix -- confirmed against https://ai.google.dev/api/generate-content.
# Auth via the `x-goog-api-key` header (also confirmed there), not the
# `?key=` query-string form the same docs mention as an alternative --
# a header keeps this consistent with the other two providers (both
# header-based) and never puts the secret in a URL that could end up in
# a proxy/log line.
GEMINI_GENERATE_CONTENT_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_KEY_MODE_TIMEOUT_SECONDS = 120
API_KEY_MODE_MAX_HISTORY_MESSAGES = 20
API_KEY_MODE_MAX_TOKENS = 4096
# Small on purpose: POST /api/provider-key's validation call only needs a
# single short word back to prove the key/model combination actually
# works -- it is never a real chat turn.
API_KEY_VALIDATION_MAX_TOKENS = 16


def credentials_path() -> Path:
    # A function, not a module-level constant -- same reasoning as
    # registry_path(): tests patch AUTO_WORKSPACE_BASE_DIR to a tempdir, and
    # a constant computed at import time wouldn't see that patch.
    return AUTO_WORKSPACE_BASE_DIR / "credentials.json"


def read_credentials() -> dict:
    """Provider name -> {"key": str, "model": str|None}. Never raises -- a
    missing, corrupt, or unreadable (permissions) credentials file just
    means no providers are configured yet, same posture as read_registry()."""
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for provider, entry in data.items():
        if provider not in API_KEY_MODE_PROVIDERS or not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        model = entry.get("model")
        result[provider] = {"key": key, "model": model if isinstance(model, str) and model else None}
    return result


def save_credential(provider: str, key: str, model: str | None) -> None:
    """Store (or, with an empty `key`, remove) one provider's API key.

    Locked and read-modify-write, like touch_registry() -- this file can be
    written from multiple request threads (two browser tabs opening the
    connection panel at once)."""
    with _CREDENTIALS_LOCK:
        path = credentials_path()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        if key:
            data[provider] = {"key": key, "model": model or None}
        else:
            data.pop(provider, None)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on platforms/filesystems that don't support chmod


def cli_available(provider: str) -> bool:
    return shutil.which(provider) is not None


def build_api_message_history(workspace: Path, prompt: str, now: datetime) -> list[dict]:
    """Anthropic's Messages API and OpenAI's Responses API are both
    stateless per call (docs/research-api-key-mode.md) -- there is no
    `codex exec resume`/`claude --resume` equivalent, so conversation
    continuity has to come from resending prior turns ourselves.

    Scans months backward (newest first, merged into chronological order),
    same pattern as collect_recent_turns() -- a bare
    read_month_messages(workspace, month_key(now)) would silently drop all
    prior context on the first message(s) of a new UTC month, the same
    class of bug Phase 3 already had to fix once for that other function.
    Stops once API_KEY_MODE_MAX_HISTORY_MESSAGES raw messages are
    collected, to bound how many months a long-lived project ever needs
    to read.

    Drops the bare current-turn log entry in favor of `prompt`
    (build_run_prompt()'s text+attachments string, which the bare log
    entry doesn't carry -- POST /api/chat always runs before POST
    /api/run, so that entry already exists by the time this is called).

    Anthropic's Messages API requires strict user/assistant alternation.
    A single CLI turn can produce two consecutive "agent" chat-log entries
    when --auto-fallback chains providers (_run_provider_via_bridge_locked()'s
    own docstring documents this, and POST /api/run appends one "agent"
    message per resulting record) -- if that workspace later loses its
    CLI(s) and falls into API-key mode within the same history window,
    replaying those as two separate assistant turns would violate
    alternation and fail with a 400. Consecutive same-role entries
    (including the final `prompt` against whatever role ends up last) are
    merged (text joined) instead of kept separate, so replay can never
    violate alternation regardless of how the log was produced.
    """
    turns: list[dict] = []
    for month in reversed(list_available_months(workspace)):
        turns = [m for m in read_month_messages(workspace, month) if m.get("role") in ("user", "agent")] + turns
        if len(turns) >= API_KEY_MODE_MAX_HISTORY_MESSAGES:
            break
    if turns and turns[-1].get("role") == "user":
        turns = turns[:-1]
    turns = turns[-API_KEY_MODE_MAX_HISTORY_MESSAGES:]

    messages: list[dict] = []

    def _append(role: str, content: str) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{content}".strip()
        else:
            messages.append({"role": role, "content": content})

    for turn in turns:
        _append("user" if turn["role"] == "user" else "assistant", turn.get("text") or "")
    _append("user", prompt)
    return messages


# docs/research-api-key-mode.md notes that the official SDKs auto-retry
# transient failures (connection errors, rate limits, 5xx) with backoff,
# honoring `retry-after` -- a hand-rolled urllib client doesn't get that
# for free, so a small bounded retry is applied here rather than surfacing
# every transient blip as a user-visible chat failure the CLI path
# wouldn't have.
API_KEY_MODE_MAX_RETRIES = 2  # total attempts = 1 + this
API_KEY_MODE_RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 529}
API_KEY_MODE_RETRY_BASE_DELAY_SECONDS = 1.0


def _sleep(seconds: float) -> None:
    """Seam so tests can avoid real delays -- same reasoning as
    _http_post_json()'s seam over urllib."""
    time.sleep(seconds)


def _retry_delay_seconds(exc: BaseException, attempt: int) -> float:
    retry_after = getattr(exc, "headers", None) and exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass  # not a plain seconds value (e.g. an HTTP-date) -- fall through to the default backoff
    return API_KEY_MODE_RETRY_BASE_DELAY_SECONDS * attempt


def _http_post_json(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, dict]:
    """Thin seam around urllib so tests can substitute a fake transport
    instead of making a real network call -- the same reason
    _run_provider_via_bridge_locked() shells out to a fake `codex`/`claude`
    script in tests rather than calling a real provider.

    Retries a rate-limited/server-error/network-transient failure up to
    API_KEY_MODE_MAX_RETRIES times before giving up -- everything else
    (auth errors, bad request, header rejection, a malformed response
    body) returns immediately since retrying wouldn't change the outcome.
    """
    encoded = json.dumps(body).encode("utf-8")
    attempt = 0
    while True:
        request = urllib.request.Request(url, data=encoded, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https:// constants above)
                raw = response.read().decode("utf-8")
                try:
                    return response.status, json.loads(raw)
                except json.JSONDecodeError:
                    # A 200 with an unparseable body (e.g. a proxy sitting
                    # in front of the real API) -- status 0 (never a real
                    # HTTP status) so callers' `if status != 200` branch
                    # still treats this as a failure instead of silently
                    # reading an empty reply out of the malformed body.
                    return 0, {"error": {"type": "api_error", "message": f"non-JSON response body: {raw[:500]}"}}
        except urllib.error.HTTPError as exc:
            if exc.code in API_KEY_MODE_RETRY_STATUS_CODES and attempt < API_KEY_MODE_MAX_RETRIES:
                attempt += 1
                _sleep(_retry_delay_seconds(exc, attempt))
                continue
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"error": {"type": "api_error", "message": payload or exc.reason}}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # ValueError (below) is deliberately NOT included here even
            # though json.JSONDecodeError (handled above, inside the try
            # block) is technically a ValueError subclass -- ordering
            # matters: that inner try/except already claims it before
            # execution would ever reach this or the next clause.
            if attempt < API_KEY_MODE_MAX_RETRIES:
                attempt += 1
                _sleep(_retry_delay_seconds(exc, attempt))
                continue
            raise
        except ValueError:
            # http.client raises a bare ValueError (not HTTPError/URLError)
            # for a header value containing forbidden characters -- e.g. a
            # saved API key with an embedded CR/LF makes it straight into
            # the `x-api-key`/`Authorization` header unescaped. httplib's
            # own exception text embeds the offending header VALUE
            # verbatim (the key itself, here), so unlike every other
            # branch in this function -- which forwards the vendor's real
            # response/exception text -- that text must never be returned
            # as-is. Retrying wouldn't help (the key is still malformed),
            # so this returns immediately rather than going through the
            # retry loop above.
            return 0, {
                "error": {
                    "type": "invalid_request",
                    "message": "request headers were rejected -- the API key or model contains characters not allowed in an HTTP header value",
                }
            }


# --- Agentic tool loop (CFL-17/DEC-21) ------------------------------------
#
# docs/research-api-key-mode.md's "Open Scope Question" laid out three
# sizes of API-key mode; Phase 4 shipped the smallest (chat-only, DEC-13),
# explicitly deferring the largest -- a bridge-built tool loop giving
# file-edit/shell-exec parity with CLI mode -- as CFL-17, since a
# bridge-controlled shell-exec tool is "a new sandboxing/security surface
# this project doesn't have today." The design interview that resolved
# CFL-17 chose to build file tools and the shell tool together in one
# pass, and to extend DEC-02 (confirm only the first send per session)
# to cover every tool call this loop makes rather than adding a stronger
# per-call confirmation -- i.e. once a session's first message is
# confirmed, this loop can read/write/edit any file under the workspace
# and run any shell command in it, with no further per-call gate. This
# mirrors the trust level already extended to CLI mode: `codex`/`claude`
# subprocesses invoked elsewhere in this file already have full local
# shell access when they actually run, so a shell tool with no allowlist
# is not a new tier of trust, just a bridge-built equivalent of what CLI
# mode already does. `run_shell` sets `cwd=workspace` (where it starts,
# matching CLI mode's own subprocess cwd) but is not a sandbox -- a
# review round flagged an earlier draft of this comment for implying
# otherwise. An absolute path or `..` still reaches anywhere the user
# account itself can, the same as it would for a real terminal, or for
# `codex`/`claude` run directly outside this bridge.

# Bounds how many tool calls a single turn can make before this loop gives
# up and returns whatever text exists -- without this, a confused model
# issuing tool calls that never satisfy it could loop indefinitely,
# burning API cost with no user-visible progress. 15 is generous enough
# for a real multi-step edit (a handful of reads, a couple of edits, one
# or two shell commands to verify) while still being a hard stop, not a
# tuned/benchmarked number.
MAX_TOOL_ITERATIONS = 15

# subprocess.run(..., shell=True) timeout for the run_shell tool -- reuses
# API_KEY_MODE_TIMEOUT_SECONDS' value rather than inventing a second
# magic number, since both bound "how long this project will wait on one
# step of an API-key-mode turn before giving up."
TOOL_EXEC_TIMEOUT_SECONDS = API_KEY_MODE_TIMEOUT_SECONDS

# A tool result this long would dominate the next call's context (and
# cost) for little benefit -- long build/test output is usually only
# interesting at the head or tail, and a truncation note (never a silent
# cut) tells the model and the persisted chat transcript alike that this
# happened.
TOOL_OUTPUT_MAX_CHARS = 4000

# One list of {name, description, params, required} feeds both vendors'
# schema builders below -- Anthropic's input_schema and OpenAI's
# parameters shapes differ, but the underlying tool set must never drift
# between them, so it's defined once here instead of twice.
_TOOL_SPECS: list[dict] = [
    {
        "name": "read_file",
        "description": "Read a text file's contents from the workspace. path is relative to the workspace root.",
        "params": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
        "required": ["path"],
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing one in the workspace with the given content.",
        "params": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "Full contents to write."},
        },
        "required": ["path", "content"],
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_string with new_string in an existing file. "
            "old_string must match exactly once in the file -- include enough surrounding "
            "context to make it unique, or the edit is rejected."
        ),
        "params": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "old_string": {"type": "string", "description": "Exact text to replace; must appear exactly once in the file."},
            "new_string": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_string", "new_string"],
    },
    {
        "name": "run_shell",
        "description": "Run a shell command in the workspace directory. Returns its exit code, stdout, and stderr.",
        "params": {"command": {"type": "string", "description": "Shell command to execute."}},
        "required": ["command"],
    },
]


def anthropic_tool_definitions() -> list[dict]:
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "input_schema": {"type": "object", "properties": spec["params"], "required": spec["required"]},
        }
        for spec in _TOOL_SPECS
    ]


def openai_tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "properties": spec["params"],
                "required": spec["required"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        for spec in _TOOL_SPECS
    ]


def gemini_tool_definitions() -> list[dict]:
    # One Tool object holding every functionDeclaration, not one Tool per
    # function -- confirmed against https://ai.google.dev/api/generate-content
    # (a request's `tools` array example groups all declarations under a
    # single `functionDeclarations` list). No `additionalProperties`/
    # `strict` here -- those are OpenAI-specific "strict mode" fields
    # Gemini's function-calling schema doesn't define.
    return [
        {
            "functionDeclarations": [
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": {"type": "object", "properties": spec["params"], "required": spec["required"]},
                }
                for spec in _TOOL_SPECS
            ]
        }
    ]


def _tool_read_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    try:
        result = read_file_preview(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    content = result["content"]
    # MAX_FILE_BYTES (read_file_preview()'s own cap, ~256KB) bounds how
    # much this project will ever read off disk for a preview -- a
    # review round pointed out that's not the same as bounding how much
    # ends up in a single tool_result feeding the *next* API call, which
    # is what TOOL_OUTPUT_MAX_CHARS is actually for (same reasoning
    # _tool_run_shell() already applies to its own output). Without this,
    # a model reading a handful of large files in one turn could still
    # blow past the context/cost this constant exists to bound.
    truncated = result["truncated"] or len(content) > TOOL_OUTPUT_MAX_CHARS
    if len(content) > TOOL_OUTPUT_MAX_CHARS:
        content = content[:TOOL_OUTPUT_MAX_CHARS]
    note = "\n(truncated)" if truncated else ""
    return f"{content}{note}"


def _tool_write_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    content = tool_input.get("content")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    if not isinstance(content, str):
        return "error: 'content' is required"
    try:
        target = safe_join(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: failed to write {path}: {exc}"
    return f"wrote {len(content)} characters to {path}"


def _tool_edit_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    if not isinstance(old_string, str) or not old_string:
        return "error: 'old_string' is required and must be non-empty"
    if not isinstance(new_string, str):
        return "error: 'new_string' is required"
    try:
        target = safe_join(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    if not target.exists() or not target.is_file():
        return f"error: not a file: {path}"
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"error: failed to read {path}: {exc}"
    count = current.count(old_string)
    if count == 0:
        return f"error: old_string not found in {path}"
    if count > 1:
        return f"error: old_string matches {count} locations in {path} -- must match exactly once, add more surrounding context"
    try:
        target.write_text(current.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return f"error: failed to write {path}: {exc}"
    return f"edited {path}"


def _tool_run_shell(workspace: Path, tool_input: dict) -> str:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return "error: 'command' is required"
    # A review round noted this timeout only guarantees killing the
    # immediate subprocess (Python's own TimeoutExpired handling), not a
    # whole process tree -- a command that backgrounds work or forks
    # descendants could leave some of them running past
    # TOOL_EXEC_TIMEOUT_SECONDS. Cross-platform process-group cleanup
    # (os.killpg on POSIX, a job object on Windows) is a real, larger
    # change than this fix round's scope; documented here as a known,
    # accepted gap rather than silently left unstated -- same posture
    # DEC-21 already takes toward run_shell having no command allowlist.
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            # Without an explicit encoding, subprocess falls back to
            # locale.getpreferredencoding() to decode stdout/stderr -- not
            # UTF-8 on a non-UTF-8-locale Windows machine -- and a model's
            # shell command (or the workspace files it reads) can easily
            # produce non-ASCII output. See run_provider()'s matching fix
            # in handoff_bridge.py for the confirmed crash this class of
            # bug produces elsewhere in this project.
            encoding="utf-8",
            errors="replace",
            timeout=TOOL_EXEC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {TOOL_EXEC_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"error: failed to run command: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    truncated = len(output) > TOOL_OUTPUT_MAX_CHARS
    if truncated:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n(output truncated)"
    return f"exit code: {result.returncode}\n{output}"


_TOOL_EXECUTORS = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "run_shell": _tool_run_shell,
}


def execute_tool_call(workspace: Path, name: str, tool_input: dict) -> str:
    """Never raises -- a bug in one tool executor, or a malformed
    `tool_input` from the model, must degrade to an error string the
    model can see and react to, not crash the turn loop mid-conversation."""
    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        return f"error: unknown tool '{name}'"
    try:
        return executor(workspace, tool_input if isinstance(tool_input, dict) else {})
    except Exception as exc:  # noqa: BLE001 -- see docstring
        return f"error: tool '{name}' raised an unexpected exception: {exc}"


def _escape_fence(text: str) -> str:
    # webui/app.js's DEC-03 renderer matches exactly ``` as a fence
    # delimiter (renderTextWithCodeBlocks()'s regex has no longer-fence
    # escape hatch to fall back to) -- a review round pointed out that
    # if tool output or arguments ever contain their own ``` run (a file
    # whose content includes a fenced block, a shell command that prints
    # one), this project's only audit trail for tool activity could be
    # cut off mid-way, hiding what actually ran. A zero-width space
    # between each backtick in any 3+ run breaks the literal-```
    # match without being visible to a human reading the transcript.
    return re.sub(r"`{3,}", lambda m: "\u200b".join(m.group(0)), text)


def _truncate_for_transcript(text: str) -> str:
    # TOOL_OUTPUT_MAX_CHARS already bounds what execute_tool_call()'s
    # *results* feed into the next API call (read_file/run_shell), but a
    # review round found the *arguments* side had no equivalent cap --
    # write_file's `content` or edit_file's `new_string` can be
    # arbitrarily long, and those land in the transcript verbatim via
    # json.dumps(tool_input). A large-but-completely-normal file write
    # would otherwise inflate every subsequent API call's context (and
    # this project's persisted chat log) for no benefit -- the file
    # itself is still on disk in full; the transcript only needs to show
    # that the write happened, not replay the entire payload.
    if len(text) > TOOL_OUTPUT_MAX_CHARS:
        return text[:TOOL_OUTPUT_MAX_CHARS] + "... (truncated for transcript)"
    return text


def _tool_call_transcript_block(name: str, raw_args: str, result_text: str) -> str:
    # Reuses DEC-03 (fenced ```code``` blocks are the only markdown this
    # project's chat bubbles render specially) instead of inventing a new
    # message role/schema just to show tool activity -- this folds
    # straight into final_text and renders with zero frontend changes.
    safe_args = _escape_fence(_truncate_for_transcript(raw_args))
    safe_result = _escape_fence(_truncate_for_transcript(result_text))
    return f"```\n$ {name}({safe_args})\n{safe_result}\n```"


def _error_with_transcript(transcript_parts: list[str], message: str) -> dict:
    # A review round found that a mid-loop API failure (network error,
    # non-200) used to discard `transcript_parts` outright -- if a tool
    # with real side effects (write_file, edit_file, run_shell) already
    # ran on an earlier iteration before the *next* API call failed, the
    # record of what actually executed vanished along with it. DEC-21's
    # whole rationale for skipping a per-tool-call confirmation is that
    # tool activity stays visible in the persisted chat log after the
    # fact -- that guarantee is void if a failure can silently erase it,
    # so any already-accumulated transcript is prepended to the error
    # message here rather than dropped.
    if not transcript_parts:
        return {"ok": False, "message": message}
    transcript_so_far = "\n\n".join(transcript_parts)
    return {"ok": False, "message": f"{transcript_so_far}\n\n{message}"}


def call_anthropic_messages_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Returns {"ok": True, "text": str} or {"ok": False, "message": str} --
    `message` is built only from the response body/exception text, never
    from `api_key`, so a saved key can never leak into a chat log entry via
    an error message. If one or more tools already executed before a
    later call in the same turn failed, `message` is prefixed with the
    transcript of what ran (_error_with_transcript()) -- a mid-turn API
    failure must never silently erase the record of a write_file/
    edit_file/run_shell that already had a real effect.

    Runs the tool-use turn loop (CFL-17/DEC-21; request/response shapes
    -- tools[].input_schema, tool_choice.disable_parallel_tool_use,
    tool_use/tool_result content blocks -- confirmed against
    https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
    before implementing, not assumed): sends `messages` plus this
    project's tool schemas, executes any `tool_use` block(s) Claude
    returns via execute_tool_call(), feeds the results back, and repeats
    until Claude stops requesting tools or MAX_TOOL_ITERATIONS actual
    tool calls have been executed (not just HTTP round trips -- a
    self-review round pointed out that bounding round trips alone
    doesn't bound cost/runaway-loop risk if a single response can carry
    more than one call). A response with no tool_use block returns on
    the first iteration -- the exact single-call behavior this function
    had before CFL-17, so a plain chat turn (no tool calls) is
    unaffected.
    `tool_choice.disable_parallel_tool_use` asks Claude for one tool
    call per turn -- simpler to log and reason about than unwinding
    several simultaneous tool calls, at the cost of an extra round trip
    for a task that could otherwise batch calls -- but this is a hint,
    not a guarantee the API enforces, so every tool_use block in a
    response is still executed (each needs a matching tool_result or
    the next call would 400 on mismatched IDs), matching the defensive
    posture call_openai_responses_api() already needs for the same
    reason on its side.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    working_messages = list(messages)
    transcript_parts: list[str] = []
    tools = anthropic_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {
            "model": model,
            "max_tokens": API_KEY_MODE_MAX_TOKENS,
            "messages": working_messages,
            "tools": tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        try:
            status, data = _http_post_json(ANTHROPIC_MESSAGES_URL, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # _http_post_json() already retried transient failures
            # (API_KEY_MODE_MAX_RETRIES times) before re-raising this.
            return _error_with_transcript(transcript_parts, f"network error calling Anthropic Messages API: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_type = (error or {}).get("type", "api_error")
            error_message = (error or {}).get("message", json.dumps(data, ensure_ascii=False))
            return _error_with_transcript(transcript_parts, f"Anthropic API error ({status} {error_type}): {error_message}")
        content = data.get("content", []) if isinstance(data, dict) else []
        text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        if text:
            transcript_parts.append(text)
        tool_use_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if not tool_use_blocks:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        working_messages.append({"role": "assistant", "content": content})
        tool_results: list[dict] = []
        for tool_use in tool_use_blocks:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            tool_name = tool_use.get("name", "")
            tool_input = tool_use.get("input") or {}
            result_text = execute_tool_call(workspace, tool_name, tool_input)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(tool_name, json.dumps(tool_input, ensure_ascii=False), result_text))
            tool_results.append({"type": "tool_result", "tool_use_id": tool_use.get("id"), "content": result_text})
        working_messages.append({"role": "user", "content": tool_results})


def call_openai_responses_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Same contract and same tool-use turn loop as
    call_anthropic_messages_api() -- see its docstring for the shared
    reasoning (MAX_TOOL_ITERATIONS bounds actual tool *executions*, not
    HTTP round trips -- a response's `output` array can carry more than
    one call, so counting round trips alone wouldn't actually bound
    cost/runaway-loop risk). Request/response shapes -- tools[].type=
    "function"/parameters/strict, function_call output items,
    function_call_output input items -- confirmed against
    https://developers.openai.com/api/docs/guides/function-calling
    before implementing, not assumed. Responses API input items use
    OpenAI's {role, content} shape too, so `messages` (already built for
    the Anthropic call) is reused as-is for the initial `input`.

    No Responses API equivalent of `disable_parallel_tool_use` turned up
    in that research, so a response's `output` array is handled as
    documented: it may contain more than one `function_call` item, and
    each is executed and given a matching `function_call_output` before
    the next call -- OpenAI's contract otherwise has no way to answer
    only some of a batch.
    """
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    working_input = list(messages)
    transcript_parts: list[str] = []
    tools = openai_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {"model": model, "input": working_input, "tools": tools}
        try:
            status, data = _http_post_json(OPENAI_RESPONSES_URL, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return _error_with_transcript(transcript_parts, f"network error calling OpenAI Responses API: {exc}")
        except json.JSONDecodeError as exc:
            return _error_with_transcript(transcript_parts, f"OpenAI API returned a non-JSON response: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_type = (error or {}).get("type", "api_error") if isinstance(error, dict) else "api_error"
            error_message = (
                (error or {}).get("message", json.dumps(data, ensure_ascii=False))
                if isinstance(error, dict)
                else json.dumps(data, ensure_ascii=False)
            )
            return _error_with_transcript(transcript_parts, f"OpenAI API error ({status} {error_type}): {error_message}")
        output = data.get("output", []) if isinstance(data, dict) else []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text = block.get("text", "")
                    if text:
                        transcript_parts.append(text)
        function_calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
        if not function_calls:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        for call in function_calls:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            working_input.append(call)
            name = call.get("name", "")
            raw_args = call.get("arguments", "{}")
            try:
                tool_input = json.loads(raw_args) if isinstance(raw_args, str) else {}
            except json.JSONDecodeError:
                tool_input = {}
            result_text = execute_tool_call(workspace, name, tool_input)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(name, raw_args if isinstance(raw_args, str) else "{}", result_text))
            working_input.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": result_text})


def _gemini_contents_from_messages(messages: list[dict]) -> list[dict]:
    # Gemini's Content shape ({"role", "parts": [{"text": ...}]}) is not
    # the {"role", "content": "..."} shape build_api_message_history()
    # builds for Anthropic/OpenAI -- translated here rather than changing
    # that shared function, since Anthropic and OpenAI's Responses API
    # both genuinely do accept {"role", "content"} directly. Gemini has
    # no "assistant" role; "model" is the equivalent (confirmed against
    # https://ai.google.dev/api/generate-content).
    return [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages]


def call_gemini_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Same {"ok": True, "text": str} / {"ok": False, "message": str}
    contract and the same tool-use turn loop shape as
    call_anthropic_messages_api()/call_openai_responses_api() -- see
    that function's docstring for the shared MAX_TOOL_ITERATIONS/
    defensive-every-call-block reasoning, which applies here unchanged.

    Request/response shapes (contents[].role/parts, tools[].
    functionDeclarations, a model turn's functionCall part, sending a
    result back as a functionResponse part with role "user", auth via
    the x-goog-api-key header) confirmed against
    https://ai.google.dev/api/generate-content and
    https://ai.google.dev/gemini-api/docs/generate-content/function-calling
    before implementing, not assumed -- DEC-25
    (docs/design-system/flutter-mapping.html#s1c), resolving DEC-15's
    deliberately-left-open "should Gemini get API-key mode too" question.

    Unlike a functionCall's `args` (already a plain object per Gemini's
    schema), a functionResponse's `response` field must itself be a JSON
    *object*, not a bare string -- execute_tool_call() returns plain text
    (the shared, provider-agnostic executor contract all three providers
    use), so it is wrapped as {"result": <text>} here rather than
    changing that shared contract for one provider's stricter schema.
    """
    url = GEMINI_GENERATE_CONTENT_URL_TEMPLATE.format(model=model)
    headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    working_contents = _gemini_contents_from_messages(messages)
    transcript_parts: list[str] = []
    tools = gemini_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {"contents": working_contents, "tools": tools}
        try:
            status, data = _http_post_json(url, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return _error_with_transcript(transcript_parts, f"network error calling Gemini API: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_status = (error or {}).get("status", "api_error") if isinstance(error, dict) else "api_error"
            error_message = (
                (error or {}).get("message", json.dumps(data, ensure_ascii=False))
                if isinstance(error, dict)
                else json.dumps(data, ensure_ascii=False)
            )
            return _error_with_transcript(transcript_parts, f"Gemini API error ({status} {error_status}): {error_message}")
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not candidates:
            # No candidate at all -- most commonly a blocked prompt
            # (promptFeedback.blockReason), never something to silently
            # treat as an empty-but-successful reply.
            block_reason = (data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
            if block_reason:
                return _error_with_transcript(transcript_parts, f"Gemini blocked the prompt: {block_reason}")
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
        if text:
            transcript_parts.append(text)
        function_calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]
        if not function_calls:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        working_contents.append({"role": "model", "parts": parts})
        response_parts: list[dict] = []
        for call in function_calls:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            name = call.get("name", "")
            args = call.get("args") or {}
            result_text = execute_tool_call(workspace, name, args)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(name, json.dumps(args, ensure_ascii=False), result_text))
            function_response = {"name": name, "response": {"result": result_text}}
            if "id" in call:
                # Only some models/responses include this (confirmed:
                # "now always returned... for Gemini 3 models," implying
                # earlier models may omit it) -- echoed back only when
                # present, never fabricated.
                function_response["id"] = call["id"]
            response_parts.append({"functionResponse": function_response})
        working_contents.append({"role": "user", "parts": response_parts})


def validate_provider_api_key(provider: str, api_key: str, model: str) -> dict:
    """Makes one real, minimal, tool-free call to the provider's own API
    with `api_key`/`model` to confirm the key actually works -- POST
    /api/provider-key calls this before save_credential() ever writes a
    non-empty key to disk, so a saved-but-wrong key is never trusted on
    the strength of its shape alone. Same {"ok": True, "text": str} /
    {"ok": False, "message": str} contract as
    call_anthropic_messages_api()/call_openai_responses_api() (`message`
    never contains `api_key`, same invariant), but deliberately skips
    their tool-use turn loop entirely: this has no workspace to act on
    and no reason to grant tool access just to check a key, so it is a
    single HTTP call with no `tools` in the request body at all.
    """
    ping_message = [{"role": "user", "content": "Reply with only the single word: ok"}]
    if provider == "claude":
        url = ANTHROPIC_MESSAGES_URL
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        body = {"model": model, "max_tokens": API_KEY_VALIDATION_MAX_TOKENS, "messages": ping_message}
    elif provider == "gemini":
        url = GEMINI_GENERATE_CONTENT_URL_TEMPLATE.format(model=model)
        headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        body = {
            "contents": _gemini_contents_from_messages(ping_message),
            "generationConfig": {"maxOutputTokens": API_KEY_VALIDATION_MAX_TOKENS},
        }
    else:
        url = OPENAI_RESPONSES_URL
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        body = {"model": model, "input": ping_message, "max_output_tokens": API_KEY_VALIDATION_MAX_TOKENS}
    try:
        status, data = _http_post_json(url, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "message": f"network error validating {provider} API key: {exc}"}
    if status != 200:
        error = data.get("error") if isinstance(data, dict) else None
        # Gemini's error shape uses `status` (a string enum like
        # "INVALID_ARGUMENT"/"PERMISSION_DENIED"), not `type` --
        # confirmed against https://ai.google.dev/api/generate-content's
        # error examples.
        error_type = (
            (error or {}).get("status" if provider == "gemini" else "type", "api_error")
            if isinstance(error, dict)
            else "api_error"
        )
        error_message = (
            (error or {}).get("message", json.dumps(data, ensure_ascii=False))
            if isinstance(error, dict)
            else json.dumps(data, ensure_ascii=False)
        )
        return {"ok": False, "message": f"{provider} API key validation failed ({status} {error_type}): {error_message}"}
    text = ""
    if provider == "claude":
        content = data.get("content", []) if isinstance(data, dict) else []
        text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    elif provider == "gemini":
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if candidates:
            content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
            for part in content.get("parts", []) if isinstance(content, dict) else []:
                if isinstance(part, dict) and "text" in part:
                    text += part["text"]
    else:
        for item in data.get("output", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text += block.get("text", "")
    return {"ok": True, "text": text.strip() or "(empty response)"}


def run_provider_via_api_key(
    workspace: Path,
    provider: str,
    prompt: str,
    credential: dict,
    instruction_type: str,
    model_override: str | None = None,
) -> list[dict]:
    """API-key-mode equivalent of _run_provider_via_bridge_locked() -- same
    return shape (one record, same keys) so it flows unchanged into
    classify_run_status()/append_chat_message(). `session_id` and `run_dir`
    are always None: there is no provider-managed session and no local run
    directory in this mode.

    Deliberately does not touch .handoff/state.json/current.md -- those are
    the CLI-handoff-specific durable state files (docs/architecture.md's
    "State Boundaries"), and API-key mode has no CLI session or auto-
    fallback-to-the-other-provider concept for them to record. What
    started as chat-only (DEC-13) now also runs the tool-use turn loop
    (CFL-17/DEC-21, inside call_anthropic_messages_api()/
    call_openai_responses_api() themselves) -- this function's own
    contract doesn't change either way, since a plain chat turn is just
    the loop's zero-tool-calls case.

    `model_override` mirrors CLI mode's per-call --model: takes priority
    over the model saved alongside the credential when the caller supplies
    one (not reachable through the shipped composer UI today, same as CLI
    mode's `model` parameter -- see run_provider_via_bridge()'s docstring).
    """
    model = model_override or credential.get("model") or API_KEY_MODE_DEFAULT_MODELS.get(provider)
    if not model:
        return [
            _api_key_mode_error_record(
                provider,
                None,
                instruction_type,
                f"{provider} API-key mode has no model configured -- set one when saving the API key "
                "(no built-in default exists for this provider; see docs/research-api-key-mode.md)",
            )
        ]
    messages = build_api_message_history(workspace, prompt, utc_now())
    callers = {
        "claude": call_anthropic_messages_api,
        "codex": call_openai_responses_api,
        "gemini": call_gemini_api,
    }
    result = callers[provider](credential["key"], model, messages, workspace)
    if not result["ok"]:
        return [_api_key_mode_error_record(provider, model, instruction_type, result["message"])]
    return [
        {
            "provider": provider,
            "model": model,
            "instruction_type": instruction_type,
            "exit_code": 0,
            "session_id": None,
            "final_text": result["text"] or "(empty response)",
            "handoff_needed": False,
            "reason": "none",
            "run_dir": None,
        }
    ]


def _api_key_mode_error_record(provider: str, model: str | None, instruction_type: str, message: str) -> dict:
    return {
        "provider": provider,
        "model": model or "app-selected default",
        "instruction_type": instruction_type,
        "exit_code": 1,
        "session_id": None,
        "final_text": message,
        "handoff_needed": True,
        "reason": f"tool_failure: api_key_mode: {message}",
        "run_dir": None,
    }


PROVIDER_RUN_TIMEOUT_SECONDS = 600

# run_provider_via_bridge() reads .handoff/state.json's history length
# before the subprocess call and diffs against it after, with no lock in
# between -- two concurrent POST /api/run calls (e.g. the Enter-key path in
# webui/app.js doesn't check whether a run is already in flight) would both
# read the same "before" length, and whichever finishes second would slice
# in the first call's already-persisted record too, duplicating it as a
# second agent chat message. There's only one AppState.workspace server-wide,
# so a single process-wide lock (not a per-workspace one) is correct here --
# every concurrent run is necessarily against the same active workspace.
# A plain threading.Lock, not handoff_bridge.WriteLock: the contention here
# is between HTTP request threads in this one process, not separate CLI
# processes, and WriteLock's 10s default timeout is far too short for a
# provider call that can legitimately take minutes.
_RUN_LOCK = threading.Lock()


class RunAlreadyInProgressError(Exception):
    """Raised instead of silently blocking/racing when a second
    POST /api/run arrives while one is still in flight."""

# Killing the outer handoff_bridge.py wrapper on timeout does NOT kill the
# real codex/claude child it spawned -- subprocess.run() only signals the
# immediate child, not its descendants, since neither process runs in its
# own process group. So the per-provider budget is delegated to the bridge
# itself via --timeout-seconds, which applies the timeout to the actual
# provider subprocess.run() call and can therefore really terminate it.
# This outer wrapper timeout becomes a hard-kill backstop for cases outside
# provider execution (e.g. the bridge process itself hanging on I/O) --
# generous enough to cover two sequential --timeout-seconds budgets (a
# rate-limited first provider auto-falling-back into a second one that also
# times out), plus real slack: each provider call's save_state()/
# append_current() also goes through handoff_bridge.WriteLock, which alone
# can block up to its own 10s timeout under contention (e.g. a second
# browser tab's /api/run racing this one) -- up to 2x that across both
# calls in a fallback chain, on top of ordinary process-startup overhead.
OUTER_SUBPROCESS_TIMEOUT_SECONDS = PROVIDER_RUN_TIMEOUT_SECONDS * 2 + 60


def classify_run_status(handoff_needed: bool, reason: str) -> str:
    """Map a handoff_bridge.py history record's (handoff_needed, reason) to
    one of the three terminal run states from
    docs/design-system/components.html §3/§9. `reason` always starts with
    one of handoff_bridge.HANDOFF_LABELS or "none" -- classify_handoff()'s
    own contract, enforced by scripts/validate_handoff.py.
    """
    if not handoff_needed:
        return "success"
    if reason.startswith("tool_failure") or reason.startswith("unknown"):
        return "fail"
    return "handoff"


def read_state_dict(workspace: Path) -> dict:
    state_path = workspace / ".handoff" / "state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_state_history(workspace: Path) -> list[dict]:
    return read_state_dict(workspace).get("history", [])


def build_run_prompt(text: str, attachments: list[dict]) -> str:
    """Combine the composer text with any attached files into the single
    string that becomes the provider's actual prompt.

    Without this, an attachment only ever reached the chat log (POST
    /api/chat persists it on the "user" message) -- POST /api/run sent just
    `text`, so a file the user thought they'd attached was never part of
    what the provider actually saw. docs/design-system/wireframes.html
    describes attachments as context for "the next message", so that's the
    contract this restores.
    """
    parts = [text] if text else []
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("path") or "attachment"
        content = attachment.get("content")
        header = f"### Attached file: {name}"
        if content is None:
            parts.append(f"{header}\n(binary or unreadable -- no preview available)")
        else:
            note = " (truncated)" if attachment.get("truncated") else ""
            parts.append(f"{header}{note}\n```\n{content}\n```")
    return "\n\n".join(parts)


def run_provider_via_bridge(
    workspace: Path, provider: str, prompt: str, model: str | None, instruction_type: str
) -> list[dict]:
    """Thin locking wrapper around `_run_provider_via_bridge_locked()`.

    Fails fast with `RunAlreadyInProgressError` instead of silently
    blocking (a provider call can legitimately take up to
    `OUTER_SUBPROCESS_TIMEOUT_SECONDS`) or racing (see `_RUN_LOCK`'s
    comment) when a second call arrives while one is already in flight.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
    try:
        return _run_provider_via_bridge_locked(workspace, provider, prompt, model, instruction_type)
    finally:
        _RUN_LOCK.release()


def _run_provider_via_bridge_locked(
    workspace: Path, provider: str, prompt: str, model: str | None, instruction_type: str
) -> list[dict]:
    """Invoke `handoff_bridge.py run <provider> --execute --auto-fallback`
    against `workspace` and return the new handoff_bridge.py history
    record(s) it appended to .handoff/state.json -- more than one if
    auto-fallback chained into a second provider.

    If the subprocess itself fails before handoff_bridge.py ever gets to
    classify_handoff()/save_state() (e.g. the interpreter can't even start),
    no history record exists to read back -- synthesize one so callers
    always get at least one result to show and persist, instead of silently
    returning nothing.

    Phase 4 (DEC-13/16): before shelling out, check whether this call
    should go over a provider's HTTP API instead -- only when its CLI is
    genuinely absent and a key is saved for it, so every previously-existing
    behavior (a CLI-available provider, or a CLI-missing one with no key
    saved) is completely unchanged by this branch.
    """
    if provider == "auto":
        if not any(cli_available(p) for p in PROVIDERS):
            # Only read credentials.json once we already know no CLI is
            # available -- the common case (some CLI installed) never
            # touches disk for a value it would just discard unused.
            credentials = read_credentials()
            api_key_provider = next((p for p in PROVIDERS if p in credentials), None)
            if api_key_provider is None:
                return [
                    _api_key_mode_error_record(
                        # PROVIDERS[0], not the literal "auto" -- the /api/run
                        # handler persists this into the chat log's `provider`
                        # field verbatim (append_chat_message(provider=record
                        # ["provider"])), and "auto" must never end up stored
                        # there (the same invariant the CLI-side synthetic-
                        # record path already enforces for the same reason;
                        # see its own comment a few lines below).
                        PROVIDERS[0],
                        None,
                        instruction_type,
                        "no provider CLI is installed and no API key is configured for any provider -- "
                        "install codex or claude, or open the connection panel to add an API key",
                    )
                ]
            return run_provider_via_api_key(
                workspace, api_key_provider, prompt, credentials[api_key_provider], instruction_type, model
            )
        # At least one CLI exists -- fall through to the existing subprocess
        # path, which already does its own choose_auto_provider() and
        # --auto-fallback among CLI-available providers.
    elif not cli_available(provider):
        # Same reasoning as above: only read credentials.json once the CLI
        # is confirmed unavailable, so the common (CLI installed) case
        # skips the disk read + JSON parse entirely.
        credentials = read_credentials()
        if provider in credentials:
            return run_provider_via_api_key(
                workspace, provider, prompt, credentials[provider], instruction_type, model
            )
        # else (a specific provider with no CLI and no saved key): fall
        # through unchanged -- the subprocess call below fails with
        # FileNotFoundError -> exit_code 127, exactly as it did before this
        # phase existed.

    before = len(read_state_history(workspace))

    # The prompt travels via --prompt-file, not as a trailing argv
    # positional: a long/multi-line prompt as a bare CLI arg risks hitting
    # OS argv-length limits and shows up in the local process list, and
    # (found via a CI-only failure) a bare positional interleaved after
    # `--instruction-type <value>` parses inconsistently across argparse
    # versions -- Python 3.11 rejected it as an unrecognized argument
    # while 3.14 accepted it. A file avoids both problems.
    prompt_fd, prompt_path_str = tempfile.mkstemp(prefix="webui-run-prompt-", suffix=".txt")
    prompt_path = Path(prompt_path_str)
    try:
        with os.fdopen(prompt_fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)

        command = bridge_command_prefix() + [
            "--workspace",
            str(workspace),
            "run",
            provider,
            "--execute",
            "--auto-fallback",
            "--instruction-type",
            instruction_type,
            "--prompt-file",
            str(prompt_path),
            "--timeout-seconds",
            str(PROVIDER_RUN_TIMEOUT_SECONDS),
        ]
        if model:
            # "--model=value", not ["--model", value]: with the latter, a
            # model string that happens to start with "-" would make
            # argparse treat it as the next flag instead of --model's
            # value ("argument --model: expected one argument"). Not
            # reachable through the shipped UI today (it never sends
            # `model`), but --prompt-file/`init ... -- task` already
            # closed the same class of gap elsewhere, so closing it here
            # too rather than leaving it for whenever `model` is wired up.
            command.append(f"--model={model}")

        hit_outer_timeout = False
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                # Without an explicit encoding, subprocess falls back to
                # locale.getpreferredencoding() to decode stdout/stderr --
                # not UTF-8 on a non-UTF-8-locale Windows machine -- and
                # the bridge subprocess's own output can reflect arbitrary
                # provider/prompt content. See run_provider()'s matching
                # fix in handoff_bridge.py for the confirmed crash this
                # class of bug produces.
                encoding="utf-8",
                errors="replace",
                timeout=OUTER_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
            stderr_tail = (result.stderr or "").strip()[-2000:]
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            # The hard-kill backstop actually fired -- the bridge process
            # (and whatever provider subprocess it was waiting on) got
            # killed before it could finish writing/saving anything for
            # that in-flight call. A per-provider --timeout-seconds
            # timeout, by contrast, is caught inside the bridge itself and
            # returns normally with exit_code 124 plus a real saved record,
            # so it never reaches this branch.
            hit_outer_timeout = True
            stderr_tail = f"timed out after {OUTER_SUBPROCESS_TIMEOUT_SECONDS}s"
            exit_code = 124
        except OSError as exc:
            stderr_tail = str(exc)
            exit_code = 127
    finally:
        prompt_path.unlink(missing_ok=True)

    new_records = read_state_history(workspace)[before:]
    if new_records:
        if hit_outer_timeout:
            # This can fire mid-auto-fallback: e.g. codex's own record is
            # already saved but the recursive claude call never got to
            # finish and save its own. Without this, the caller would
            # silently see only the first record and have no idea a second
            # attempt was even in flight.
            #
            # Phase 5 bug, found in review: this used to be a hardcoded
            # "claude" if codex else "codex" binary guess -- other_provider()'s
            # own replacement (next_provider()) generalized the *real*
            # fallback logic in handoff_bridge.py, but this webui-local
            # guess was never updated to match. With PROVIDERS now 3-wide,
            # the recursive fallback call handoff_bridge.py actually made
            # is next_available_provider(new_records[-1]["provider"]) (a
            # second review found run_provider() itself needed the
            # CLI-availability-aware variant, not plain next_provider(),
            # so this guess has to match exactly that or it can name the
            # wrong provider whenever the naive next-in-order pick isn't
            # installed) -- e.g. a claude run needing handoff recurses
            # into gemini, not codex, and skips further if gemini isn't
            # installed either. Reusing the same function here (rather
            # than reimplementing the guess) is the only way this stays
            # correct if PROVIDERS' order or installed set ever changes.
            timed_out_provider = next_available_provider(new_records[-1]["provider"])
            new_records.append(
                {
                    "provider": timed_out_provider,
                    "model": model or "app-selected default",
                    "instruction_type": instruction_type,
                    "exit_code": exit_code,
                    "session_id": None,
                    "final_text": f"Timed out after {OUTER_SUBPROCESS_TIMEOUT_SECONDS}s waiting for a reply.",
                    "handoff_needed": True,
                    "reason": "tool_failure: subprocess did not produce a history record (exit 124)",
                    "run_dir": None,
                }
            )
        return new_records
    # No history record exists to read the real provider back from, so
    # "auto" (schema: docs/webui-chat-storage.md) must still be resolved
    # here -- otherwise a synthetic record could persist "auto" as a
    # chat-log `provider` value, which callers never expect to see.
    resolved_provider = (
        choose_auto_provider(read_state_dict(workspace)) if provider == "auto" else provider
    )
    return [
        {
            "provider": resolved_provider,
            "model": model or "app-selected default",
            "instruction_type": instruction_type,
            "exit_code": exit_code,
            "session_id": None,
            "final_text": stderr_tail or f"handoff_bridge.py run exited {exit_code} with no output",
            "handoff_needed": True,
            "reason": f"tool_failure: subprocess did not produce a history record (exit {exit_code})",
            "run_dir": None,
        }
    ]


def build_handler(state: "AppState") -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentHandoffWebUI/0.2"

        def log_message(self, fmt: str, *args: object) -> None:  # quieter default logging
            sys.stderr.write(f"[webui] {self.address_string()} - {fmt % args}\n")

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2_000_000:
                raise WorkspaceError("missing or oversized request body")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceError(f"invalid JSON body: {exc}") from exc
            if not isinstance(data, dict):
                raise WorkspaceError("request body must be a JSON object")
            return data

        def _send_static(self, rel_name: str) -> None:
            path = WEBUI_ROOT / rel_name
            if not path.is_file():
                self.send_error(404, "not found")
                return
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text") or "javascript" in content_type else content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            rel_path = (query.get("path", [""])[0]).strip()
            workspace = state.workspace

            if parsed.path == "/":
                self._send_static("index.html")
            elif parsed.path in ("/app.css", "/app.js"):
                self._send_static(parsed.path.lstrip("/"))
            elif parsed.path == "/api/info":
                if workspace is None:
                    self._send_json(200, {"workspace": None, "name": None})
                else:
                    self._send_json(200, {"workspace": str(workspace), "name": workspace.name or str(workspace)})
            elif parsed.path == "/api/tree":
                if workspace is None:
                    # Nothing to browse yet -- an empty tree, not an error;
                    # the "no workspace" screen is the expected state here.
                    self._send_json(200, {"path": rel_path, "entries": []})
                else:
                    try:
                        entries = list_tree_entries(workspace, rel_path)
                        self._send_json(200, {"path": rel_path, "entries": entries})
                    except WorkspaceError as exc:
                        self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/file":
                if workspace is None:
                    self._send_json(400, {"error": "no workspace selected"})
                else:
                    try:
                        preview = read_file_preview(workspace, rel_path)
                        self._send_json(200, preview)
                    except WorkspaceError as exc:
                        self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/chat":
                if workspace is None:
                    month = (query.get("month", [""])[0]).strip() or month_key(utc_now())
                    self._send_json(200, {"month": month, "months": [], "messages": []})
                else:
                    month = (query.get("month", [""])[0]).strip() or month_key(utc_now())
                    messages = read_month_messages(workspace, month)
                    self._send_json(
                        200, {"month": month, "months": list_available_months(workspace), "messages": messages}
                    )
            elif parsed.path == "/api/history":
                self._send_json(200, {"groups": build_history_drawer(workspace)})
            elif parsed.path == "/api/providers":
                # Backs SCR-06/components.html §14's connection panel. Never
                # includes the raw key -- only whether one is configured.
                # Covers the full (Gemini-included) PROVIDERS for CLI
                # detection -- Phase 5 finally resolves what SCR-06
                # originally shipped as a "미확인" placeholder badge for
                # Gemini into a real one. `api_key_mode_supported` is now
                # True for Gemini too (DEC-25) -- API_KEY_MODE_PROVIDERS
                # was extended to include it, so the frontend offers a key
                # field for it exactly like codex/claude.
                credentials = read_credentials()
                providers = []
                for provider in PROVIDERS:
                    supports_api_key_mode = provider in API_KEY_MODE_PROVIDERS
                    entry = credentials.get(provider) if supports_api_key_mode else None
                    providers.append(
                        {
                            "provider": provider,
                            "cli_detected": cli_available(provider),
                            "api_key_mode_supported": supports_api_key_mode,
                            "api_key_configured": entry is not None,
                            "model": (entry or {}).get("model") if entry else None,
                        }
                    )
                self._send_json(200, {"providers": providers})
            elif parsed.path == "/api/update-check":
                # Phase 6 (SCR-07): reads the cached result of the
                # background check main() kicked off at startup -- never
                # runs `gh` itself on this request path, so this is
                # always a fast, synchronous read regardless of network
                # conditions.
                #
                # `checked` (review fix: a real race, not a nitpick) lets
                # the frontend tell "still checking" (poll again shortly)
                # apart from "checked, no update" (stop asking) -- the
                # real `gh` call is network I/O and can easily still be
                # in flight by the time the page's first request for this
                # arrives, especially right after server startup.
                #
                # Read `checked` BEFORE `info`, opposite of the write
                # order in _check_for_update_in_background() (info then
                # checked) -- a second review found this matters: if the
                # background thread's two writes land in the gap between
                # this handler's own two reads, reading `info` first
                # could observe the pre-write `None` alongside a
                # freshly-written `checked = True`, reporting "checked,
                # no update" for a request that actually raced a real
                # update being found. Reading `checked` first instead
                # means the only possible stale read is `checked = False`
                # paired with a not-yet-visible `info` -- which just tells
                # the polling client to ask again, the safe direction to
                # be wrong in (under-reporting readiness self-corrects on
                # the next poll; over-reporting readiness with stale data
                # does not, since the client stops polling on `checked:
                # true`).
                checked = state.update_checked
                # Only look at `info` at all once `checked` was observed
                # True -- if the writer hasn't gotten there yet, `info`
                # isn't meaningful regardless of what value happens to be
                # sitting in it, so this never spreads a value the
                # `checked: false` response shouldn't be making claims
                # about either way. Once checked, `info` is always a dict
                # with a `status` field (CFL-18: "available"/"current"/
                # "unavailable" -- check_for_update() never returns None
                # anymore, precisely so "genuinely current" and "couldn't
                # check at all" are distinguishable here instead of both
                # collapsing into the same response).
                info = state.update_info if checked else None
                self._send_json(200, {"checked": checked, **(info or {})})
            else:
                # _send_json(), not send_error(): every other /api/* branch
                # in do_GET responds with a JSON body, and webui/app.js's
                # fetchJSON() unconditionally calls res.json() on whatever
                # comes back -- send_error()'s HTML body would surface as a
                # confusing JSON-parse error instead of a real "not found"
                # message if this branch is ever actually reached (a typo'd
                # path, a stale cached bundle hitting a renamed endpoint).
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/chat":
                try:
                    body = self._read_json_body()
                    role = str(body.get("role") or "user")
                    if role not in CLIENT_WRITABLE_CHAT_ROLES:
                        # "agent" is only ever written by POST /api/run, right
                        # after a real provider call -- accepting it here
                        # would let a client forge a fake agent reply with no
                        # provider having actually run.
                        raise WorkspaceError(f"role must be one of {CLIENT_WRITABLE_CHAT_ROLES}")
                    if role == "user" and _RUN_LOCK.locked():
                        # pair_messages_into_turns() (Phase 3) walks the chat
                        # log in append order and attaches each agent reply
                        # to whichever user message it saw most recently --
                        # a second tab/client posting a new user message
                        # while another run is still in flight could get
                        # that in-flight run's eventual reply misattributed
                        # to the newer message once it lands in the drawer.
                        # Reject outright rather than let two turns overlap;
                        # this is a plain (non-atomic) check-then-append, not
                        # airtight against a run starting in the gap, but it
                        # closes the realistic window (the run's full
                        # duration) down to a few instructions.
                        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
                    text = str(body.get("text") or "")
                    attachments = body.get("attachments") or []
                    if not isinstance(attachments, list):
                        raise WorkspaceError("attachments must be a list")
                    if state.workspace is None:
                        # DEC-05: creation is deferred all the way to here --
                        # the first *user* message, regardless of whether the
                        # "새 폴더 자동 생성" button was clicked first. A
                        # "system"-role post can't carry a summary and
                        # shouldn't silently create a workspace as a
                        # side effect of something other than the user
                        # actually sending something.
                        if role != "user":
                            raise WorkspaceError("no workspace selected")
                        # Double-checked locking: _WORKSPACE_CREATE_LOCK
                        # serializes creation, and re-checking workspace is
                        # None *after* acquiring it means a request that lost
                        # the race just uses the workspace the winner already
                        # created, instead of creating a second one.
                        with _WORKSPACE_CREATE_LOCK:
                            if state.workspace is None:
                                state.workspace = create_workspace_for_first_message(text, attachments)
                                touch_registry(state.workspace, utc_now())
                    message = append_chat_message(state.workspace, role, text, attachments, utc_now())
                    self._send_json(200, message)
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/open-folder":
                try:
                    if _RUN_LOCK.locked():
                        # A provider run writes into whatever workspace was
                        # active when it started and persists into that
                        # workspace's chat log when it finishes -- switching
                        # state.workspace out from under an in-flight run
                        # would misdirect where that write (and everything
                        # the client renders once the run resolves) ends up.
                        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
                    body = self._read_json_body()
                    candidate = validate_workspace_candidate(str(body.get("path") or ""))
                    state.workspace = candidate
                    ensure_chat_gitignore(candidate)
                    archive_old_months(candidate, utc_now())
                    touch_registry(candidate, utc_now())
                    self._send_json(200, {"workspace": str(candidate), "name": candidate.name or str(candidate)})
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/run":
                try:
                    body = self._read_json_body()
                    provider = str(body.get("provider") or "auto")
                    if provider not in ("auto",) + PROVIDERS:
                        raise WorkspaceError(f"invalid provider: {provider}")
                    text = str(body.get("text") or "").strip()
                    attachments = body.get("attachments") or []
                    if not isinstance(attachments, list):
                        raise WorkspaceError("attachments must be a list")
                    if not text and not attachments:
                        raise WorkspaceError("text or attachments required")
                    if state.workspace is None:
                        # Normal flow always creates the workspace as a side
                        # effect of the preceding POST /api/chat -- this is
                        # only reachable via a client bug or direct API use
                        # that skips it.
                        raise WorkspaceError("no workspace selected")
                    model = body.get("model") or None
                    workspace = state.workspace
                    prompt = build_run_prompt(text, attachments)
                    records = run_provider_via_bridge(workspace, provider, prompt, model, "continue")
                    messages = []
                    for record in records:
                        status = classify_run_status(record["handoff_needed"], record["reason"])
                        agent_text = record.get("final_text") or f"(exit {record['exit_code']}, no output)"
                        messages.append(
                            append_chat_message(
                                workspace,
                                "agent",
                                agent_text,
                                [],
                                utc_now(),
                                provider=record["provider"],
                                status=status,
                                reason=record["reason"],
                            )
                        )
                    self._send_json(200, {"messages": messages})
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/provider-key":
                try:
                    body = self._read_json_body()
                    provider = str(body.get("provider") or "")
                    if provider not in API_KEY_MODE_PROVIDERS:
                        raise WorkspaceError(f"invalid provider: {provider}")
                    key = str(body.get("key") or "").strip()
                    model = str(body.get("model") or "").strip() or None
                    # An empty key removes the credential (save_credential()'s
                    # contract) -- lets the connection panel's same "저장"
                    # action double as "disconnect API key mode".
                    #
                    # A non-empty key is verified with a real, minimal call
                    # (validate_provider_api_key()) *before* it is ever
                    # written to disk -- previously any non-empty string was
                    # saved unconditionally and only found to be wrong (or
                    # right) the next time the user actually tried to chat.
                    # This needs a model to call with, and
                    # API_KEY_MODE_DEFAULT_MODELS is deliberately empty for
                    # both providers (no built-in default, DEC-13/15) -- so a
                    # model is now required in the same request as a
                    # non-empty key, not merely recommended. Removal (empty
                    # key) skips validation entirely; there is nothing to
                    # verify when disconnecting.
                    confirmation: str | None = None
                    if key:
                        if not model:
                            raise WorkspaceError(
                                f"a model is required to save and verify a {provider} API key "
                                "(no built-in default exists for this provider)"
                            )
                        result = validate_provider_api_key(provider, key, model)
                        if not result["ok"]:
                            raise WorkspaceError(result["message"])
                        confirmation = result["text"]
                    # Unlike touch_registry() (best-effort, failure only
                    # logged -- always called *after* a real state change it
                    # shouldn't be allowed to desync from), this write IS the
                    # entire point of the request: the user is actively
                    # trying to save/remove a key, so a write failure
                    # (permissions, full disk, base dir exists as a file)
                    # must reach them as a real error, not disappear.
                    # save_credential() doesn't wrap OSError itself; do it
                    # here so it becomes a normal WorkspaceError -> 400 like
                    # every other failure this endpoint (and this whole
                    # feature) produces, instead of an uncaught exception
                    # crashing this request's thread with no JSON response.
                    try:
                        save_credential(provider, key, model)
                    except OSError as exc:
                        raise WorkspaceError(f"failed to save API key: {exc}") from exc
                    response = {"provider": provider, "api_key_configured": bool(key), "model": model if key else None}
                    if confirmation is not None:
                        response["verified"] = True
                        response["confirmation"] = confirmation
                    self._send_json(200, response)
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            else:
                # _send_json(), matching do_GET's fallback fix above and
                # every other /api/* branch's JSON-error contract.
                self._send_json(405, {"error": "unsupported POST endpoint"})

    return Handler


class AppState:
    """Mutable holder for the active workspace so "Open Folder" can switch
    it at runtime without restarting the server.

    `workspace` is `None` in Phase 2's "no workspace" state (DEC-04) --
    every handler that reads it must handle that case explicitly rather
    than assuming a `Path`.
    """

    def __init__(self, workspace: Path | None):
        self.workspace = workspace
        # Phase 6 (SCR-07): written once by a background thread started in
        # main() shortly after startup, read by GET /api/update-check.
        # Plain attributes, not behind a lock: single write-once-then-
        # read-many values, and CPython attribute assignment is already
        # atomic with respect to concurrent reads from other request
        # threads.
        #
        # update_checked distinguishes "the background check hasn't
        # finished yet" from "it finished and found nothing newer" --
        # both used to collapse into the same `update_info is None`,
        # which a review correctly flagged as a real race: the real `gh`
        # subprocess call is network I/O (can easily take a few seconds),
        # while the page load + its first GET /api/update-check can
        # finish well before that -- webui/app.js used to check exactly
        # once at boot, so a frontend that happened to ask before the
        # background check finished would permanently treat "still
        # checking" as "no update," even once the real answer arrived
        # moments later. app.js now polls while `checked` is false
        # instead of asking only once.
        #
        # update_info is None until update_checked is True, and always a
        # dict (never None) once it is -- check_for_update() (CFL-18)
        # always returns a dict with a `status` field
        # ("available"/"current"/"unavailable"), so "genuinely current"
        # and "couldn't check at all" stay distinguishable all the way
        # through to the frontend instead of both collapsing into the
        # same falsy value.
        self.update_checked = False
        self.update_info: dict | None = None


class Api:
    """Exposed to the frontend as window.pywebview.api when running as a
    native window. Only responsibility: show the OS folder picker and hand
    the chosen path back to JS -- the actual workspace switch still goes
    through POST /api/open-folder like the browser-mode fallback does, so
    there is exactly one code path that mutates AppState."""

    def pick_folder(self) -> str | None:
        if webview is None or not webview.windows:  # pragma: no cover - needs a real window
            return None
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MVP web UI: browse/switch workspace, chat, and run Codex/Claude (Phase 1: POST /api/run)."
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Initial workspace folder. Switchable at runtime. Defaults to the current "
        "directory if it's already an initialized handoff workspace (has .handoff/); "
        "otherwise starts in the Phase 2 'no workspace' state instead of assuming cwd "
        "is the intended project.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Must be loopback (127.0.0.1/localhost/::1) -- this server has no auth, so non-loopback hosts are refused at startup.",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open a regular browser tab instead of a native app window, even if pywebview is installed.",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open anything automatically.")
    return parser


def choose_ui_mode(prefer_browser: bool, webview_available: bool) -> str:
    """Pure decision so this branch is unit-testable without a display.

    Returns "native" or "browser". Native is preferred (this is meant to
    feel like an app, not a browser tab) but only when pywebview actually
    imported; --browser always forces the tab.
    """
    if prefer_browser:
        return "browser"
    return "native" if webview_available else "browser"


# Same set remote_handoff_server.py checks before allowing --no-auth. This
# server has no auth mechanism at all -- unlike remote_handoff_server.py,
# there is no flag that makes a non-loopback bind acceptable here, so the
# check is unconditional rather than gated behind --no-auth.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _check_for_update_in_background(state: "AppState") -> None:
    """Runs check_for_update() (a real `gh` subprocess call -- network
    I/O, can take a few seconds) off the startup path so server boot and
    the browser/native window opening aren't delayed by it. Matches
    docs/design-system/wireframes.html SCR-07's "앱 시작 시 백그라운드로
    최신 릴리즈 확인" -- once at startup, not on every request.
    check_for_update() itself never raises (gh missing/unauthenticated/
    offline all just resolve to a `{"status": "unavailable", ...}` dict,
    CFL-18), so nothing here needs its own try/except.

    Sets `update_info` before `update_checked` -- a reader that observes
    `update_checked is True` must never see a stale/uninitialized
    `update_info` alongside it."""
    state.update_info = check_for_update()
    state.update_checked = True


def main(argv: list[str] | None = None) -> int:
    # Phase 7a (DEC-22): when this process is spawned as a Tauri sidecar
    # (src-tauri/src/lib.rs), the caller waits for a specific line on
    # this process's stdout before it will create the app window at
    # all -- but CPython only line-buffers stdout when it's a real tty;
    # piped to another process (exactly what a sidecar's stdout is),
    # it's fully block-buffered by default, so the readiness print below
    # could sit unflushed in this process's own memory indefinitely
    # (this is a long-running server that never naturally exits to
    # trigger an on-exit flush). Setting PYTHONUNBUFFERED=1 on the
    # sidecar spawn was tried first and empirically did NOT reliably
    # reach this process through PyInstaller's onefile bootloader when
    # actually tested against the built binary -- reconfiguring the
    # stream directly here is unaffected by that and by any other
    # environment-variable propagation question.
    sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    if not is_loopback_host(args.host):
        print(f"refusing to bind to non-loopback host: {args.host!r}", file=sys.stderr)
        print(f"this server has no authentication -- only {sorted(LOOPBACK_HOSTS)} are allowed", file=sys.stderr)
        return 1
    workspace, error = resolve_startup_workspace(args.workspace, Path.cwd())
    if error:
        print(error, file=sys.stderr)
        return 1

    state = AppState(workspace)
    if state.workspace is not None:
        ensure_chat_gitignore(state.workspace)
        archive_old_months(state.workspace, utc_now())
        touch_registry(state.workspace, utc_now())  # DEC-10: CLI startup counts too

    threading.Thread(target=_check_for_update_in_background, args=(state,), daemon=True).start()

    handler = build_handler(state)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        if exc.errno in (48, 98, 10048):  # EADDRINUSE: macOS, Linux, Windows
            print(PORT_CONFLICT_MARKER, file=sys.stderr)
        raise
    url = f"http://{args.host}:{args.port}/"
    if workspace is not None:
        print(f"Agent Handoff Bridge web UI (MVP) serving {workspace}")
    else:
        print("Agent Handoff Bridge web UI (MVP) -- no workspace yet")
        print(f"  Send a message or use Open Folder; a folder is auto-created under {AUTO_WORKSPACE_BASE_DIR} otherwise.")
    print(f"  {url}")
    print("  File browsing + local chat, and POST /api/run actually calls Codex/Claude.")
    print(f"  Chat history: <workspace>/{CHAT_DIR_RELATIVE.as_posix()}/ (monthly, compressed after month-end)")

    mode = choose_ui_mode(args.browser, webview is not None)
    if mode == "browser" and webview is None and not args.browser and not args.no_browser:
        print(f"  pywebview not installed ({WEBVIEW_IMPORT_ERROR}); opening a browser tab instead.")
        print("  For a native app window and native 'Open Folder' dialog: pip install pywebview")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    if args.no_browser:
        print("  Ctrl+C to stop.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
    elif mode == "native":
        window_title = f"Agent Handoff Bridge — {workspace.name if workspace is not None else '워크스페이스 없음'}"
        webview.create_window(window_title, url, width=1100, height=760, min_size=(720, 480), js_api=Api())
        webview.start()  # blocks until the window is closed
    else:
        webbrowser.open(url)
        print("  Ctrl+C to stop.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass

    httpd.shutdown()
    httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
