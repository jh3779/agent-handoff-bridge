#!/usr/bin/env python3
"""Bridge Claude Code CLI and Codex CLI through shared handoff files."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BRIDGE_VERSION = "0.1.0"

BRIDGE_ROOT = Path(__file__).resolve().parent
HANDOFF_DIR = Path(".handoff")
RUNS_DIR = HANDOFF_DIR / "runs"
STATE_FILE = HANDOFF_DIR / "state.json"
CURRENT_FILE = HANDOFF_DIR / "current.md"
NEXT_PROMPT_FILE = HANDOFF_DIR / "next-prompt.md"
WRITE_LOCK_FILE = HANDOFF_DIR / ".write.lock"
CONTRACT_FILE = Path("docs/shared-agent-contract.md")
VERIFICATION_FILE = Path("docs/verification-playbook.md")

# Canonical handoff failure classification. Must stay in sync with the
# enum documented in docs/shared-agent-contract.md ("Start Of Turn Checklist");
# scripts/validate_handoff.py enforces that every label appears here.
HANDOFF_LABELS = (
    "quota",
    "rate_limit",
    "auth",
    "billing",
    "context_limit",
    "overloaded",
    "tool_failure",
    "unknown",
)

PROVIDERS = ("codex", "claude")

INSTALL_FILES = [
    ("handoff_bridge.py", "handoff_bridge.py"),
    ("handoff_control.py", "handoff_control.py"),
    ("handoff_desktop.py", "handoff_desktop.py"),
    ("remote_handoff_server.py", "remote_handoff_server.py"),
    ("remote_handoff_submit.py", "remote_handoff_submit.py"),
    ("README.md", "README.md"),
    ("AGENTS.md", "AGENTS.md"),
    ("CLAUDE.md", "CLAUDE.md"),
    ("docs/shared-agent-contract.md", "docs/shared-agent-contract.md"),
    ("docs/verification-playbook.md", "docs/verification-playbook.md"),
    ("docs/preflight-setup-guide.md", "docs/preflight-setup-guide.md"),
    ("docs/agent-targeting-protocol.md", "docs/agent-targeting-protocol.md"),
    ("docs/mobile-app-remote-guide.md", "docs/mobile-app-remote-guide.md"),
    ("docs/index.md", "docs/index.md"),
    ("docs/architecture.md", "docs/architecture.md"),
    ("docs/cli-reference.md", "docs/cli-reference.md"),
    ("docs/workflow-guide.md", "docs/workflow-guide.md"),
    ("docs/ko-operator-guide.md", "docs/ko-operator-guide.md"),
    ("docs/platform-setup.md", "docs/platform-setup.md"),
    ("docs/security-model.md", "docs/security-model.md"),
    ("docs/release-notes.md", "docs/release-notes.md"),
    ("docs/research.md", "docs/research.md"),
    ("docs/quality-gates.md", "docs/quality-gates.md"),
    ("schemas/handoff-summary.schema.json", "schemas/handoff-summary.schema.json"),
    ("scripts/handoff_hook.py", "scripts/handoff_hook.py"),
    ("scripts/validate_handoff.py", "scripts/validate_handoff.py"),
    ("scripts/package_platforms.py", "scripts/package_platforms.py"),
    ("scripts/scan_secrets.py", "scripts/scan_secrets.py"),
    ("scripts/check_branch_name.py", "scripts/check_branch_name.py"),
    ("scripts/install_git_hooks.sh", "scripts/install_git_hooks.sh"),
    ("tests/__init__.py", "tests/__init__.py"),
    ("tests/test_handoff_bridge.py", "tests/test_handoff_bridge.py"),
    (".githooks/pre-commit", ".githooks/pre-commit"),
    (".githooks/pre-push", ".githooks/pre-push"),
    ("examples/claude-settings.handoff.json", "examples/claude-settings.handoff.json"),
    ("examples/codex-hooks.handoff.json", "examples/codex-hooks.handoff.json"),
    ("launchers/macos/handoff-bridge.command", "launchers/macos/handoff-bridge.command"),
    ("launchers/macos/install.sh", "launchers/macos/install.sh"),
    ("launchers/windows/handoff-bridge.cmd", "launchers/windows/handoff-bridge.cmd"),
    ("launchers/windows/handoff-bridge.ps1", "launchers/windows/handoff-bridge.ps1"),
    ("launchers/windows/install.ps1", "launchers/windows/install.ps1"),
    (".handoff/.gitignore", ".handoff/.gitignore"),
    (".handoff/task-template.md", ".handoff/task-template.md"),
]

ERROR_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rate_limit", re.compile(r"\b(429|rate limit|too many requests|usage limit)\b", re.I)),
    ("quota", re.compile(r"\b(quota|token limit|tokens exhausted|insufficient quota)\b", re.I)),
    ("billing", re.compile(r"\b(billing|payment required|spend limit)\b", re.I)),
    ("auth", re.compile(r"\b(not logged in|authentication_failed|unauthorized|forbidden)\b", re.I)),
    ("context_limit", re.compile(r"\b(context window|context length|maximum context|max_output_tokens)\b", re.I)),
    ("overloaded", re.compile(r"\b(overloaded|server overloaded|temporarily unavailable)\b", re.I)),
    (
        "tool_failure",
        re.compile(
            r"\b(tool_failure|tool execution failed|command not found|permission denied|no such file or directory)\b",
            re.I,
        ),
    ),
]

assert set(label for label, _ in ERROR_PATTERNS) <= set(HANDOFF_LABELS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_handoff_dir() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")


def read_workspace_or_bridge(rel_path: str, default: str = "") -> str:
    workspace_path = Path(rel_path)
    if workspace_path.exists():
        return workspace_path.read_text(encoding="utf-8")
    bridge_path = BRIDGE_ROOT / rel_path
    if bridge_path.exists():
        return bridge_path.read_text(encoding="utf-8")
    return default


class WriteLock:
    """Cross-process advisory lock for shared handoff files.

    Uses exclusive file creation (portable across macOS/Linux/Windows) rather
    than fcntl/msvcrt so a single implementation covers every launcher path,
    including the HTTP remote server where each task runs as its own
    subprocess rather than a thread the interpreter can lock in-process.
    """

    def __init__(self, path: Path = WRITE_LOCK_FILE, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "WriteLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"timed out waiting for handoff write lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` without ever leaving a partial file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_json(path: Path, data: dict[str, Any]) -> None:
    with WriteLock():
        atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "cwd": str(Path.cwd()),
            "task": "",
            "primary_provider": "codex",
            "last_provider": None,
            "status": "new",
            "sessions": {"codex": None, "claude": None},
            "history": [],
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(STATE_FILE, state)


def short_run(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timed out"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def resolve_workspace(path: str, create: bool = False) -> Path:
    workspace = Path(path).expanduser().resolve()
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    if not workspace.exists():
        raise SystemExit(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise SystemExit(f"workspace is not a directory: {workspace}")
    return workspace


def chdir_workspace(path: str, create: bool = False) -> Path:
    workspace = resolve_workspace(path, create=create)
    os.chdir(workspace)
    return workspace


def install_standard_files(force: bool = False) -> list[tuple[str, str]]:
    ensure_handoff_dir()
    results = []
    for source_rel, target_rel in INSTALL_FILES:
        source = BRIDGE_ROOT / source_rel
        target = Path(target_rel)
        if not source.exists():
            results.append((target_rel, "missing-source"))
            continue
        if target.exists() and not force:
            results.append((target_rel, "skipped"))
            continue
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        results.append((target_rel, "updated" if existed else "installed"))
    if not CURRENT_FILE.exists():
        CURRENT_FILE.write_text(
            f"""# Handoff Packet

