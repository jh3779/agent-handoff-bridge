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
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from handoff_bridge import WriteLock

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


def append_chat_message(workspace: Path, role: str, text: str, attachments: list[dict], now: datetime) -> dict:
    if role not in ("user", "system"):
        raise WorkspaceError(f"invalid role: {role}")
    message = {
        "id": uuid.uuid4().hex,
        "ts": now.isoformat(),
        "role": role,
        "text": text,
        "attachments": attachments or [],
    }
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
                self._send_json(200, {"workspace": str(workspace), "name": workspace.name or str(workspace)})
            elif parsed.path == "/api/tree":
                try:
                    entries = list_tree_entries(workspace, rel_path)
                    self._send_json(200, {"path": rel_path, "entries": entries})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/file":
                try:
                    preview = read_file_preview(workspace, rel_path)
                    self._send_json(200, preview)
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/chat":
                month = (query.get("month", [""])[0]).strip() or month_key(utc_now())
                messages = read_month_messages(workspace, month)
                self._send_json(200, {"month": month, "months": list_available_months(workspace), "messages": messages})
            else:
                self.send_error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/chat":
                try:
                    body = self._read_json_body()
                    role = str(body.get("role") or "user")
                    text = str(body.get("text") or "")
                    attachments = body.get("attachments") or []
                    if not isinstance(attachments, list):
                        raise WorkspaceError("attachments must be a list")
                    message = append_chat_message(state.workspace, role, text, attachments, utc_now())
                    self._send_json(200, message)
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/open-folder":
                try:
                    body = self._read_json_body()
                    candidate = validate_workspace_candidate(str(body.get("path") or ""))
                    state.workspace = candidate
                    ensure_chat_gitignore(candidate)
                    archive_old_months(candidate, utc_now())
                    self._send_json(200, {"workspace": str(candidate), "name": candidate.name or str(candidate)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            else:
                # No other write/execute endpoints in this MVP by design.
                self.send_error(405, "unsupported POST endpoint")

    return Handler


class AppState:
    """Mutable holder for the active workspace so "Open Folder" can switch
    it at runtime without restarting the server."""

    def __init__(self, workspace: Path):
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
        description="MVP web UI: browse/switch workspace and draft attachments (no provider calls)."
    )
    parser.add_argument("--workspace", default=".", help="Initial workspace folder. Switchable at runtime.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. Keep this local-only.")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        print(f"workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
        return 1

    state = AppState(workspace)
    ensure_chat_gitignore(state.workspace)
    archive_old_months(state.workspace, utc_now())

    handler = build_handler(state)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Agent Handoff Bridge web UI (MVP) serving {workspace}")
    print(f"  {url}")
    print("  File browsing + local chat drafts only. No provider is called.")
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
        window_title = f"Agent Handoff Bridge — {workspace.name}"
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
