"""Workspace resolution and file access: safe path handling under a
workspace root, the file tree/preview API's backing logic, validating a
user-supplied "Open Folder" path, and auto-creating a new workspace under
~/Documents/Agent Handoff Bridge/ for a first chat message with none
selected (DEC-04~07).
"""

from __future__ import annotations

import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from handoff_bridge import HANDOFF_DIR, normalize_path, short_run

import webui_common
from webui_common import WorkspaceError, bridge_command_prefix, utc_now
from webui_chat_storage import ensure_chat_gitignore

EXCLUDED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv"}

MAX_FILE_BYTES = 256_000


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
        webui_common.AUTO_WORKSPACE_BASE_DIR.mkdir(parents=True, exist_ok=True)
        # .resolve() to match the other two ways AppState.workspace ever
        # gets set (resolve_startup_workspace(), validate_workspace_candidate())
        # -- Path.home() doesn't itself resolve symlinks (e.g. ~/Documents
        # under iCloud Desktop & Documents sync), so without this, the same
        # physical folder reached different ways (auto-create now, Open
        # Folder or plain --workspace startup later) could stringify
        # differently and show up as two separate entries in the Phase 3
        # history registry instead of deduping to one.
        base_dir = webui_common.AUTO_WORKSPACE_BASE_DIR.resolve()
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