## User Task

No task has been initialized yet.

## Current State

- Status: installed.
- Primary provider: codex.
- Fallback provider: claude.

## Active Work Target

- Provider: Either.
- Model: app-selected default.
- Account/App: CLI bridge or mobile remote.
- Workspace: {Path.cwd()}.
- Instruction type: setup.

## Handoff Rules

- Follow `docs/preflight-setup-guide.md` before first remote use.
- Follow `docs/agent-targeting-protocol.md` for every task change or handoff.
- Follow `docs/shared-agent-contract.md`.
- Verify with `docs/verification-playbook.md`.
- Read this file before continuing.
- Inspect the workspace and git status before editing.
- Keep raw provider logs under `.handoff/runs/`.
- Update this file before stopping so the next CLI can continue naturally.

## Latest Summary

Run `python3 handoff_bridge.py init "<task>"` or use
`python3 handoff_control.py` to create a real task packet.
""",
            encoding="utf-8",
        )
        results.append((str(CURRENT_FILE), "installed"))
    return results


def install(args: argparse.Namespace) -> int:
    results = install_standard_files(force=args.force)
    print(f"Installed handoff support files in {Path.cwd()}")
    for target, status_text in results:
        print(f"- {status_text}: {target}")
    return 0


def diagnose(_: argparse.Namespace) -> int:
    checks = []
    for provider in PROVIDERS:
        path = shutil.which(provider)
        version_args = [provider, "--version"]
        code, out, err = short_run(version_args)
        checks.append(
            {
                "provider": provider,
                "path": path,
                "version_exit": code,
                "version": out or err,
            }
        )

    codex_auth = short_run(["codex", "login", "status"])
    claude_auth = short_run(["claude", "auth", "status", "--text"])

    print(f"Handoff bridge diagnostics (agent-handoff-bridge {BRIDGE_VERSION})")
    print(f"- cwd: {Path.cwd()}")
    for item in checks:
        print(f"- {item['provider']}: {item['path'] or 'missing'}")
        if item["version"]:
            print(f"  version: {item['version']}")
    print(f"- codex auth: exit {codex_auth[0]} | {(codex_auth[1] or codex_auth[2])}")
    print(f"- claude auth: exit {claude_auth[0]} | {(claude_auth[1] or claude_auth[2])}")
    return 0


def init_handoff(args: argparse.Namespace) -> int:
    if not args.no_install:
        install_standard_files(force=False)
    ensure_handoff_dir()
    task = args.task
    state = {
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "cwd": str(Path.cwd()),
        "task": task,
        "primary_provider": args.primary,
        "active_provider": args.primary,
        "active_model": args.target_model,
        "instruction_type": args.instruction_type,
        "last_provider": None,
        "status": "ready",
        "sessions": {"codex": None, "claude": None},
        "history": [],
    }
    save_state(state)

    CURRENT_FILE.write_text(
        f"""# Handoff Packet

