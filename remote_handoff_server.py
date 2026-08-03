#!/usr/bin/env python3
"""HTTP server for remote handoff task instructions."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BRIDGE_ROOT = Path(__file__).resolve().parent
BRIDGE_SCRIPT = BRIDGE_ROOT / "handoff_bridge.py"
REMOTE_DIR = Path(".handoff/remote")
TASKS_DIR = REMOTE_DIR / "tasks"
TOKEN_FILE = REMOTE_DIR / "token"
PROVIDERS = {"auto", "codex", "claude"}
PRIMARY_PROVIDERS = {"codex", "claude"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_or_create_token(token_arg: str | None, token_file: Path) -> str | None:
    if token_arg:
        return token_arg
    env_token = os.environ.get("HANDOFF_REMOTE_TOKEN")
    if env_token:
        return env_token
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        pass
    return token


class RemoteHandoffServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], args: argparse.Namespace):
        super().__init__(server_address, handler_class)
        self.args = args
        self.token = None if args.no_auth else load_or_create_token(args.token, args.token_file)
        self.allow_roots = [Path(root).expanduser().resolve() for root in args.allow_root]
        self.allow_execute = args.allow_execute
        self.task_timeout = args.task_timeout


class Handler(BaseHTTPRequestHandler):
    server: RemoteHandoffServer

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.args.quiet:
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.write_json(HTTPStatus.OK, {"ok": True, "time": utc_now(), "execute_enabled": self.server.allow_execute})
            return
        if not self.authorized():
            return
        if parsed.path == "/tasks":
            tasks = []
            for path in sorted(TASKS_DIR.glob("*.json")):
                tasks.append(read_json(path))
            self.write_json(HTTPStatus.OK, {"tasks": tasks})
            return
        if parsed.path.startswith("/tasks/"):
            task_id = parsed.path.rsplit("/", 1)[-1]
            path = TASKS_DIR / f"{task_id}.json"
            if not path.exists():
                self.write_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
                return
            self.write_json(HTTPStatus.OK, read_json(path))
            return
        self.write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/tasks":
            self.write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self.authorized():
            return
        try:
            payload = self.read_json_body()
            task = normalize_task(payload, self.server)
        except ValueError as exc:
            self.write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        write_json(TASKS_DIR / f"{task['id']}.json", task)
        worker = threading.Thread(target=run_task, args=(task, self.server), daemon=True)
        worker.start()
        self.write_json(HTTPStatus.ACCEPTED, {"task_id": task["id"], "status": task["status"]})

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("missing JSON body")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def authorized(self) -> bool:
        token = self.server.token
        if token is None:
            return True
        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-Handoff-Token", "")
        bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if secrets.compare_digest(token, bearer) or secrets.compare_digest(token, header_token):
            return True
        self.write_json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid token"})
        return False

    def write_json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def normalize_task(payload: dict[str, Any], server: RemoteHandoffServer) -> dict[str, Any]:
    task_text = str(payload.get("task", "")).strip()
    if not task_text:
        raise ValueError("task is required")
    provider = str(payload.get("provider", "auto")).strip().lower()
    primary = str(payload.get("primary", "codex")).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError("provider must be one of: auto, codex, claude")
    if primary not in PRIMARY_PROVIDERS:
        raise ValueError("primary must be one of: codex, claude")
    execute = bool(payload.get("execute", False))
    if execute and not server.allow_execute:
        raise ValueError("server was not started with --allow-execute")

    raw_workspace = str(payload.get("workspace", ".")).strip() or "."
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    workspace = workspace.resolve()
    if not any(is_relative_to(workspace, root) for root in server.allow_roots):
        allowed = ", ".join(str(root) for root in server.allow_roots)
        raise ValueError(f"workspace must be under an allowed root: {allowed}")
    if not workspace.exists():
        if bool(payload.get("create_workspace", True)):
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")

    task_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
    return {
        "id": task_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "queued",
        "workspace": str(workspace),
        "task": task_text,
        "prompt": str(payload.get("prompt", "Start the task")).strip() or "Start the task",
        "provider": provider,
        "primary": primary,
        "execute": execute,
        "auto_fallback": bool(payload.get("auto_fallback", True)),
        "commands": [],
        "exit_code": None,
        "error": None,
    }


def update_task(task: dict[str, Any], **updates: Any) -> None:
    task.update(updates)
    task["updated_at"] = utc_now()
    write_json(TASKS_DIR / f"{task['id']}.json", task)


def run_command(task: dict[str, Any], args: list[str], timeout: int) -> int:
    started_at = utc_now()
    command = [sys.executable, str(BRIDGE_SCRIPT), "--workspace", task["workspace"], *args]
    command_record = {
        "started_at": started_at,
        "command": command,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }
    task["commands"].append(command_record)
    update_task(task)
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=None if timeout == 0 else timeout,
            check=False,
        )
        command_record["exit_code"] = result.returncode
        command_record["stdout"] = result.stdout[-8000:]
        command_record["stderr"] = result.stderr[-8000:]
        update_task(task)
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        command_record["exit_code"] = 124
        command_record["stdout"] = (exc.stdout or "")[-8000:]
        command_record["stderr"] = (exc.stderr or "command timed out")[-8000:]
        update_task(task)
        return 124


def run_task(task: dict[str, Any], server: RemoteHandoffServer) -> None:
    update_task(task, status="running")
    try:
        install_code = run_command(task, ["install"], server.task_timeout)
        if install_code != 0:
            update_task(task, status="failed", exit_code=install_code, error="install failed")
            return
        init_code = run_command(task, ["init", task["task"], "--primary", task["primary"]], server.task_timeout)
        if init_code != 0:
            update_task(task, status="failed", exit_code=init_code, error="init failed")
            return
        run_args = ["run", task["provider"]]
        if task["execute"]:
            run_args.append("--execute")
            if task["auto_fallback"]:
                run_args.append("--auto-fallback")
        run_args.append(task["prompt"])
        run_code = run_command(task, run_args, server.task_timeout)
        update_task(task, status="completed" if run_code == 0 else "failed", exit_code=run_code)
    except Exception as exc:
        update_task(task, status="failed", error=str(exc), exit_code=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve remote handoff task instructions over HTTP.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Use 127.0.0.1 by default.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    parser.add_argument("--allow-root", action="append", default=None, help="Allowed workspace root. Repeatable.")
    parser.add_argument("--allow-execute", action="store_true", help="Allow remote requests to spend provider tokens.")
    parser.add_argument("--no-auth", action="store_true", help="Disable token auth. Use only on trusted local networks.")
    parser.add_argument("--token", help="Bearer token. Defaults to HANDOFF_REMOTE_TOKEN or token file.")
    parser.add_argument("--token-file", type=Path, default=TOKEN_FILE, help="Token file path.")
    parser.add_argument("--task-timeout", type=int, default=0, help="Per bridge command timeout. 0 means no timeout.")
    parser.add_argument("--quiet", action="store_true", help="Suppress HTTP access logs.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.allow_root is None:
        args.allow_root = [str(Path.cwd())]
    if args.no_auth and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Refusing --no-auth on a non-local host.", file=sys.stderr)
        return 2
    server = RemoteHandoffServer((args.host, args.port), Handler, args)
    print(f"Remote handoff server listening on http://{args.host}:{args.port}")
    print(f"Allowed roots: {', '.join(str(root) for root in server.allow_roots)}")
    print(f"Execute enabled: {server.allow_execute}")
    if server.token is not None:
        print(f"Auth token file: {args.token_file}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRemote handoff server stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
