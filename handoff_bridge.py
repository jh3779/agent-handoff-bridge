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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


BRIDGE_VERSION = "0.2.0"

# Phase 6 (docs/design-system/roadmap.md, SCR-07, resolves CFL-11):
# written while this repo was still private (docs/security-model.md), when
# an anonymous request couldn't list its GitHub Releases.
# check_for_update() below shells out to the user's own local `gh` CLI auth
# instead of standing up new public infrastructure -- the same tool
# docs/release-process.md already assumes for cutting releases, just
# reused for reading them too. The repo is public now, so an anonymous
# API call would also work, but the `gh`-CLI approach hasn't been revisited
# since it still works fine and avoids a second code path.
GITHUB_REPO = "jh3779/agent-handoff-bridge"

BRIDGE_ROOT = Path(__file__).resolve().parent
HANDOFF_DIR = Path(".handoff")
RUNS_DIR = HANDOFF_DIR / "runs"
STATE_FILE = HANDOFF_DIR / "state.json"
CURRENT_FILE = HANDOFF_DIR / "current.md"
NEXT_PROMPT_FILE = HANDOFF_DIR / "next-prompt.md"
WRITE_LOCK_FILE = HANDOFF_DIR / ".write.lock"
# Separate from WRITE_LOCK_FILE on purpose: WRITE_LOCK_FILE is held only for
# the instant of an atomic file write, but a `run` invocation's
# load_state()-...-save_state() cycle spans an entire (possibly many-minute)
# provider subprocess call. Holding WRITE_LOCK_FILE for that whole span would
# block every unrelated quick write elsewhere (append_current(), other
# save_state() calls the run itself makes) past their own short timeout.
# RUN_LOCK_FILE instead serializes only concurrent `run` invocations against
# the same workspace, found necessary because remote_handoff_server.py can
# spawn two overlapping `handoff_bridge.py run` subprocesses against the same
# workspace with nothing else preventing a lost-update race on state.json.
RUN_LOCK_FILE = HANDOFF_DIR / ".run.lock"
RUN_LOCK_TIMEOUT_SECONDS = 3600.0
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

PROVIDERS = ("codex", "claude", "gemini")

