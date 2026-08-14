#!/usr/bin/env python3
"""Generic hook helper for recording Claude/Codex lifecycle events."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handoff_bridge import WriteLock, atomic_write_text  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        # Without an explicit encoding, subprocess falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine. A repo path can plausibly contain non-ASCII
        # characters (a Windows username, a localized folder name).
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path.cwd()


def read_event() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return {"hook_event_name": "unknown", "raw": raw}
    return event if isinstance(event, dict) else {"hook_event_name": "unknown", "raw": event}


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_current(root: Path, event: dict[str, Any]) -> None:
    handoff_dir = root / ".handoff"
    current = handoff_dir / "current.md"
    event_name = event.get("hook_event_name", "unknown")
    lines = [
        "",
        f"## Hook Event {utc_now()}",
        "",
        f"- Event: {event_name}",
    ]
    if "error" in event:
        lines.append(f"- Error: {event.get('error')}")
    if "error_details" in event:
        lines.append(f"- Error details: {event.get('error_details')}")
    if "session_id" in event:
        lines.append(f"- Session ID: {event.get('session_id')}")
    if "transcript_path" in event:
        lines.append(f"- Transcript: {event.get('transcript_path')}")
    addition = "\n".join(lines) + "\n"
    # Cross-process lock, matching handoff_bridge.py's own append_current():
    # this hook runs as a standalone process alongside `handoff_bridge.py run`
    # (Codex CLI side), and both append to the same .handoff/current.md. A
    # plain unlocked append here could be silently lost if it lands between
    # the other process's read-existing and its atomic os.replace().
    with WriteLock(handoff_dir / ".write.lock"):
        current.parent.mkdir(parents=True, exist_ok=True)
        existing = current.read_text(encoding="utf-8") if current.exists() else ""
        atomic_write_text(current, existing + addition)


def write_next_prompt(root: Path, event: dict[str, Any]) -> None:
    handoff_dir = root / ".handoff"
    current_text = (handoff_dir / "current.md").read_text(encoding="utf-8") if (handoff_dir / "current.md").exists() else ""
    error = event.get("error") or event.get("hook_event_name", "unknown")
    details = event.get("error_details") or event.get("last_assistant_message") or ""
    prompt = f"""Continue this task after a provider handoff.

Reason: {error}
Details: {details}

Read `.handoff/current.md`, inspect the workspace and git status, then continue
the task. Update `.handoff/current.md` before stopping.

Current packet:

{current_text}
"""
    (handoff_dir / "next-prompt.md").write_text(prompt, encoding="utf-8")


def main() -> int:
    root = repo_root()
    event = read_event()
    event["recorded_at"] = utc_now()
    handoff_dir = root / ".handoff"
    append_jsonl(handoff_dir / "hook-events.jsonl", event)
    append_current(root, event)
    if event.get("hook_event_name") == "StopFailure":
        write_next_prompt(root, event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
