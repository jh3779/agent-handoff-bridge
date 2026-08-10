#!/usr/bin/env python3
"""Submit a task to a remote handoff server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from handoff_bridge import PROVIDERS as BRIDGE_PROVIDERS


# Derived from handoff_bridge.PROVIDERS (the canonical set, currently
# codex/claude/gemini) instead of a stale local copy -- see handoff_webui.py,
# which already imports PROVIDERS the same way. "auto" is a CLI-only concept
# added on top; handoff_bridge.py itself has no "auto" provider, and its own
# `init --primary` accepts the full PROVIDERS set with no restricted subset.
PROVIDERS = ("auto",) + BRIDGE_PROVIDERS


def load_token(args: argparse.Namespace) -> str | None:
    if args.token:
        return args.token
    if args.token_file:
        path = Path(args.token_file).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return os.environ.get("HANDOFF_REMOTE_TOKEN")


def request_json(method: str, url: str, token: str | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise SystemExit(f"request failed: {exc}") from exc


def submit(args: argparse.Namespace) -> int:
    token = load_token(args)
    payload = {
        "workspace": str(Path(args.workspace).expanduser()),
        "task": args.task,
        "prompt": args.prompt,
        "provider": args.provider,
        "primary": args.primary,
        "execute": args.execute,
        "auto_fallback": args.auto_fallback,
        "create_workspace": args.create_workspace,
    }
    base = args.url.rstrip("/")
    response = request_json("POST", f"{base}/tasks", token, payload)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    if not args.wait:
        return 0
    task_id = response["task_id"]
    while True:
        task = request_json("GET", f"{base}/tasks/{task_id}", token)
        status = task.get("status")
        print(f"{task_id}: {status}")
        if status in {"completed", "failed"}:
            print(json.dumps(task, indent=2, ensure_ascii=False))
            return int(task.get("exit_code") or 0)
        time.sleep(args.poll_interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit a remote handoff task.")
    parser.add_argument("task", help="Task instruction.")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Remote handoff server URL.")
    parser.add_argument("--workspace", "-W", default=".", help="Workspace folder on the server machine.")
    parser.add_argument("--prompt", default="Start the task", help="Turn prompt.")
    parser.add_argument("--provider", choices=PROVIDERS, default="auto")
    parser.add_argument("--primary", choices=BRIDGE_PROVIDERS, default="codex")
    parser.add_argument("--execute", action="store_true", help="Request real provider execution.")
    parser.add_argument("--auto-fallback", action="store_true", default=True)
    parser.add_argument("--no-create-workspace", dest="create_workspace", action="store_false")
    parser.add_argument("--wait", action="store_true", help="Poll until the remote task finishes.")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--token", help="Bearer token.")
    parser.add_argument("--token-file", help="Read bearer token from this file.")
    return parser


def main() -> int:
    return submit(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