# The canonical instruction-type vocabulary. handoff_desktop.py's GUI
# already restricts its Instruction combobox to exactly this set (its own
# INSTRUCTION_TYPES constant -- keep both in sync); this CLI's
# `--instruction-type` previously had no such restriction at all --
# `--instruction-type anything-you-want` was silently accepted and written
# straight into the shared .handoff/current.md/state.json that both
# providers read as their source of truth, no warning.
INSTRUCTION_TYPES = ("new-task", "continue", "handoff", "review", "verify")

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
    ("docs/release-process.md", "docs/release-process.md"),
    ("docs/provider-extensibility.md", "docs/provider-extensibility.md"),
    ("docs/webui-chat-storage.md", "docs/webui-chat-storage.md"),
    ("schemas/handoff-summary.schema.json", "schemas/handoff-summary.schema.json"),
    ("scripts/handoff_hook.py", "scripts/handoff_hook.py"),
    ("scripts/validate_handoff.py", "scripts/validate_handoff.py"),
    ("scripts/package_platforms.py", "scripts/package_platforms.py"),
    ("scripts/build_sidecars.py", "scripts/build_sidecars.py"),
    ("scripts/scan_secrets.py", "scripts/scan_secrets.py"),
    ("scripts/check_branch_name.py", "scripts/check_branch_name.py"),
    ("scripts/install_git_hooks.sh", "scripts/install_git_hooks.sh"),
    ("handoff_webui.py", "handoff_webui.py"),
    ("webui/index.html", "webui/index.html"),
    ("webui/app.css", "webui/app.css"),
    ("webui/app.js", "webui/app.js"),
    ("tests/__init__.py", "tests/__init__.py"),
    ("tests/test_handoff_bridge.py", "tests/test_handoff_bridge.py"),
    ("tests/test_scan_secrets.py", "tests/test_scan_secrets.py"),
    ("tests/test_check_branch_name.py", "tests/test_check_branch_name.py"),
    ("tests/test_handoff_webui.py", "tests/test_handoff_webui.py"),
    ("tests/test_validate_handoff.py", "tests/test_validate_handoff.py"),
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
    (
        "auth",
        re.compile(
            # AuthError/FatalAuthenticationError: Gemini's *documented*
            # error.type/exit-code-41 vocabulary (docs/research-gemini-cli.md)
            # -- added after a review found summarize_gemini()'s error dict
            # (e.g. {"type": "AuthError", "message": "not authenticated"})
            # didn't match any existing pattern and fell through to
            # "unknown" instead of "auth".
            #
            # "set an auth method": what a real installed CLI (v0.54.0)
            # actually emits for this failure, confirmed 2026-08-06 by
            # running the real unauthenticated binary -- its JSON
            # error.type came back as the generic "Error", not
            # "AuthError"/"FatalAuthenticationError", so the type-name
            # match alone does *not* fire for this real, very-likely-common
            # case (a user who hasn't run gemini's auth setup yet). The
            # real message text was "Please set an Auth method in your
            # ...settings.json or specify one of the following environment
            # variables before running: GEMINI_API_KEY,
            # GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA". Matching the
            # fuller imperative phrase "set an auth method" rather than the
            # bare "auth method" matters: this pattern is also checked
            # against a *successful* run's raw combined stdout+stderr
            # (classify_handoff()'s second, unconditional loop, for runs
            # with no structured `errors`) -- a bare "auth method" match
            # was found (review) to misfire on a genuinely successful
            # response that merely discusses auth methods in prose (e.g.
            # "you can configure the auth method in settings.json"),
            # wrongly triggering a handoff/auto-fallback and discarding a
            # good answer. The fuller imperative phrase is Gemini's own
            # distinctive error wording, not something a normal response
            # is likely to say verbatim.
            r"\b(not logged in|authentication_failed|unauthorized|forbidden|AuthError|FatalAuthenticationError|set an auth method)\b",
            re.I,
        ),
    ),
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
            "sessions": {provider: None for provider in PROVIDERS},
            "history": [],
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(STATE_FILE, state)


