"""What a provider CLI is doing right now, read from its live event stream.

`handoff_bridge.py run --execute` points the provider's stdout straight at
`.handoff/runs/<run-id>/stdout.jsonl`, so the file grows while the CLI works.
The Web UI polls `GET /api/run-progress`, which calls `read_progress()` here to
turn the newest event into a small `{"kind", "detail"}` pair; the page renders
that with its own (translated) wording, the way a terminal shows "Running
command ..." under the spinner.

Only codex and claude emit events as they go. agy prints one envelope when it is
done, so for it (and before the first event) the answer is just "working".

Nothing here is stored: a command line shown to the user is read from the run
log the user already has on disk, and goes no further than the local UI.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RUNS_DIR_PARTS = (".handoff", "runs")
# Events can carry whole tool outputs; the newest labelled event is always near
# the end, so only the tail of the file is ever read.
TAIL_BYTES = 128 * 1024
DETAIL_MAX_CHARS = 90

# Claude tool name -> kind. Unlisted tools are shown as a generic "tool" step.
_CLAUDE_TOOL_KINDS = {
    "Bash": "command",
    "Read": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Write": "write",
    "Grep": "search",
    "Glob": "search",
    "WebSearch": "web",
    "WebFetch": "web",
}
# The input field that best says what the tool is acting on.
_CLAUDE_DETAIL_FIELDS = {
    "command": ("command",),
    "read": ("file_path", "path"),
    "edit": ("file_path", "notebook_path", "path"),
    "write": ("file_path", "path"),
    "search": ("pattern", "query"),
    "web": ("query", "url"),
}
_SHELL_WRAPPER = re.compile(r"^(?:\S*/)?(?:ba|z|da|k)?sh\s+-\w*c\s+(.*)$", re.DOTALL)


def _tidy(text: Any) -> str:
    """One short line: shell wrapper and quotes dropped, whitespace collapsed."""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    match = _SHELL_WRAPPER.match(text)
    if match:
        text = match.group(1).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
    text = " ".join(text.split())
    if len(text) > DETAIL_MAX_CHARS:
        text = text[: DETAIL_MAX_CHARS - 1].rstrip() + "…"
    return text


def _step(kind: str, detail: str = "") -> dict[str, str]:
    return {"kind": kind, "detail": _tidy(detail)}


def _codex_step(event: dict[str, Any]) -> dict[str, str] | None:
    etype = event.get("type")
    if etype in ("thread.started", "turn.started"):
        return _step("starting")
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    started = etype == "item.started"
    if itype == "command_execution":
        # A finished command means it is deciding what to do with the result.
        return _step("command", item.get("command") or "") if started else _step("thinking")
    if itype == "reasoning":
        return _step("thinking")
    if itype == "file_change":
        changes = item.get("changes")
        first = changes[0].get("path") if isinstance(changes, list) and changes and isinstance(changes[0], dict) else ""
        return _step("edit", first or "")
    if itype == "mcp_tool_call":
        if not started:
            return _step("thinking")
        return _step("tool", f"{item.get('server') or ''} {item.get('tool') or ''}".strip())
    if itype == "web_search":
        return _step("web", item.get("query") or "")
    if itype == "todo_list":
        return _step("thinking")
    if itype == "agent_message":
        return _step("writing")
    return None


def _claude_step(event: dict[str, Any]) -> dict[str, str] | None:
    etype = event.get("type")
    if etype == "system":
        return _step("starting") if event.get("subtype") == "init" else None
    if etype == "user":
        return _step("thinking")  # a tool result came back; it is reading it
    if etype != "assistant":
        return None
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    # The last block is what it is doing now (a message can hold text, then a tool call).
    for block in reversed(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name") or ""
            kind = _CLAUDE_TOOL_KINDS.get(name, "tool")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            detail = ""
            for field in _CLAUDE_DETAIL_FIELDS.get(kind, ()):
                value = tool_input.get(field)
                if isinstance(value, str) and value:
                    detail = value
                    break
            return _step(kind, detail or (name if kind == "tool" else ""))
        if btype == "thinking":
            return _step("thinking")
        if btype == "text":
            return _step("writing")
    return None


def describe_event(provider: str, event: Any) -> dict[str, str] | None:
    """`{"kind", "detail"}` for an event that says what the CLI is doing, else None.

    kinds: starting, thinking, command, read, edit, write, search, web, tool,
    writing. `detail` is a short single line (a command, a path, a query) or "".
    """
    if not isinstance(event, dict):
        return None
    if provider == "codex":
        return _codex_step(event)
    if provider == "claude":
        return _claude_step(event)
    return None


def _tail_lines(path: Path) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read()
            truncated = size > TAIL_BYTES
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]  # the first line was cut in half
    return lines


def current_step(provider: str, stdout_path: Path) -> dict[str, str] | None:
    """The newest recognised step in a (possibly still growing) stdout.jsonl."""
    for line in reversed(_tail_lines(stdout_path)):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue  # a line still being written
        step = describe_event(provider, event)
        if step is not None:
            return step
    return None


def runs_dir(workspace: Path) -> Path:
    return workspace.joinpath(*RUNS_DIR_PARTS)


def run_started_prefix(utc_now: Any) -> str:
    """The run-directory name prefix (`%Y%m%dT%H%M%SZ`) of a given UTC time."""
    return utc_now.strftime("%Y%m%dT%H%M%SZ")


def newest_run_dir(workspace: Path, not_before_prefix: str) -> Path | None:
    """The newest `.handoff/runs/<stamp>-<provider>[-n]` created at or after
    `not_before_prefix`, so the previous run's directory is never mistaken for
    the one in progress."""
    try:
        names = [
            entry.name
            for entry in runs_dir(workspace).iterdir()
            if entry.is_dir() and entry.name[:16] >= not_before_prefix
        ]
    except OSError:
        return None
    if not names:
        return None
    return runs_dir(workspace) / max(names)


def read_progress(workspace: Path, not_before_prefix: str) -> dict[str, str]:
    """Progress of the run that started at `not_before_prefix`, or `{"kind": "working"}`
    when nothing readable exists yet (agy, or the first moments of a run)."""
    run_dir = newest_run_dir(workspace, not_before_prefix)
    if run_dir is None:
        return {"kind": "working", "detail": ""}
    provider = run_dir.name[17:].split("-")[0] if len(run_dir.name) > 17 else ""
    step = current_step(provider, run_dir / "stdout.jsonl") if provider else None
    result = dict(step) if step else {"kind": "working", "detail": ""}
    result["provider"] = provider
    return result
