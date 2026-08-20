"""Chat history storage: one JSONL file per calendar month per workspace,
gzip-compressed once a month is no longer current -- plus the cross-
workspace "recently opened" registry and the history-drawer pairing logic
built on top of it. Lives at <workspace>/.handoff/webui/chat/YYYY-MM.jsonl[.gz]
so it travels with the project, and registry.json under
AUTO_WORKSPACE_BASE_DIR (the same "the app owns this" location Phase 3
established, DEC-09).
"""

from __future__ import annotations

import gzip
import json
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from handoff_bridge import WriteLock, atomic_write_text

import webui_common
from webui_common import WorkspaceError, month_key

CHAT_DIR_RELATIVE = Path(".handoff") / "webui" / "chat"

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
    return webui_common.AUTO_WORKSPACE_BASE_DIR / "registry.json"


def read_registry() -> list[dict]:
    """Entries ordered most-recently-opened first. Never raises -- a
    missing, corrupt, or unreadable (permissions) registry file just
    means an empty "recently opened" list, same posture as
    read_state_history()."""
    data = webui_common.read_json_or_default(registry_path(), [])
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