def decode_timeout_output(value: str | bytes | None) -> str:
    """Normalize `subprocess.TimeoutExpired.stdout`/`.stderr`.

    CPython's `_communicate()` builds the exception's partial output via
    `b''.join(...)` on the timeout path regardless of `text=True`/`encoding`
    on the `Popen`/`run()` call -- only the successful-return path decodes to
    `str`. So even with `text=True`, `exc.stdout`/`exc.stderr` can still be
    `bytes` here.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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
        return 124, decode_timeout_output(exc.stdout), decode_timeout_output(exc.stderr) or "timed out"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def parse_version_tuple(version_str: str) -> "tuple[int, ...] | None":
    """"v0.2.0" or "0.2.0" -> (0, 2, 0). None if unparseable -- a
    malformed or unexpected tag (release process typo, a non-version tag
    someone pushed) must never crash the update check, just make it
    unable to compare, which check_for_update() treats as "no update"."""
    cleaned = version_str.strip().lstrip("vV")
    if not cleaned:
        return None
    parts = cleaned.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def check_for_update() -> "dict[str, str]":
    """Always returns a dict with a `status` field -- never raises, and
    never returns `None`:

    - `{"status": "available", "latest_version", "current_version", "url"}`
      -- a genuinely newer release exists.
    - `{"status": "current", "current_version"}` -- checked successfully,
      nothing newer.
    - `{"status": "unavailable", "current_version"}` -- couldn't tell.
      `gh` missing, unauthenticated, offline, rate-limited, or returning
      something unparseable all collapse into this one status, the same
      fail-silent posture as touch_registry() elsewhere in this project:
      this is a background convenience check
      (docs/design-system/wireframes.html SCR-07 -- "앱 시작 시 백그라운드로
      최신 릴리즈 확인"), not something that should surface a detailed
      error to a user who didn't ask for one.

    CFL-18, resolved as DEC-20 (docs/design-system/flutter-mapping.html#s1c): an earlier
    version returned `None` for *both* "genuinely current" and
    "couldn't check at all," which the caller couldn't tell apart --
    `gh` missing/unauthenticated (real, DEC-19-documented failure paths)
    displayed the same "you're up to date" message as an actual
    successful check, which isn't true. `status` makes the two
    distinguishable.
    """
    exit_code, stdout, _stderr = short_run(
        ["gh", "release", "view", "--repo", GITHUB_REPO, "--json", "tagName,url"]
    )
    unavailable = {"status": "unavailable", "current_version": BRIDGE_VERSION}
    if exit_code != 0:
        return unavailable
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return unavailable
    if not isinstance(data, dict):
        return unavailable
    tag = data.get("tagName")
    url = data.get("url")
    if not isinstance(tag, str) or not isinstance(url, str):
        return unavailable
    latest = parse_version_tuple(tag)
    current = parse_version_tuple(BRIDGE_VERSION)
    if latest is None or current is None:
        return unavailable
    if latest <= current:
        return {"status": "current", "current_version": BRIDGE_VERSION}
    return {
        "status": "available",
        "latest_version": tag.lstrip("vV"),
        "current_version": BRIDGE_VERSION,
        "url": url,
    }


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
    # Gemini CLI has no free auth-status subcommand (docs/research-gemini-
    # cli.md "Practical Limitations") -- the only way to actually check
    # would be a real headless call that spends a token. DEC-18
    # (docs/design-system/flutter-mapping.html#s1c): diagnose() stays free
    # to run as often as a user wants, so this deliberately does not
    # probe -- CLI installation is still checked above (the `checks` loop
    # already covers Gemini via PROVIDERS), only auth state is skipped.

    print(f"Handoff bridge diagnostics (agent-handoff-bridge {BRIDGE_VERSION})")
    print(f"- cwd: {Path.cwd()}")
    for item in checks:
        print(f"- {item['provider']}: {item['path'] or 'missing'}")
        if item["version"]:
            print(f"  version: {item['version']}")
    print(f"- codex auth: exit {codex_auth[0]} | {(codex_auth[1] or codex_auth[2])}")
    print(f"- claude auth: exit {claude_auth[0]} | {(claude_auth[1] or claude_auth[2])}")
    print("- gemini auth: not checked (no free status command exists -- see docs/research-gemini-cli.md)")
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
        "sessions": {provider: None for provider in PROVIDERS},
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
- Fallback provider: {next_provider(args.primary)}.

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


def next_provider(current: str, tried: "set[str] | frozenset[str]" = frozenset()) -> str:
    """Replaces the old `other_provider()` binary toggle (Phase 5,
    docs/provider-extensibility.md "The Current Code Assumes Exactly Two
    Providers") -- with three or more entries in PROVIDERS, "the other
    one" stops being well-defined.

    Walks PROVIDERS in order starting right after `current`, wrapping
    around, and returns the first entry not in `tried` (which does not
    need to include `current` itself -- it's excluded unconditionally).
    Falls back to `current` if every provider has already been tried
    (nothing left to hand off to) rather than raising, since every
    existing call site only ever does a single hop today and has no
    other provider to fall back to in that case either.
    """
    exclude = set(tried) | {current}
    ordered = list(PROVIDERS)
    start = ordered.index(current) if current in ordered else -1
    for offset in range(1, len(ordered) + 1):
        candidate = ordered[(start + offset) % len(ordered)]
        if candidate not in exclude:
            return candidate
    return current


def next_available_provider(current: str, tried: "set[str] | frozenset[str]" = frozenset()) -> str:
    """next_provider(), but also skips any provider whose CLI isn't
    installed (shutil.which()).

    Found via review, real gap only reachable once PROVIDERS grew past
    two entries (Phase 5): both call sites below used to call
    next_provider() directly, which walks PROVIDERS in order with no
    regard for whether the candidate is actually installed. With exactly
    two providers this never mattered -- if "the other one" wasn't
    installed either, there was no third option being skipped past. With
    three, a codex failure could land the single-hop auto-fallback on an
    uninstalled "claude" and never reach an installed "gemini" sitting
    right after it in PROVIDERS order, even though auto-fallback exists
    specifically to reach a *working* provider. Still exactly one hop
    (unchanged token-spend-bounding design, docs/research.md) -- this
    only changes *which* provider that one hop can land on.
    """
    not_installed = {provider for provider in PROVIDERS if not shutil.which(provider)}
    return next_provider(current, tried=set(tried) | not_installed)


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

    if provider == "claude":
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

    # provider == "gemini". Prompt travels via stdin like the other two
    # (subprocess.run(..., input=prompt, ...) in run_provider()) -- no
    # `-p "<text>"` flag needed; docs/research-gemini-cli.md confirmed
    # piped stdin alone auto-triggers Gemini's non-interactive mode.
    command = ["gemini", "--output-format", "json"]
    if session_id:
        # Gemini's JSON response has no session/thread ID field
        # (docs/research-gemini-cli.md "Practical Limitations") -- unlike
        # codex/claude, this bridge can never capture a *specific* prior
        # session to resume by ID. DEC-17
        # (docs/design-system/flutter-mapping.html#s1c): use `--resume
        # latest` instead. `session_id` here is always the literal
        # sentinel "latest" (set by summarize_gemini() only after a clean
        # prior run in this workspace), not a real ID -- it exists purely
        # to answer "has gemini run successfully here before," which is
        # exactly when --resume is safe to add at all.
        command.extend(["--resume", "latest"])
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


def summarize_gemini(stdout: str, stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    """`gemini --output-format json` returns one JSON object at the end
    of the run, not a JSONL event stream like Codex/Claude
    (docs/research-gemini-cli.md "Bottom Line") -- parse_jsonl() doesn't
    apply here, so this parses the JSON directly instead of taking
    pre-parsed `events` like summarize_codex()/summarize_claude() do.

    Confirmed against a real installed binary (v0.54.0, 2026-08-06): a
    *successful* response object is written to stdout, but a
    *fatal-error* response object (auth failure, cancellation,
    max-turns-exceeded, fatal tool error) is written to stderr instead,
    via the CLI's internal `UserFeedback` event path -- the two streams
    are mutually exclusive for this command, never both populated by the
    same run. `stdout` is tried first, so a successful run's parsing is
    unaffected; `stderr` is only consulted when `stdout` has nothing
    parseable, so this can't accidentally prefer stale/interleaved
    stderr log text over a genuine response.

    `session_id` is always the literal sentinel "latest", never a real
    ID (see provider_command()'s Gemini branch for why) -- set only when
    `exit_code` is 0 *and* the response parsed as a JSON object with no
    `error` field, so a workspace that has never had a clean Gemini run
    has nothing marked resumable. Both conditions matter: Gemini's own
    exit-code/JSON-body correlation on failure isn't fully documented
    (docs/research-gemini-cli.md "Practical Limitations" -- two
    overlapping, disagreeing exit-code tables exist in its own docs), so
    a nonzero exit with a superficially clean, `error`-free JSON body is
    a real possibility this project can't rule out from official sources
    alone -- checking exit_code too, not just the JSON body, avoids
    marking a failed run resumable on the strength of the body check
    alone.
    """
    summary: dict[str, Any] = {
        "provider": "gemini",
        "session_id": None,
        "usage": None,
        "cost_usd": None,
        "final_text": "",
        "errors": [],
    }
    data: Any = None
    for candidate in (stdout, stderr):
        try:
            candidate_data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate_data, dict):
            data = candidate_data
            break
    if not isinstance(data, dict):
        return summary
    summary["final_text"] = data.get("response") or ""
    stats = data.get("stats")
    if isinstance(stats, dict):
        summary["usage"] = stats
    error = data.get("error")
    if error:
        summary["errors"].append(error)
    elif exit_code == 0:
        summary["session_id"] = "latest"
    return summary


def classify_handoff(exit_code: int, stdout: str, stderr: str, parsed: dict[str, Any]) -> tuple[bool, str]:
    """Classify whether the run needs a handoff.

    The reason string always starts with one of `HANDOFF_LABELS` (or `none`
    when no handoff is needed) so downstream tooling and `.handoff/current.md`
    stay in the vocabulary defined by docs/shared-agent-contract.md.
    """
    combined = "\n".join([stdout, stderr])
    # The provider's own final answer text (already extracted into
    # parsed["final_text"] by summarize_gemini()/its counterparts) is cut out
    # of the text these patterns scan below. Found via review: on a
    # genuinely successful run, that answer text can legitimately *quote* a
    # phrase like "command not found" or "set an auth method" (e.g.
    # summarizing a bug it just fixed), and scanning it wrongly triggered a
    # handoff/auto-fallback that discarded a good response. This must stay a
    # substring cut, not an exit_code gate: a provider that exits 0 while
    # still emitting a real plain-text error signal outside its answer
    # (verified real case -- see test_rate_limit_signal_in_stdout, exit_code
    # 0) must still be caught.
    final_text = parsed.get("final_text") or ""
    scan_text = combined.replace(final_text, "") if final_text else combined
    if parsed.get("errors"):
        errors_text = json.dumps(parsed["errors"], ensure_ascii=False)
        for label, pattern in ERROR_PATTERNS:
            if pattern.search(combined) or pattern.search(errors_text):
                return True, f"{label}: provider emitted a machine-readable error event"
        return True, "unknown: provider emitted a machine-readable error event"
    for label, pattern in ERROR_PATTERNS:
        if pattern.search(scan_text):
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
        return next_available_provider(state["last_provider"])
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
        stdout = decode_timeout_output(exc.stdout)
        stderr = decode_timeout_output(exc.stderr) or "provider timed out"

    (run_dir / "stdout.jsonl").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    if provider == "codex":
        parsed = summarize_codex(parse_jsonl(stdout))
    elif provider == "claude":
        parsed = summarize_claude(parse_jsonl(stdout))
    else:
        # single JSON object, not a JSONL stream; may land on stdout
        # (success) or stderr (fatal error) -- see summarize_gemini()
        parsed = summarize_gemini(stdout, stderr, exit_code)
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
        fallback = next_available_provider(provider)
        if args.auto_fallback:
            # Don't build/write the fallback prompt here: the recursive
            # run_provider() call below builds it again from scratch right
            # after state["instruction_type"] is set to "handoff", so a
            # prompt built at this point (still "continue"/etc.) would be
            # stale before it's ever read and cost an extra build_prompt()
            # (4 doc reads + a git_snapshot() subprocess pair) for nothing.
            print(f"Auto-fallback enabled; switching to {fallback}.")
            next_args = argparse.Namespace(
                prompt=user_prompt,
                prompt_file=None,
                execute=True,
                auto_fallback=False,
                timeout_seconds=args.timeout_seconds,
                model=None,
                instruction_type="handoff",
            )
            return run_provider(fallback, next_args, state, handoff_reason)
        # Carry the ORIGINAL user_prompt into the fallback, not a generic
        # placeholder -- the handoff_reason is already conveyed separately
        # via build_prompt()'s reason_block, so replacing the actual
        # request/attachments here just means the fallback provider has no
        # idea what the user actually asked for.
        next_prompt = build_prompt(fallback, state, user_prompt, handoff_reason)
        NEXT_PROMPT_FILE.write_text(next_prompt, encoding="utf-8")
        print(f"Next prompt written to: {NEXT_PROMPT_FILE}")

    return exit_code


def run_command(args: argparse.Namespace) -> int:
    try:
        with WriteLock(RUN_LOCK_FILE, timeout=RUN_LOCK_TIMEOUT_SECONDS):
            state = load_state()
            if not STATE_FILE.exists():
                state["task"] = args.prompt or "Ad-hoc handoff task"
                state["primary_provider"] = "codex"
                state["status"] = "ready"
                save_state(state)
            provider = choose_auto_provider(state) if args.provider == "auto" else args.provider
            return run_provider(provider, args, state)
    except TimeoutError:
        print(
            f"error: another `run` is already in progress for this workspace "
            f"(waited {RUN_LOCK_TIMEOUT_SECONDS:.0f}s for {RUN_LOCK_FILE})",
            file=sys.stderr,
        )
        return 75


def status(_: argparse.Namespace) -> int:
    state = load_state()
    print(json.dumps(state, indent=2, ensure_ascii=False))
    if CURRENT_FILE.exists():
        print(f"\nCurrent packet: {CURRENT_FILE}")
    return 0


def check(_: argparse.Namespace) -> int:
    if getattr(sys, "frozen", False):
        # Phase 7a (DEC-22, docs/research-phase7-framework.md): frozen as
        # the Tauri sidecar agent-handoff-bridge-cli, sys.executable is
        # this binary itself, not a Python interpreter -- passing it
        # scripts/validate_handoff.py's path wouldn't run that script.
        # A sibling PyInstaller sidecar built from validate_handoff.py
        # (agent-handoff-bridge-validate, Tauri places every declared
        # sidecar in the same directory as this one) is invoked directly
        # instead, matching handoff_webui.py's bridge_command_prefix().
        # PureWindowsPath/PurePosixPath, not the host-native Path -- see
        # bridge_command_prefix()'s comment for why.
        validate_name = "agent-handoff-bridge-validate.exe" if sys.platform == "win32" else "agent-handoff-bridge-validate"
        pure_path = PureWindowsPath if sys.platform == "win32" else PurePosixPath
        command = [str(pure_path(sys.executable).parent / validate_name), "--root", str(Path.cwd())]
    else:
        validator = BRIDGE_ROOT / "scripts/validate_handoff.py"
        command = [sys.executable, str(validator), "--root", str(Path.cwd())]
    return subprocess.run(command, check=False).returncode


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
    p_init.add_argument(
        "--instruction-type", choices=INSTRUCTION_TYPES, default="new-task", help="Instruction type to record in the packet."
    )
    p_init.add_argument("--no-install", action="store_true", help="Do not install support files first.")
    p_init.set_defaults(func=init_handoff)

    p_run = sub.add_parser("run", help="Run or preview a provider invocation.")
    p_run.add_argument("provider", choices=("auto",) + PROVIDERS)
    p_run.add_argument("prompt", nargs="?", default="", help="Turn-specific prompt.")
    p_run.add_argument("--prompt-file", help="Read the turn-specific prompt from a file.")
    p_run.add_argument("--execute", action="store_true", help="Actually invoke the provider.")
    p_run.add_argument("--auto-fallback", action="store_true", help="Invoke the other provider on handoff signals.")
    p_run.add_argument("--timeout-seconds", type=int, default=0, help="0 means no timeout.")
    p_run.add_argument("--model", help="Model label to record; exact model strings are also passed to the provider.")
    p_run.add_argument(
        "--instruction-type", choices=INSTRUCTION_TYPES, default="continue", help="Instruction type for this run."
    )
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