## User Task

{task}

## Current State

- Status: ready.
- Primary provider: {args.primary}.
- Fallback provider: {other_provider(args.primary)}.

## Active Work Target

- Provider: {args.primary}.
- Model: {args.target_model}.
- Account/App: CLI bridge.
- Workspace: {Path.cwd()}.
- Instruction type: {args.instruction_type}.

## Handoff Rules

- Follow `docs/preflight-setup-guide.md` before first remote use.
- Follow `docs/agent-targeting-protocol.md` for every task change or handoff.
- Follow `docs/shared-agent-contract.md`.
- Verify with `docs/verification-playbook.md`.
- Read this file before continuing.
- Inspect the workspace and git status before editing.
- Keep raw provider logs under `.handoff/runs/`.
- Update this file before stopping so the next CLI can continue naturally.

## Latest Summary

No agent run has been recorded yet.
""",
        encoding="utf-8",
    )
    print(f"Initialized {CURRENT_FILE}")
    print(f"Initialized {STATE_FILE}")
    return 0


def other_provider(provider: str) -> str:
    return "claude" if provider == "codex" else "codex"


def git_snapshot() -> str:
    status = short_run(["git", "status", "--short"])
    diff_stat = short_run(["git", "diff", "--stat"])
    parts = []
    parts.append("### git status --short")
    parts.append(status[1] or status[2] or "(clean)")
    parts.append("### git diff --stat")
    parts.append(diff_stat[1] or diff_stat[2] or "(no diff)")
    return "\n".join(parts)


def build_prompt(provider: str, state: dict[str, Any], user_prompt: str, reason: str | None = None) -> str:
    task = state.get("task") or user_prompt or "Continue the current handoff task."
    active_model = state.get("active_model") or "app-selected default"
    instruction_type = state.get("instruction_type") or "continue"
    targeting = read_workspace_or_bridge("docs/agent-targeting-protocol.md", "(no docs/agent-targeting-protocol.md yet)")
    contract = read_workspace_or_bridge(str(CONTRACT_FILE), "(no docs/shared-agent-contract.md yet)")
    verification = read_workspace_or_bridge(str(VERIFICATION_FILE), "(no docs/verification-playbook.md yet)")
    current = read_text(CURRENT_FILE, "(no .handoff/current.md yet)")
    reason_block = f"\n## Handoff Reason\n\n{reason}\n" if reason else ""
    return f"""You are {provider} continuing a shared CLI handoff task.

