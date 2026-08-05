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
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from handoff_bridge import HANDOFF_DIR, WriteLock, atomic_write_text, choose_auto_provider

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "handoff_bridge.py"

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
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceError(f"must be an absolute path: {raw_path}")
    resolved = candidate.resolve()
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
    try:
        result = subprocess.run(
            # "--" guarantees `task` is always treated as the positional
            # argument, even if the user's first message happens to be (or
            # start with) something that looks like one of init's own
            # flags, e.g. a literal "--no-install" or "-h" -- without it,
            # argparse would consume that as an option instead and fail
            # with "the following arguments are required: task".
            [sys.executable, str(BRIDGE_SCRIPT), "--workspace", str(new_workspace), "init", "--", task],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        shutil.rmtree(new_workspace, ignore_errors=True)
        raise WorkspaceError(f"failed to scaffold new workspace: {exc}") from exc
    if result.returncode != 0:
        shutil.rmtree(new_workspace, ignore_errors=True)
        stderr_tail = (result.stderr or "").strip()[-500:]
        raise WorkspaceError(f"failed to scaffold new workspace (exit {result.returncode}): {stderr_tail}")

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
    if plain.exists():
        text = plain.read_text(encoding="utf-8")
    elif archived.exists():
        with gzip.open(archived, "rt", encoding="utf-8") as handle:
            text = handle.read()
    else:
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
    """
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

        command = [
            sys.executable,
            str(BRIDGE_SCRIPT),
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
            timed_out_provider = "claude" if new_records[-1]["provider"] == "codex" else "codex"
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
            else:
                self.send_error(404, "not found")

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
                    if provider not in ("auto", "codex", "claude"):
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
            else:
                # No other write/execute endpoints in this MVP by design.
                self.send_error(405, "unsupported POST endpoint")

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


def main(argv: list[str] | None = None) -> int:
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

    handler = build_handler(state)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
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