## Task

{task}

## User Prompt For This Turn

{user_prompt or "Continue from the shared handoff packet and current workspace."}
{reason_block}
## Required Behavior

- Treat the active work target as Provider `{provider}` and Model `{active_model}` unless
  the user's latest instruction says otherwise.
- If the active model is unknown or app-selected, record the visible model if
  this surface exposes it.
- Follow `docs/agent-targeting-protocol.md` when task scope or provider changes.
- Follow `docs/shared-agent-contract.md`.
- Use `docs/verification-playbook.md` for checks and reporting.
- Read and respect `.handoff/current.md`.
- Inspect the current workspace and git status before editing.
- Continue from the files on disk rather than assuming the prior transcript is complete.
- Keep changes narrowly scoped to the task.
- Before stopping, update `.handoff/current.md` with changed files, checks run,
  remaining work, and any blocker.

## Active Work Target

- Provider: {provider}
- Model: {active_model}
- Account/App: CLI bridge
- Workspace: {Path.cwd()}
- Instruction type: {instruction_type}

## Agent Targeting Protocol

{targeting}

## Shared Agent Contract

{contract}

## Verification Playbook

{verification}

## Shared Handoff Packet

{current}

## Workspace Snapshot

{git_snapshot()}
"""


def provider_command(provider: str, state: dict[str, Any], model: str | None = None) -> list[str]:
    sessions = state.get("sessions", {})
    session_id = sessions.get(provider)
    if provider == "codex":
        if session_id:
            command = [
                "codex",
                "exec",
                "resume",
                "--json",
                "-c",
                'sandbox_mode="workspace-write"',
            ]
            if model:
                command.extend(["--model", model])
            command.extend([session_id, "-"])
            return command
        command = ["codex", "exec", "--json", "--sandbox", "workspace-write"]
        if model:
            command.extend(["--model", model])
        command.append("-")
        return command

    if session_id:
        command = [
            "claude",
            "--resume",
            session_id,
            "-p",
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "auto",
        ]
        if model:
            command.extend(["--model", model])
        return command
    command = [
        "claude",
        "-p",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "auto",
    ]
    if model:
        command.extend(["--model", model])
    return command


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def summarize_codex(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"provider": "codex", "session_id": None, "usage": None, "final_text": "", "errors": []}
    agent_messages = []
    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started":
            summary["session_id"] = event.get("thread_id")
        elif event_type == "turn.completed":
            summary["usage"] = event.get("usage")
        elif event_type in {"turn.failed", "error"}:
            summary["errors"].append(event)
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if text:
                agent_messages.append(text)
    if agent_messages:
        summary["final_text"] = agent_messages[-1]
    return summary


def summarize_claude(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "provider": "claude",
        "session_id": None,
        "usage": None,
        "cost_usd": None,
        "final_text": "",
        "errors": [],
    }
    for event in events:
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            summary["session_id"] = event.get("session_id") or event.get("data", {}).get("session_id")
        elif event_type == "result":
            summary["session_id"] = event.get("session_id") or summary["session_id"]
            summary["usage"] = event.get("usage")
            summary["cost_usd"] = event.get("total_cost_usd")
            summary["final_text"] = event.get("result") or summary["final_text"]
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                summary["errors"].append(event)
        elif event_type == "error":
            summary["errors"].append(event)
    return summary


def classify_handoff(exit_code: int, stdout: str, stderr: str, parsed: dict[str, Any]) -> tuple[bool, str]:
    """Classify whether the run needs a handoff.

    The reason string always starts with one of `HANDOFF_LABELS` (or `none`
    when no handoff is needed) so downstream tooling and `.handoff/current.md`
    stay in the vocabulary defined by docs/shared-agent-contract.md.
    """
    combined = "\n".join([stdout, stderr])
    if parsed.get("errors"):
        errors_text = json.dumps(parsed["errors"], ensure_ascii=False)
        for label, pattern in ERROR_PATTERNS:
            if pattern.search(combined) or pattern.search(errors_text):
                return True, f"{label}: provider emitted a machine-readable error event"
        return True, "unknown: provider emitted a machine-readable error event"
    for label, pattern in ERROR_PATTERNS:
        if pattern.search(combined):
            return True, f"{label}: matched {label} signal"
    if exit_code == 127:
        return True, "tool_failure: provider command not found"
    if exit_code != 0:
        return True, f"tool_failure: provider exited with code {exit_code}"
    return False, "none: no handoff signal detected"


def excerpt(text: str, max_chars: int = 1200) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n... [truncated]"


def append_current(record: dict[str, Any]) -> None:
    ensure_handoff_dir()
    block = [
        "",
        f"## Run {record['started_at']}",
        "",
        f"- Provider: {record['provider']}",
        f"- Model: {record.get('model') or 'app-selected default'}",
        f"- Instruction type: {record.get('instruction_type') or 'continue'}",
        f"- Exit code: {record['exit_code']}",
        f"- Handoff needed: {record['handoff_needed']}",
        f"- Reason: {record['reason']}",
    ]
    if record.get("session_id"):
        block.append(f"- Session ID: {record['session_id']}")
    if record.get("usage"):
        block.append(f"- Usage: `{json.dumps(record['usage'], ensure_ascii=False)}`")
    if record.get("cost_usd") is not None:
        block.append(f"- Cost USD: {record['cost_usd']}")
    final_text = record.get("final_text")
    if final_text:
        block.extend(["", "### Final Output Excerpt", "", excerpt(final_text)])
    addition = "\n".join(block) + "\n"
    with WriteLock():
        CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = CURRENT_FILE.read_text(encoding="utf-8") if CURRENT_FILE.exists() else ""
        atomic_write_text(CURRENT_FILE, existing + addition)


def choose_auto_provider(state: dict[str, Any]) -> str:
    if state.get("status") == "handoff_needed" and state.get("last_provider"):
        return other_provider(state["last_provider"])
    primary = state.get("primary_provider") or "codex"
    if shutil.which(primary):
        return primary
    for provider in PROVIDERS:
        if shutil.which(provider):
            return provider
    return primary


def command_to_display(command: list[str]) -> str:
    return shlex.join(command)


def model_override_arg(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.strip().lower()
    if normalized in {"app-selected default", "provider default", "default", "unknown"}:
        return None
    return model


def run_provider(provider: str, args: argparse.Namespace, state: dict[str, Any], reason: str | None = None) -> int:
    ensure_handoff_dir()
    if args.prompt_file:
        user_prompt = read_text(Path(args.prompt_file))
    else:
        user_prompt = args.prompt or ""
    if args.model:
        state["active_model"] = args.model
    state["active_provider"] = provider
    state["instruction_type"] = args.instruction_type
    prompt = build_prompt(provider, state, user_prompt, reason)
    command = provider_command(provider, state, model_override_arg(args.model))

    if not args.execute:
        NEXT_PROMPT_FILE.write_text(prompt, encoding="utf-8")
        print("Preview mode: no provider was invoked and no tokens were spent.")
        print(f"Provider: {provider}")
        print(f"Command: {command_to_display(command)}")
        print(f"Prompt written to: {NEXT_PROMPT_FILE}")
        print("Add --execute to run it.")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{provider}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started_at = utc_now()
    print(f"Running {provider}; logs will be written to {run_dir}")
    try:
        process = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=Path.cwd(),
            timeout=None if args.timeout_seconds == 0 else args.timeout_seconds,
            check=False,
        )
        exit_code = process.returncode
        stdout = process.stdout
        stderr = process.stderr
    except FileNotFoundError:
        exit_code = 127
        stdout = ""
        stderr = f"{provider} command not found"
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or "provider timed out"

    (run_dir / "stdout.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    events = parse_jsonl(stdout)
    parsed = summarize_codex(events) if provider == "codex" else summarize_claude(events)
    handoff_needed, handoff_reason = classify_handoff(exit_code, stdout, stderr, parsed)

    session_id = parsed.get("session_id")
    if session_id:
        state.setdefault("sessions", {})[provider] = session_id

    record = {
        "started_at": started_at,
        "provider": provider,
        "model": state.get("active_model") or "app-selected default",
        "instruction_type": state.get("instruction_type") or "continue",
        "exit_code": exit_code,
        "session_id": session_id,
        "usage": parsed.get("usage"),
        "cost_usd": parsed.get("cost_usd"),
        "final_text": parsed.get("final_text") or excerpt(stderr),
        "handoff_needed": handoff_needed,
        "reason": handoff_reason,
        "run_dir": str(run_dir),
    }
    state["last_provider"] = provider
    state["status"] = "handoff_needed" if handoff_needed else "completed_or_waiting"
    state.setdefault("history", []).append(record)
    save_state(state)
    append_current(record)

    print(f"{provider} exit code: {exit_code}")
    print(f"handoff needed: {handoff_needed} ({handoff_reason})")

    if handoff_needed:
        fallback = other_provider(provider)
        next_prompt = build_prompt(fallback, state, "Continue after provider handoff.", handoff_reason)
        NEXT_PROMPT_FILE.write_text(next_prompt, encoding="utf-8")
        print(f"Next prompt written to: {NEXT_PROMPT_FILE}")
        if args.auto_fallback:
            print(f"Auto-fallback enabled; switching to {fallback}.")
            next_args = argparse.Namespace(
                prompt="Continue after provider handoff.",
                prompt_file=None,
                execute=True,
                auto_fallback=False,
                timeout_seconds=args.timeout_seconds,
                model=None,
                instruction_type="handoff",
            )
            return run_provider(fallback, next_args, state, handoff_reason)

    return exit_code


def run_command(args: argparse.Namespace) -> int:
    state = load_state()
    if not STATE_FILE.exists():
        state["task"] = args.prompt or "Ad-hoc handoff task"
        state["primary_provider"] = "codex"
        state["status"] = "ready"
        save_state(state)
    provider = choose_auto_provider(state) if args.provider == "auto" else args.provider
    return run_provider(provider, args, state)


def status(_: argparse.Namespace) -> int:
    state = load_state()
    print(json.dumps(state, indent=2, ensure_ascii=False))
    if CURRENT_FILE.exists():
        print(f"\nCurrent packet: {CURRENT_FILE}")
    return 0


def check(_: argparse.Namespace) -> int:
    validator = BRIDGE_ROOT / "scripts/validate_handoff.py"
    return subprocess.run([sys.executable, str(validator), "--root", str(Path.cwd())], check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge Claude Code CLI and Codex CLI.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"agent-handoff-bridge {BRIDGE_VERSION}",
    )
    parser.add_argument(
        "-W",
        "--workspace",
        default=".",
        help="Workspace folder where handoff files and provider commands should run.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_diag = sub.add_parser("diagnose", help="Check local CLI availability and auth state.")
    p_diag.set_defaults(func=diagnose)

    p_install = sub.add_parser("install", help="Install shared handoff support files into the workspace.")
    p_install.add_argument("--force", action="store_true", help="Overwrite existing support files.")
    p_install.set_defaults(func=install)

    p_init = sub.add_parser("init", help="Create a shared handoff packet.")
    p_init.add_argument("task", help="Task description to preserve for handoff.")
    p_init.add_argument("--primary", choices=PROVIDERS, default="codex")
    p_init.add_argument("--target-model", default="app-selected default", help="Model label to record for the active work target.")
    p_init.add_argument("--instruction-type", default="new-task", help="Instruction type to record in the packet.")
    p_init.add_argument("--no-install", action="store_true", help="Do not install support files first.")
    p_init.set_defaults(func=init_handoff)

    p_run = sub.add_parser("run", help="Run or preview a provider invocation.")
    p_run.add_argument("provider", choices=("auto", "codex", "claude"))
    p_run.add_argument("prompt", nargs="?", default="", help="Turn-specific prompt.")
    p_run.add_argument("--prompt-file", help="Read the turn-specific prompt from a file.")
    p_run.add_argument("--execute", action="store_true", help="Actually invoke the provider.")
    p_run.add_argument("--auto-fallback", action="store_true", help="Invoke the other provider on handoff signals.")
    p_run.add_argument("--timeout-seconds", type=int, default=0, help="0 means no timeout.")
    p_run.add_argument("--model", help="Model label to record; exact model strings are also passed to the provider.")
    p_run.add_argument("--instruction-type", default="continue", help="Instruction type for this run.")
    p_run.set_defaults(func=run_command)

    p_status = sub.add_parser("status", help="Print bridge state.")
    p_status.set_defaults(func=status)

    p_check = sub.add_parser("check", help="Validate bridge files without calling model providers.")
    p_check.set_defaults(func=check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chdir_workspace(args.workspace, create=args.command == "install")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
