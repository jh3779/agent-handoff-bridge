#!/usr/bin/env python3
"""Bridge Claude Code CLI and Codex CLI through shared handoff files."""

from __future__ import annotations

import argparse
import contextlib
import io
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


BRIDGE_VERSION = "0.5.3"

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
# `.handoff/current.md` is append-only (append_current() never trims it) and
# gets folded into *every* build_prompt() call in full -- on a long-lived,
# actively-used project this file only grows, so every turn's prompt (and
# its latency/cost) grows right along with it, even for a trivial message
# with no need for the older history (real, verified case: this project's
# own current.md alone was ~270KB / roughly 65,000 tokens by v0.4.10,
# before this cap existed). Only the tail actually matters for continuing
# the next turn -- the full log is still preserved on disk untouched for
# anyone who wants to read the whole thing. Matches webui/app.js's
# MAX_PREVIEW_CHARS (20000) -- not derived from it, just the same
# already-established "how much raw text is reasonable to hand a model at
# once" magnitude elsewhere in this project.
MAX_CURRENT_FILE_PROMPT_CHARS = 20_000
# Default (compact) prompt mode: the handoff log is folded in as its newest
# few *entries* -- whole `##`/`###` sections, newest first, up to this many
# characters -- instead of a raw 20,000-character tail. The newest entry
# is where the previous provider recorded Remaining/Blocked/Next, which is
# all a receiving provider actually needs; older history stays on disk.
# `run --full-prompt` restores the pre-compact 20,000-character tail.
COMPACT_CURRENT_PROMPT_CHARS = 5_000
# `.handoff/current.md` is append-only and an agent that follows "read
# .handoff/current.md first" opens the *whole* file (a 280KB log is ~70,000
# tokens), regardless of how little build_prompt() sends. Once the file
# passes the threshold, the oldest entries move to .handoff/archive/ so the
# live file stays near CURRENT_ARCHIVE_KEEP_CHARS.
CURRENT_ARCHIVE_THRESHOLD_CHARS = 60_000
CURRENT_ARCHIVE_KEEP_CHARS = 30_000
ARCHIVE_DIR = HANDOFF_DIR / "archive"
CURRENT_ARCHIVE_POINTER = "<!-- Older entries were moved to .handoff/archive/ -->\n"
EARLIER_HISTORY_OMITTED_NOTE = (
    "...[earlier history omitted -- see `.handoff/current.md` on disk "
    "for the full log]"
)
NEXT_PROMPT_FILE = HANDOFF_DIR / "next-prompt.md"
# User-editable, workspace-local project context that should reach every
# provider regardless of how it's invoked -- folded into every prompt by
# build_prompt() below. Distinct from CURRENT_FILE (the
# handoff *log* -- what changed, what's next) and from the docs/ files
# folded into every prompt (shared, repo-wide contract/protocol text) --
# this is free-form, per-workspace context the user writes once
# ("this project uses X style", "never touch the legacy/ folder") rather
# than something either provider or this bridge generates.
SHARED_CONTEXT_FILE = HANDOFF_DIR / "shared-context.md"
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

PROVIDERS = ("codex", "claude", "agy")

# The canonical instruction-type vocabulary. handoff_desktop.py's GUI
# already restricts its Instruction combobox to exactly this set (its own
# INSTRUCTION_TYPES constant -- keep both in sync); this CLI's
# `--instruction-type` previously had no such restriction at all --
# `--instruction-type anything-you-want` was silently accepted and written
# straight into the shared .handoff/current.md/state.json that both
# providers read as their source of truth, no warning.
INSTRUCTION_TYPES = ("new-task", "continue", "handoff", "review", "verify")

INSTALL_FILES = [
    # scripts/validate_handoff.py's REQUIRED_FILES has required a top-level
    # `.gitignore` since before this list existed, but this list never
    # copied one -- so `handoff_bridge.py check` failed immediately and
    # deterministically in *every* freshly install()ed/init()ed workspace,
    # including every workspace the webui auto-creates. Found via a real
    # end-to-end reproduction (2026-09-04), not by re-reading the list.
    (".gitignore", ".gitignore"),
    ("handoff_bridge.py", "handoff_bridge.py"),
    ("handoff_control.py", "handoff_control.py"),
    ("handoff_desktop.py", "handoff_desktop.py"),
    ("remote_handoff_server.py", "remote_handoff_server.py"),
    ("remote_handoff_submit.py", "remote_handoff_submit.py"),
    ("README.md", "README.md"),
    ("AGENTS.md", "AGENTS.md"),
    (".agents/skills/handoff-status/SKILL.md", ".agents/skills/handoff-status/SKILL.md"),
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
    ("scripts/build_updater_manifest.py", "scripts/build_updater_manifest.py"),
    ("scripts/smoke_sidecar.py", "scripts/smoke_sidecar.py"),
    ("scripts/scan_secrets.py", "scripts/scan_secrets.py"),
    ("scripts/check_branch_name.py", "scripts/check_branch_name.py"),
    ("scripts/install_git_hooks.sh", "scripts/install_git_hooks.sh"),
    ("handoff_webui.py", "handoff_webui.py"),
    ("webui_common.py", "webui_common.py"),
    ("webui_workspace.py", "webui_workspace.py"),
    ("webui_chat_storage.py", "webui_chat_storage.py"),
    ("webui_credentials.py", "webui_credentials.py"),
    ("webui_bridge_run.py", "webui_bridge_run.py"),
    ("webui_stdio.py", "webui_stdio.py"),
    ("run_progress.py", "run_progress.py"),
    ("webui/index.html", "webui/index.html"),
    ("webui/app.css", "webui/app.css"),
    ("webui/app.js", "webui/app.js"),
    ("webui/i18n.js", "webui/i18n.js"),
    ("webui/theme-init.js", "webui/theme-init.js"),
    ("tests/__init__.py", "tests/__init__.py"),
    ("tests/test_handoff_bridge.py", "tests/test_handoff_bridge.py"),
    ("tests/test_handoff_control.py", "tests/test_handoff_control.py"),
    ("tests/test_handoff_desktop.py", "tests/test_handoff_desktop.py"),
    ("tests/test_handoff_hook.py", "tests/test_handoff_hook.py"),
    ("tests/test_remote_handoff_server.py", "tests/test_remote_handoff_server.py"),
    ("tests/test_remote_handoff_submit.py", "tests/test_remote_handoff_submit.py"),
    ("tests/test_scan_secrets.py", "tests/test_scan_secrets.py"),
    ("tests/test_check_branch_name.py", "tests/test_check_branch_name.py"),
    ("tests/test_handoff_webui.py", "tests/test_handoff_webui.py"),
    ("tests/test_webui_stdio.py", "tests/test_webui_stdio.py"),
    ("tests/test_run_progress.py", "tests/test_run_progress.py"),
    ("tests/test_webui_frontend_logic.py", "tests/test_webui_frontend_logic.py"),
    ("tests/test_validate_handoff.py", "tests/test_validate_handoff.py"),
    ("tests/test_build_updater_manifest.py", "tests/test_build_updater_manifest.py"),
    ("tests/test_package_platforms.py", "tests/test_package_platforms.py"),
    ("tests/test_agent_skills.py", "tests/test_agent_skills.py"),
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
            # "authentication required": what `agy` (Antigravity CLI) exits
            # with when a non-interactive run has no cached login
            # (https://antigravity.google/docs/cli/headless). The fuller
            # phrase rather than a bare "auth" word matters: this pattern is
            # also checked against a *successful* run's raw combined
            # stdout+stderr (classify_handoff()'s second loop, for runs with
            # no structured `errors`), and a normal response may merely
            # discuss authentication in prose.
            r"\b(not logged in|authentication_failed|unauthorized|forbidden|authentication required)\b",
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


def tail_for_prompt(text: str, max_chars: int) -> str:
    """Bound how much of an ever-growing file gets folded into a prompt --
    see MAX_CURRENT_FILE_PROMPT_CHARS. Cuts at the next newline after the
    truncation point rather than mid-line, so the excerpt still reads as
    clean markdown starting from a section/line boundary instead of a
    fragment of whatever heading or sentence the raw character cut landed
    inside."""
    if len(text) <= max_chars:
        return text
    tail = text[-max_chars:]
    newline_index = tail.find("\n")
    if newline_index != -1:
        tail = tail[newline_index + 1 :]
    return f"{EARLIER_HISTORY_OMITTED_NOTE}\n\n{tail}"


_LOG_SECTION_HEADING = re.compile(r"^#{2,3} ", re.M)


def split_log_sections(text: str) -> list[str]:
    """Split `.handoff/current.md` text into `##`/`###` sections (any text
    before the first heading is its own leading section). `"".join()` of
    the result is always exactly `text`."""
    if not text:
        return []
    starts = [m.start() for m in _LOG_SECTION_HEADING.finditer(text)]
    bounds = ([0] if not starts or starts[0] != 0 else []) + starts + [len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:])]


def newest_sections(sections: list[str], max_chars: int) -> list[str]:
    """Newest-first walk keeping whole sections while they fit in
    `max_chars`; the newest section is always kept even if it alone is
    over budget."""
    kept: list[str] = []
    total = 0
    for section in reversed(sections):
        if kept and total + len(section) > max_chars:
            break
        kept.append(section)
        total += len(section)
    kept.reverse()
    return kept


def recent_log_for_prompt(text: str, max_chars: int) -> str:
    """Compact-mode counterpart of tail_for_prompt(): keeps the newest whole
    log entries instead of cutting at an arbitrary character offset, so the
    receiving provider sees complete Remaining/Blocked/Next sections. A
    single entry larger than the budget falls back to tail_for_prompt()."""
    if len(text) <= max_chars:
        return text
    sections = split_log_sections(text)
    kept = newest_sections(sections, max_chars)
    body = "".join(kept)
    if len(body) > max_chars:
        return tail_for_prompt(body, max_chars)
    if len(kept) < len(sections):
        return f"{EARLIER_HISTORY_OMITTED_NOTE}\n\n{body}"
    return body


def archive_old_current_entries(state: dict[str, Any]) -> int:
    """Move the oldest `.handoff/current.md` entries to
    `.handoff/archive/current-<UTC timestamp>.md` once the file passes
    CURRENT_ARCHIVE_THRESHOLD_CHARS, keeping about CURRENT_ARCHIVE_KEEP_CHARS
    of the newest entries. Returns how many characters left the live file
    (0 when nothing moved).

    `state["prompt_context_offsets"]` are character offsets into the live
    file (see build_prompt()), so they are shifted by exactly what was
    removed -- otherwise a continuation turn would slice the shortened file
    at a stale position and silently skip new entries. The caller owns
    saving `state`."""
    with WriteLock():
        if not CURRENT_FILE.exists():
            return 0
        text = CURRENT_FILE.read_text(encoding="utf-8")
        if len(text) <= CURRENT_ARCHIVE_THRESHOLD_CHARS:
            return 0
        prefix_len = len(CURRENT_ARCHIVE_POINTER) if text.startswith(CURRENT_ARCHIVE_POINTER) else 0
        body = text[prefix_len:]
        sections = split_log_sections(body)
        kept = newest_sections(sections, CURRENT_ARCHIVE_KEEP_CHARS)
        moved = "".join(sections[: len(sections) - len(kept)])
        if not moved:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = ARCHIVE_DIR / f"current-{stamp}.md"
        counter = 1
        while archive_path.exists():
            archive_path = ARCHIVE_DIR / f"current-{stamp}-{counter}.md"
            counter += 1
        # Archive first: if we crash before the live file is rewritten the
        # entries exist twice, never zero times.
        atomic_write_text(archive_path, moved)
        atomic_write_text(CURRENT_FILE, CURRENT_ARCHIVE_POINTER + "".join(kept))
    shift = prefix_len + len(moved) - len(CURRENT_ARCHIVE_POINTER)
    offsets = state.get("prompt_context_offsets")
    if isinstance(offsets, dict):
        for name, value in list(offsets.items()):
            if isinstance(value, int):
                offsets[name] = max(0, value - shift)
    return shift


def read_workspace_or_bridge(rel_path: str, default: str = "") -> str:
    workspace_path = Path(rel_path)
    if workspace_path.exists():
        return workspace_path.read_text(encoding="utf-8")
    bridge_path = BRIDGE_ROOT / rel_path
    if bridge_path.exists():
        return bridge_path.read_text(encoding="utf-8")
    return default


# On Windows, os.open(O_CREAT | O_EXCL) on a lock file the previous holder is
# still deleting raises PermissionError (EACCES) instead of FileExistsError.
# That window lasts milliseconds, so PermissionError is retried only briefly
# (LOCK_PERMISSION_RETRY_SECONDS of *consecutive* PermissionErrors); a lasting
# one is a real permission problem (e.g. a read-only .handoff) and is raised
# as it is, not absorbed into a lock timeout that can be an hour (.run.lock).
# On POSIX PermissionError is never transient.
TRANSIENT_LOCK_ERRORS: tuple[type[OSError], ...] = (PermissionError,) if os.name == "nt" else ()
LOCK_PERMISSION_RETRY_SECONDS = 2.0


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
        permission_since: float | None = None
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError as exc:
                permission_since = None
                if time.monotonic() > deadline:
                    raise TimeoutError(f"timed out waiting for handoff write lock: {self.path}") from exc
            except TRANSIENT_LOCK_ERRORS:
                now = time.monotonic()
                if permission_since is None:
                    permission_since = now
                if now - permission_since > LOCK_PERMISSION_RETRY_SECONDS or now > deadline:
                    raise
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


def default_state() -> dict[str, Any]:
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


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupted/partially-written state.json (crash mid-write,
        # concurrent process killed) must degrade to "start fresh," the
        # same way a missing file already does, not propagate an uncaught
        # exception -- found via a structure audit that the two peripheral
        # copies of "read JSON state with a default" (handoff_webui.py's
        # read_state_dict(), remote_handoff_server.py's read_json()) both
        # already caught this, while this more central implementation
        # didn't.
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(STATE_FILE, state)


def read_text_lossy(path: Path) -> str:
    """A file's text, decoded as UTF-8 with undecodable bytes replaced; "" if unreadable."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


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


def short_run(args: list[str], timeout: float | None = 10, cwd: "str | Path | None" = None) -> tuple[int, str, str]:
    """Shared subprocess wrapper: UTF-8-safe, normalizes a missing binary
    (FileNotFoundError) to exit 127 and a timeout to exit 124, the same
    way every direct caller of this class of subprocess call across the
    project needs to (found via a structure audit: handoff_desktop.py,
    remote_handoff_server.py, and handoff_webui.py each reimplemented this
    independently, with the FileNotFoundError normalization missing from
    all three copies). Not every subprocess call in the project fits this
    shape, though -- callers that need `input=`/stdin (the actual provider
    CLI invocation in run_provider()) or `shell=True` (the LLM tool loop's
    run_shell) are deliberately NOT migrated to this helper; folding those
    in would either lose behavior or widen this helper's interface for a
    single caller's sake.
    """
    try:
        result = subprocess.run(
            args,
            text=True,
            # encoding/errors: without an explicit encoding, subprocess
            # falls back to locale.getpreferredencoding() for both
            # directions -- on a non-UTF-8-locale Windows machine (e.g.
            # cp949 on Korean Windows) that raises UnicodeEncodeError the
            # instant a git/gh call's output contains a character the
            # locale codec can't represent (an em dash in a commit
            # message is enough). Every git/gh call this project makes
            # can plausibly hit non-ASCII output, and errors="replace"
            # matches decode_timeout_output()'s own established
            # never-crash-on-decode posture below.
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
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


def normalize_path(raw_path: str) -> Path:
    """Turn a raw path string into a normalized absolute Path: expand `~`,
    then resolve (symlinks + relative-to-cwd) -- the one piece of "what
    does a path string actually mean" logic every workspace-path consumer
    in the project needs, shared here so it only has one definition
    (found via a structure audit: handoff_control.py, handoff_webui.py,
    and remote_handoff_server.py each recomputed this same expression).
    What happens next -- raise vs. prompt vs. create, which exception type
    -- still varies legitimately per caller and is deliberately NOT
    unified here.
    """
    return Path(raw_path).expanduser().resolve()


def resolve_workspace(path: str, create: bool = False) -> Path:
    workspace = normalize_path(path)
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


# Claude Code loads project skills from `.claude/skills/<name>/SKILL.md` and does
# NOT read `.agents/skills/` (verified against a real `claude -p` run on
# 2026-10-28: a skill under .agents/skills was reported unavailable, and became
# available once `.claude/skills/<name>` was a symlink to it; the symlink form
# is documented at code.claude.com/docs/en/skills). Codex, Antigravity (agy)
# and OpenCode read `.agents/skills/`. So the bridge's own skills keep ONE
# original under `.agents/skills/` and get a relative symlink under
# `.claude/skills/`.
SHARED_SKILLS_DIR = Path(".agents/skills")
CLAUDE_SKILLS_DIR = Path(".claude/skills")
MANAGED_COPY_MARKER = ".handoff-bridge-managed"
BRIDGE_SKILL_NAMES = tuple(
    sorted({PurePosixPath(target).parts[2] for _, target in INSTALL_FILES if target.startswith(".agents/skills/")})
)


def link_claude_skills(force: bool = False) -> list[tuple[str, str]]:
    """Make `.claude/skills/<name>` a relative symlink to
    `.agents/skills/<name>` for each skill this bridge installs. Never
    touches a skill the user owns: an existing real directory (or a symlink
    that points somewhere else, unless `force`) is left as it is. Where
    symlinks cannot be created (Windows without developer mode), the skill is
    copied instead and the copy is marked as bridge-managed, so `install
    --force` can refresh it without ever touching a directory the user made.
    A plain file holding exactly the link target is what git leaves behind
    when it checks a symlink out without symlink support; that is replaced
    on any `install`."""
    results: list[tuple[str, str]] = []
    for name in BRIDGE_SKILL_NAMES:
        source = SHARED_SKILLS_DIR / name
        link = CLAUDE_SKILLS_DIR / name
        label = str(PurePosixPath(*link.parts))
        if not (source / "SKILL.md").is_file():
            continue
        wanted = os.path.relpath(source.resolve(), link.parent.resolve())
        if os.path.lexists(link):
            if link.is_symlink():
                points_at_source = link.exists() and link.resolve() == source.resolve()
                if points_at_source:
                    results.append((label, "ok"))
                    continue
                if not force:
                    results.append((label, "skipped"))
                    continue
                link.unlink()
            elif link.is_file() and _is_git_symlink_placeholder(link, wanted):
                link.unlink()
            elif link.is_dir() and (link / MANAGED_COPY_MARKER).is_file():
                if not force:
                    results.append((label, "ok"))
                    continue
                shutil.rmtree(link)
            else:
                results.append((label, "skipped"))
                continue
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(wanted, link, target_is_directory=True)
            results.append((label, "linked"))
        except (OSError, NotImplementedError):
            shutil.copytree(source, link)
            (link / MANAGED_COPY_MARKER).write_text(
                f"Copied from {source.as_posix()} by handoff_bridge.py install; "
                "`install --force` refreshes it. Delete this file to keep your own edits.\n",
                encoding="utf-8",
            )
            results.append((label, "copied"))
    return results


def _is_git_symlink_placeholder(path: Path, wanted: str) -> bool:
    """True for the small text file git writes in place of a symlink
    (core.symlinks=false): its whole content is the link target."""
    try:
        if path.stat().st_size > 512:
            return False
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return content.replace("\\", "/") == wanted.replace("\\", "/")


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
    results.extend(link_claude_skills(force=force))
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
    # `agy` has no auth-status subcommand (its subcommands are agent(s),
    # changelog, install, mcp, models, plugin(s), remote-control, update) --
    # the only way to actually check would be a real headless call that
    # spends quota. DEC-18 (docs/design-system/flutter-mapping.html#s1c):
    # diagnose() stays free to run as often as a user wants, so this
    # deliberately does not probe -- CLI installation is still checked above
    # (the `checks` loop already covers agy via PROVIDERS), only auth state
    # is skipped.

    print(f"Handoff bridge diagnostics (agent-handoff-bridge {BRIDGE_VERSION})")
    print(f"- cwd: {Path.cwd()}")
    for item in checks:
        print(f"- {item['provider']}: {item['path'] or 'missing'}")
        if item["version"]:
            print(f"  version: {item['version']}")
    print(f"- codex auth: exit {codex_auth[0]} | {(codex_auth[1] or codex_auth[2])}")
    print(f"- claude auth: exit {claude_auth[0]} | {(claude_auth[1] or claude_auth[2])}")
    print("- agy auth: not checked (no free status command exists; run `agy` once interactively to sign in)")
    if agy_allow_writes_enabled():
        print(f"- agy permissions: ALL tool calls auto-approved ({AGY_ALLOW_WRITES_ENV} is set)")
    else:
        print("- agy permissions: file writes and commands are denied unless allowed in agy's own settings")
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
    uninstalled "claude" and never reach an installed third provider sitting
    right after it in PROVIDERS order, even though auto-fallback exists
    specifically to reach a *working* provider. Still exactly one hop
    (unchanged token-spend-bounding design, docs/research.md) -- this
    only changes *which* provider that one hop can land on.
    """
    not_installed = {provider for provider in PROVIDERS if not shutil.which(provider)}
    return next_provider(current, tried=set(tried) | not_installed)


def git_snapshot() -> str:
    """Folded into every build_prompt() call, so keep this cheap and short
    -- it's pure overhead added to every single turn, not itself something
    the user asked for.

    Outside a git repo (the default for every workspace this bridge
    auto-creates -- see provider_command()'s `--skip-git-repo-check`),
    `git status --short` fails with a short, single-line "fatal: not a
    git repository" message, but `git diff --stat` fails very differently:
    with no repo to diff against, git falls back to treating this as a
    `--no-index` two-path comparison, finds no paths were given, and dumps
    its own ~7KB `--no-index` usage/help text to stderr instead (real,
    confirmed exit code 129) -- previously included here verbatim on every
    single prompt in every non-git workspace, pure noise with no signal
    value. Once `git status` has already confirmed there's no repo, skip
    running `git diff` at all rather than trying to keep discovering (and
    special-casing) more of git's own inconsistent non-repo failure shapes.
    """
    status_exit, status_out, status_err = short_run(["git", "status", "--short"])
    parts = ["### git status --short", status_out or status_err or "(clean)"]
    parts.append("### git diff --stat")
    if status_exit != 0:
        parts.append("(not a git repository)")
        return "\n".join(parts)
    diff_exit, diff_out, _diff_err = short_run(["git", "diff", "--stat"])
    parts.append((diff_out or "(no diff)") if diff_exit == 0 else "(unavailable)")
    return "\n".join(parts)


# See build_prompt()'s docstring for the real, observed failure mode this
# addresses: a provider re-reading content already inlined in its own
# prompt, and/or matching this project's "handoff" terminology against an
# unrelated, user-configured global skill (a same-named but functionally
# different session-continuity or self-evaluation skill).
SELF_CONTAINED_NOTICE = """This prompt is self-contained for this turn -- the protocol docs, workspace
state, and git status referenced below are already included (or were already given to you
earlier in this same resumed session). Do not spend time re-opening those files or re-running the
same git commands unless you have a concrete, stated reason to think they changed since this prompt
was generated. This tool (agent-handoff-bridge) manages its own state under `.handoff/` -- unrelated
to any other project-continuation, checkpoint, or self-evaluation skill you may separately have
configured; do not invoke those for this task."""

# Same real reproduction as SELF_CONTAINED_NOTICE, a second angle on it: even
# after that notice stopped the redundant re-reads and the unrelated-skill
# collision it targeted, a follow-up real run still ballooned a trivial
# message into a multi-minute session -- this time by finding genuine,
# unrelated bugs (a missing .gitignore, a stale run lock) and pulling in yet
# another global skill (a generic TDD workflow) to fix them, none of which
# the turn's actual prompt asked for. This notice narrows scope without
# forbidding investigation a *real* task genuinely needs (reading files,
# running the relevant tests, checking git status) -- it only tells the
# model not to go looking for extra work uninvited.
#
# A further real re-run against the first version of this notice showed
# partial but incomplete effect: the model stopped chasing unrelated global
# skills, but still fixed a real bug (the missing .gitignore above) it
# happened to hit while merely trying to verify its own no-op turn -- its
# own reasoning treated "the blocker in front of me" as in-scope even
# though nothing about it was actually asked for. The explicit
# "report it, don't fix it" clause below targets that exact loophole.
SCOPE_DISCIPLINE_NOTICE = """Scope discipline: do what "User Prompt For This Turn" below actually asks --
no more, no less. If completing it genuinely requires investigation (reading files, running the
relevant tests or commands, checking git status), that is expected. But unless the prompt itself
asks you to find or fix something, do not proactively audit for unrelated bugs, do not run the
full project test suite or a broad review, and do not invoke a skill, subagent, or larger workflow
beyond what this specific turn needs. This includes a blocker you happen to run into while merely
investigating or verifying (a failing check, a missing file, an unrelated bug) -- report it briefly
in your summary and stop there; do not fix it unprompted, even if fixing it looks small."""


# Compact stand-in for the three docs/ files (docs/agent-targeting-protocol.md,
# docs/shared-agent-contract.md, docs/verification-playbook.md -- ~9,400
# characters, about half of it mobile-app example blocks) that a cold-start
# or handoff prompt used to inline in full. Keep it in sync with the rules in
# shared-agent-contract.md; the failure labels are checked against
# HANDOFF_LABELS by tests. `run --full-prompt` still inlines the real files
# (a workspace may have customised them).
PROTOCOL_SUMMARY = """- Priority when instructions conflict: the user's latest instruction, then safety and
  non-destructive handling, then this project's shared contract, then `.handoff/current.md`,
  then project conventions. Record any conflict in `.handoff/current.md`.
- The files on disk are the source of truth, not the previous provider's hidden transcript.
  Preserve other people's unrelated edits; keep changes scoped to the task; no broad refactors,
  new dependencies, or formatting churn unless the task needs them.
- Verify with the narrowest relevant check first (`python3 -m py_compile` for changed Python,
  `python3 -m json.tool` for JSON, the project's own test/build commands for app code). If a check
  cannot run, record `not run` with the reason. Never commit secrets or auth files.
- Failure labels (use exactly these when recording why a run stopped): quota, rate_limit, auth,
  billing, context_limit, overloaded, tool_failure, unknown.
- Hand off to another provider when the current one hits a quota, rate-limit, billing, auth,
  context, or max-output failure, when tools are repeatedly blocked, or when the user asks.
- @@FINISH@@ Target (provider/model/workspace), Changed, Verified, Remaining, Blocked, Next. If
  nothing changed, say so. The task is done only when the requested work exists, checks passed or
  are marked not applicable, and the other provider could continue from files alone.
- The full rules live in `docs/shared-agent-contract.md`, `docs/agent-targeting-protocol.md`, and
  `docs/verification-playbook.md`; this summary is complete for ordinary tasks."""

# The one sentence of PROTOCOL_SUMMARY that differs per provider: how the final
# summary reaches .handoff/current.md. agy cannot write it (see
# PROVIDER_RUNTIME_NOTES), so it must not be told to.
_SUMMARY_FINISH_DEFAULT = "Before stopping, append a concise entry to `.handoff/current.md` in this shape:"
_SUMMARY_FINISH_BY_PROVIDER = {
    "agy": (
        "Before stopping, end your reply with a concise entry in this shape (do not edit"
        " `.handoff/current.md` yourself; the bridge records your reply there):"
    ),
}


def protocol_summary_for(provider: str) -> str:
    finish = _SUMMARY_FINISH_BY_PROVIDER.get(provider, _SUMMARY_FINISH_DEFAULT)
    return PROTOCOL_SUMMARY.replace("@@FINISH@@", finish)


# Appended last in a full-mode prompt, after the real docs, because those docs
# tell every agent to read and update `.handoff/current.md` itself and the last
# instruction wins: agy would otherwise spend its turn on a denied write.
PROVIDER_FINAL_OVERRIDES = {
    "agy": (
        "Override for this provider: where the protocol documents above tell you to update, append to,\n"
        "or write `.handoff/current.md`, do not do that -- put the same content at the end of your reply;\n"
        "the bridge records it. Reading the log (included above) is fine."
    ),
}


def provider_final_override_block(provider: str, full: bool) -> str:
    note = PROVIDER_FINAL_OVERRIDES.get(provider)
    return f"\n## Final Instruction ({provider})\n\n{note}\n" if note and full else ""


# Extra, provider-specific instructions folded into the prompt. agy runs
# headless, where a tool call that needs a permission prompt is denied
# silently; without this note a first-turn "also update .handoff/current.md"
# made it try (and fail) to write, then end the turn with an empty reply
# (observed against agy 1.4.8, 2026-10-28) -- which looks like a failed run.
PROVIDER_RUNTIME_NOTES = {
    "agy": (
        "This provider runs headless: any file write or shell command that is not pre-approved in\n"
        "its settings is denied without a prompt. Do not retry a denied action and never end your\n"
        "turn without a text reply -- say what you did or would have done."
    ),
}

# The standard "update .handoff/current.md before stopping" instruction needs
# a file write. For agy that write is denied unless the user allowed it, and
# even with a note saying so the model still spent its turn trying (agy 1.4.8,
# 2026-10-28) and finished with an empty reply. The bridge appends every
# provider's final reply to current.md itself (append_current()), so agy is
# asked to put the same summary in its reply instead.
BEFORE_STOPPING_DEFAULT = (
    "- Before stopping, update `.handoff/current.md` with changed files, checks run,\n"
    "  remaining work, and any blocker."
)
BEFORE_STOPPING_BY_PROVIDER = {
    "agy": (
        "- Before stopping, end your reply with a concise summary (changed files, checks run,\n"
        "  remaining work, any blocker). The bridge records your final reply in\n"
        "  `.handoff/current.md` -- do not edit that file yourself."
    ),
}


# Opt-in to agy writing files and running commands in headless mode. Off by
# default (see provider_command()); read from the environment so every surface
# that ends up running `handoff_bridge.py` (CLI, chat, Web UI, desktop app,
# remote server) honours it, and `--agy-allow-writes` just sets it for the
# current process tree.
AGY_ALLOW_WRITES_ENV = "HANDOFF_AGY_ALLOW_WRITES"


def agy_allow_writes_enabled() -> bool:
    return os.environ.get(AGY_ALLOW_WRITES_ENV, "").strip().lower() in ("1", "true", "yes", "on")


AGY_ALLOW_WRITES_RUNTIME_NOTE = (
    "This provider runs headless with every tool call pre-approved (no permission prompts), so a file\n"
    "write or shell command takes effect immediately. Stay inside the workspace, make only the changes\n"
    "the task asks for, and do not run destructive or irreversible commands (deleting files, git reset,\n"
    "git clean, force-push, publishing packages) unless the user's request explicitly asks for them."
)


def provider_runtime_note(provider: str) -> str | None:
    if provider == "agy" and agy_allow_writes_enabled():
        return AGY_ALLOW_WRITES_RUNTIME_NOTE
    return PROVIDER_RUNTIME_NOTES.get(provider)


def provider_runtime_note_block(provider: str) -> str:
    note = provider_runtime_note(provider)
    return f"\n## Runtime Note ({provider})\n\n{note}\n" if note else ""


PROTOCOL_DOC_PATHS = (
    "docs/agent-targeting-protocol.md",
    "docs/shared-agent-contract.md",
    "docs/verification-playbook.md",
)


def _normalized_doc_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def protocol_docs_customized() -> bool:
    """True when this workspace carries its own version of any of the three
    protocol docs (text differs from the copy bundled with the bridge).
    PROTOCOL_SUMMARY only summarizes the *bundled* rules, so a workspace
    that added its own rules (e.g. extra checks in
    docs/verification-playbook.md -- a documented extension point) must
    have the real text sent, on every surface (CLI, Web UI, desktop,
    remote) without any flag having to be threaded through each of them."""
    for rel in PROTOCOL_DOC_PATHS:
        workspace_path = Path(rel)
        bundled_path = BRIDGE_ROOT / rel
        if not workspace_path.exists() or not bundled_path.exists():
            continue
        try:
            workspace_text = workspace_path.read_text(encoding="utf-8")
            bundled_text = bundled_path.read_text(encoding="utf-8")
        except OSError:
            return True
        if _normalized_doc_text(workspace_text) != _normalized_doc_text(bundled_text):
            return True
    return False


def build_prompt(
    provider: str, state: dict[str, Any], user_prompt: str, reason: str | None = None, full: bool = False
) -> str:
    """`full=False` (the default, "compact" prompt mode) folds in the newest few
    `.handoff/current.md` entries (COMPACT_CURRENT_PROMPT_CHARS) and
    PROTOCOL_SUMMARY; `full=True` (`run --full-prompt`) restores the
    original behavior of inlining the three docs/ files in full plus a
    MAX_CURRENT_FILE_PROMPT_CHARS tail of the log. Compact mode switches
    itself to full when the workspace has customised any of the three docs
    (protocol_docs_customized()).

    `reason` is set only for a genuine handoff (this provider is being
    invoked *because* another one just hit quota/rate-limit/etc., or this
    is otherwise a cold start for it) -- in that case it needs the full
    picture below, same as ever.

    Otherwise, if `provider` already has a live `--resume`d session
    (`state["sessions"][provider]`, set by provider_command()/run_provider()
    once a session id comes back), the provider's own CLI-side session
    already retains everything sent in that session's first turn -- the
    protocol docs, the task, and whatever slice of `.handoff/current.md`
    existed then. Re-sending all of that in full on every subsequent turn
    is pure repeated overhead for no new information (confirmed real case:
    this alone was most of a ~70,000-token prompt for a one-word message
    on an actively-used project, see MAX_CURRENT_FILE_PROMPT_CHARS). A
    continuation turn instead sends only the new user message, a live
    git snapshot (this can genuinely change turn to turn), and whatever
    was appended to `.handoff/current.md` since this provider's own last
    turn (tracked per-provider in `state["prompt_context_offsets"]`, since
    a *different* provider's turn in between would otherwise be silently
    skipped from what this one sees).

    Known tradeoff, accepted deliberately rather than solved here: if a
    provider's CLI-side session has itself expired or been cleared
    independently of this bridge's own `state.json` still pointing at it,
    a continuation turn would under-inform a provider that (despite
    `--resume` being passed) no longer actually has that history. This is
    no worse than the pre-existing `--resume`-with-a-stale-id risk that
    already existed regardless of prompt content.

    SELF_CONTAINED_NOTICE below exists because of a real, observed failure
    mode (2026-09-04): asked to run a real first-turn prompt end-to-end,
    codex re-opened every doc already inlined below via `sed` (redundant
    tool-call round trips, each one real added latency on top of prompt
    size) *and* separately matched this project's own "handoff"
    terminology against an unrelated, user-machine-global codex skill
    (also named "handoff", covering a totally different `.agent/`
    PROJECT_CONTEXT/HANDOFF/DECISIONS scheme) plus a generic
    self-evaluation skill -- ballooning a one-word message into a
    multi-minute session that ran this project's own full test suite
    repeatedly and edited unrelated files. Nothing in this bridge's own
    code can prevent a differently-scoped, user-configured global skill
    from existing; the notice is a best-effort, in-prompt mitigation, not
    a guarantee.
    """
    if not full and protocol_docs_customized():
        full = True
    task = state.get("task") or user_prompt or "Continue the current handoff task."
    active_model = state.get("active_model") or "app-selected default"
    instruction_type = state.get("instruction_type") or "continue"
    full_current = read_text(CURRENT_FILE, "(no .handoff/current.md yet)")
    reason_block = f"\n## Handoff Reason\n\n{reason}\n" if reason else ""
    # Free-form, user-authored, per-workspace -- absent entirely (not an
    # empty/placeholder section) when the file doesn't exist or is
    # whitespace-only, so a workspace that never set one up doesn't carry
    # dead weight in every prompt.
    shared_context = read_text(SHARED_CONTEXT_FILE, "").strip()
    shared_context_block = f"\n## Project Context\n\n{shared_context}\n" if shared_context else ""

    resumed_session_id = (state.get("sessions") or {}).get(provider)
    prior_offset = (state.get("prompt_context_offsets") or {}).get(provider, 0)
    if not isinstance(prior_offset, int) or prior_offset < 0 or prior_offset > len(full_current):
        # A stale offset (log shortened by archiving, edited, or replaced
        # since it was recorded) would slice to nothing and silently skip
        # everything new; re-send what is there instead.
        prior_offset = 0
    # Recorded unconditionally (both branches below), so whichever branch
    # runs this turn, the *next* continuation turn's delta starts from
    # here -- not just from whenever a continuation turn last happened to
    # run.
    state.setdefault("prompt_context_offsets", {})[provider] = len(full_current)

    if resumed_session_id and reason is None:
        unseen = full_current[prior_offset:].strip()
        new_entries = (
            tail_for_prompt(unseen, MAX_CURRENT_FILE_PROMPT_CHARS)
            if full
            else recent_log_for_prompt(unseen, COMPACT_CURRENT_PROMPT_CHARS)
        )
        new_entries_block = (
            f"\n## New Handoff Log Entries Since Your Last Turn\n\n{new_entries}\n" if new_entries else ""
        )
        return f"""Continuing your existing {provider} session (resumed) via the agent-handoff-bridge CLI tool --
you already have the task, the shared protocol docs, and the prior state from earlier in this same
session; only what's new is included below.

{SELF_CONTAINED_NOTICE}

{SCOPE_DISCIPLINE_NOTICE}

## User Prompt For This Turn

{user_prompt or "Continue from the shared handoff packet and current workspace."}
{reason_block}{shared_context_block}{new_entries_block}{provider_runtime_note_block(provider)}
## Active Work Target

- Provider: {provider}
- Model: {active_model}
- Instruction type: {instruction_type}

## Workspace Snapshot

{git_snapshot()}
"""

    if full:
        targeting = read_workspace_or_bridge("docs/agent-targeting-protocol.md", "(no docs/agent-targeting-protocol.md yet)")
        contract = read_workspace_or_bridge(str(CONTRACT_FILE), "(no docs/shared-agent-contract.md yet)")
        verification = read_workspace_or_bridge(str(VERIFICATION_FILE), "(no docs/verification-playbook.md yet)")
        current = tail_for_prompt(full_current, MAX_CURRENT_FILE_PROMPT_CHARS)
        docs_behavior = (
            "- The full text of `docs/agent-targeting-protocol.md`, `docs/shared-agent-contract.md`, and\n"
            "  `docs/verification-playbook.md` is included below -- follow them, but there is no need to\n"
            "  open these files again."
        )
        docs_sections = f"""## Agent Targeting Protocol

{targeting}

## Shared Agent Contract

{contract}

## Verification Playbook

{verification}
"""
    else:
        current = recent_log_for_prompt(full_current, COMPACT_CURRENT_PROMPT_CHARS)
        docs_behavior = (
            "- The working rules are summarized below under \"Working Rules\" -- that summary is complete\n"
            "  for this task, so do not open `docs/agent-targeting-protocol.md`,\n"
            "  `docs/shared-agent-contract.md`, or `docs/verification-playbook.md` unless the task is about\n"
            "  changing them."
        )
        docs_sections = f"""## Working Rules

{protocol_summary_for(provider)}
"""
    return f"""You are {provider}, working via the agent-handoff-bridge CLI tool on a task in this workspace.

{SELF_CONTAINED_NOTICE}

{SCOPE_DISCIPLINE_NOTICE}

## Task

{task}
{shared_context_block}
## User Prompt For This Turn

{user_prompt or "Continue from the shared handoff packet and current workspace."}
{reason_block}
## Required Behavior

- Treat the active work target as Provider `{provider}` and Model `{active_model}` unless
  the user's latest instruction says otherwise.
- If the active model is unknown or app-selected, record the visible model if
  this surface exposes it.
{docs_behavior}
- `.handoff/current.md`'s relevant content is included below -- respect it without re-reading
  the file.
- The current git status is included below (see Workspace Snapshot) -- only re-run `git status`/
  `git diff` yourself if you have concrete reason to think something changed after this prompt
  was generated.
- Continue from the files on disk rather than assuming the prior transcript is complete.
- Keep changes narrowly scoped to the task.
{BEFORE_STOPPING_BY_PROVIDER.get(provider, BEFORE_STOPPING_DEFAULT)}
{provider_runtime_note_block(provider)}
## Active Work Target

- Provider: {provider}
- Model: {active_model}
- Account/App: CLI bridge
- Workspace: {Path.cwd()}
- Instruction type: {instruction_type}

{docs_sections}
## Bridge State Log (`.handoff/current.md`)

{current}

## Workspace Snapshot

{git_snapshot()}
{provider_final_override_block(provider, full)}"""


def provider_command(provider: str, state: dict[str, Any], model: str | None = None) -> list[str]:
    sessions = state.get("sessions", {})
    session_id = sessions.get(provider)
    if provider == "codex":
        # --skip-git-repo-check: codex exec refuses to run at all ("Not
        # inside a trusted directory and --skip-git-repo-check was not
        # specified") in any workspace that isn't itself a git repo --
        # which every workspace this bridge auto-creates
        # (create_workspace_for_first_message(), AUTO_WORKSPACE_BASE_DIR)
        # is not, by default. This bridge's own "workspace" concept (Open
        # Folder / auto-create) already establishes the same trust
        # boundary codex's own check exists to protect -- the user chose
        # this exact directory through this app -- so this is the
        # deliberate, no-narrower equivalent of running codex from inside
        # a directory it already trusts, not a broadened grant beyond
        # what --sandbox workspace-write already allows there.
        if session_id:
            command = [
                "codex",
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                "-c",
                'sandbox_mode="workspace-write"',
            ]
            if model:
                command.extend(["--model", model])
            command.extend([session_id, "-"])
            return command
        command = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
        if model:
            command.extend(["--model", model])
        command.append("-")
        return command

    if provider == "claude":
        # --verbose: newer claude CLI builds hard-require this alongside
        # --print (-p) + --output-format=stream-json ("Error: When using
        # --print, --output-format=stream-json requires --verbose") --
        # this only affects stdout's event stream (more event types, e.g.
        # tool-use events), not the "system"/"result"/"error" event
        # shapes summarize_claude() actually reads; parse_jsonl() already
        # tolerates and skips any event type it doesn't recognize.
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
                "--verbose",
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
            "--verbose",
            "--permission-mode",
            "auto",
        ]
        if model:
            command.extend(["--model", model])
        return command

    # provider == "agy" (Antigravity CLI, which replaced Gemini CLI for
    # individual accounts on 2026-06-18 and can run Google, Anthropic and
    # open-weight models -- `agy models`). The prompt travels via stdin like
    # the other two (subprocess.run(..., input=prompt, ...) in
    # run_provider()): piped stdin alone triggers print mode, verified
    # against agy 1.4.8 on 2026-10-28. Do NOT add `-p`: `-p` followed by
    # another flag swallows that flag as its prompt and exits 2.
    #
    # No permission flag is passed. In print mode agy auto-denies any tool
    # that needs a permission prompt (file writes, shell commands) unless
    # the user has allow rules under `permissions.allow` in agy's own
    # settings.json -- the run still exits 0 with status SUCCESS and an empty
    # response plus a `denied_actions` list, which summarize_agy() turns into
    # a tool_failure so the work is handed on instead of looking done.
    # `--dangerously-skip-permissions` approves every tool call with no
    # scoping (documented at antigravity.google/docs/cli/headless), which is
    # broader than codex's workspace-write sandbox, so it is only passed when
    # the user opted in (HANDOFF_AGY_ALLOW_WRITES / --agy-allow-writes). The
    # separate `--sandbox` flag is NOT added: its effect ("terminal
    # restrictions") could not be verified, and an unverified flag would only
    # give a false sense of safety.
    command = ["agy", "--output-format", "json"]
    if session_id:
        # `--conversation <ID>` resumes a specific prior conversation (the
        # `conversation_id` in agy's JSON response, captured by
        # summarize_agy()); verified together with stdin input.
        command.extend(["--conversation", session_id])
    if model:
        command.extend(["--model", model])
    if agy_allow_writes_enabled():
        command.append("--dangerously-skip-permissions")
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
    summary: dict[str, Any] = {
        "provider": "codex",
        "session_id": None,
        "usage": None,
        "final_text": "",
        "errors": [],
    }
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


def summarize_agy(stdout: str, stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    """`agy --output-format json` writes one JSON envelope (not a JSONL
    stream): `conversation_id`, `status`, `response`, `duration_seconds`,
    `num_turns`, `usage` (input/output/thinking/cache_read/total tokens), and
    -- only when something was refused -- `denied_actions`. Shape verified
    against agy 1.4.8 (2026-10-28) and
    https://antigravity.google/docs/cli/headless.

    Two quirks this has to handle:

    - A tool call that needs a permission prompt is auto-denied in print
      mode, yet the run still exits 0 with `status: "SUCCESS"`. With an empty
      `response` that means the work was not done, so it is reported as a
      `tool_failure` (which classify_handoff() turns into a handoff) with a
      message telling the user how to grant the permission. A denial next to
      a non-empty response is a failure too: a refused write or command
      means the changes that reply describes were not applied (agy may only
      be proposing a diff), so the work is handed on with the reply kept.
    - `stdout` is tried first and `stderr` only when stdout has nothing
      parseable, so a real response is never shadowed by stderr noise.

    `session_id` is the real `conversation_id`, set only when exit_code is 0
    and nothing in the envelope signalled a hard error, so a failed run never
    marks a conversation resumable.
    """
    summary: dict[str, Any] = {
        "provider": "agy",
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
    usage = data.get("usage")
    if isinstance(usage, dict):
        summary["usage"] = usage
    hard_error = False
    error = data.get("error")
    status = data.get("status")
    if error:
        summary["errors"].append(error)
        hard_error = True
    elif isinstance(status, str) and status.upper() != "SUCCESS":
        summary["errors"].append({"type": "agy_status", "status": status, "message": str(data.get("message") or "")})
        hard_error = True
    denied = data.get("denied_actions")
    if isinstance(denied, list) and denied:
        names = sorted(
            {
                str(item.get("action") or item.get("display_name") or "?")
                for item in denied
                if isinstance(item, dict)
            }
        )
        listed = ", ".join(names) or "?"
        if not str(summary["final_text"]).strip():
            message = (
                f"agy denied tool action(s) [{listed}]: print mode cannot prompt for "
                "permission, so nothing was done. Add allow rules under `permissions.allow` in agy's "
                "settings.json (for example write_file(<path>) or command(<command>))."
            )
            summary["final_text"] = message
        else:
            # The reply is kept as-is (the next provider can read what agy
            # proposed), and a denied write/command means the work it
            # describes was NOT applied -- so this is still a failure, not a
            # success with a footnote. The note stays out of final_text on
            # purpose: classify_handoff() cuts final_text out of the stdout
            # it scans, which only works while final_text is byte-for-byte
            # what the CLI printed.
            message = (
                f"agy denied tool action(s) [{listed}]: print mode cannot prompt for permission, so "
                "the changes its reply describes were not applied. Allow them under `permissions.allow` "
                "in agy's settings.json, or let the next provider apply them."
            )
            summary["note"] = f"({message})"
        summary["errors"].append({"type": "tool_failure", "message": message})
    if not hard_error and exit_code == 0 and data.get("conversation_id"):
        summary["session_id"] = str(data["conversation_id"])
    return summary


# Event types each parser understands. Their lines are dropped from the signal
# scan whole: they carry the answer, narration and every tool's echoed output,
# and a real failure among them (codex "error"/"turn.failed", claude is_error
# results) is already in parsed["errors"]. The sets are per provider on purpose:
# a type that is normal for one CLI ("result" for claude) is unknown for the
# other, and must not hide that CLI's new error event.
KNOWN_EVENT_TYPES_BY_PROVIDER = {
    "codex": frozenset(
        {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
         "item.started", "item.updated", "item.completed"}
    ),
    "claude": frozenset(
        {"system", "assistant", "user", "result", "error", "stream_event", "rate_limit_event"}
    ),
}
# Used when the provider is not known (synthetic callers): the union.
_ALL_KNOWN_EVENT_TYPES = frozenset().union(*KNOWN_EVENT_TYPES_BY_PROVIDER.values())
# The fields of an *unrecognized* event that can say "usage limit reached".
_UNKNOWN_EVENT_SIGNAL_FIELDS = ("error", "message", "status", "reason", "detail")


def _unknown_event_text(event: dict[str, Any]) -> str:
    parts = []
    for field in _UNKNOWN_EVENT_SIGNAL_FIELDS:
        value = event.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            parts.extend(v for v in value.values() if isinstance(v, str))
    return "\n".join(parts)


def _signal_scan_text(stdout: str, stderr: str, parsed: dict[str, Any]) -> str:
    """The text classify_handoff() looks for handoff signals in: stderr, the
    stdout lines that are NOT structured JSON, and the error-like fields of JSON
    events this parser does not recognize.

    A provider's structured events (codex/claude JSONL, agy's single envelope)
    carry its answer, narration and every tool's echoed output -- arbitrary text
    that may legitimately say "quota", "rate limit" or "command not found" (a grep
    over this repo's own source, a README, a bug summary). A real failure from a
    known event already arrives as an error in parsed["errors"], which
    classify_handoff() handles first, so those lines are dropped whole instead of
    being matched against.

    This replaces cutting final_text / tool output out of the raw stream by
    string replacement, which leaked: codex writes non-ASCII as UTF-8 but the
    cut looked for the \\uXXXX form, one short fragment ("error") removed
    from inside longer lines before they could match, nested JSON escaping
    changed the bytes again, and earlier agent_message narration was never cut.

    What is left is where a real signal can still appear: stderr, plain-text
    stdout (a CLI that prints "Error: 429 too many requests" and exits 0 -- see
    test_rate_limit_signal_in_stdout), and a future event type such as
    {"type": "warning", "message": "usage limit reached"}, which no parser knows
    yet. parsed["final_text"] is still removed for providers whose answer is
    plain text.
    """
    kept = [stderr]
    provider = parsed.get("provider")
    known_types = KNOWN_EVENT_TYPES_BY_PROVIDER.get(provider, _ALL_KNOWN_EVENT_TYPES)
    stripped_stdout = stdout.strip()
    whole_is_json = False
    # Only agy prints one (possibly pretty-printed, multi-line) JSON envelope; its
    # failures are in parsed["errors"]. For the JSONL providers a single event
    # on stdout must still go through the per-event check below.
    if provider == "agy" and stripped_stdout.startswith(("{", "[")):
        try:
            json.loads(stripped_stdout)
            whole_is_json = True
        except ValueError:
            pass
    if not whole_is_json:
        for line in stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith(("{", "[")):
                try:
                    event = json.loads(text)
                except ValueError:
                    kept.append(line)  # looks like JSON but isn't: treat as text
                    continue
                if isinstance(event, dict) and event.get("type") not in known_types:
                    kept.append(_unknown_event_text(event))
                continue  # a recognized event line: its errors are in parsed["errors"]
            kept.append(line)
    scan_text = "\n".join(kept)
    final_text = parsed.get("final_text") or ""
    if final_text:
        scan_text = scan_text.replace(final_text, "")
    return scan_text


def classify_handoff(exit_code: int, stdout: str, stderr: str, parsed: dict[str, Any]) -> tuple[bool, str]:
    """Classify whether the run needs a handoff.

    The reason string always starts with one of `HANDOFF_LABELS` (or `none`
    when no handoff is needed) so downstream tooling and `.handoff/current.md`
    stay in the vocabulary defined by docs/shared-agent-contract.md.
    """
    scan_text = _signal_scan_text(stdout, stderr, parsed)
    if parsed.get("errors"):
        errors_text = json.dumps(parsed["errors"], ensure_ascii=False)
        for label, pattern in ERROR_PATTERNS:
            if pattern.search(scan_text) or pattern.search(errors_text):
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
        *([f"- Duration: {record['duration_seconds']}s"] if record.get("duration_seconds") is not None else []),
        *(["- Permissions: all tool calls auto-approved"] if record.get("permissions") == "allow-all" else []),
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
    # A state.json written before a provider was removed (e.g. "gemini")
    # must not resolve to it: fall through to the first installed one.
    if primary in PROVIDERS and shutil.which(primary):
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
    full_prompt = bool(getattr(args, "full_prompt", False))
    # A run started while the previous run's handoff is still unresolved, on a
    # different provider, is the bridge's own notion of "taking over" (it is
    # what choose_auto_provider() and auto-fallback act on). Remember which run
    # it took over so stats can count real hops instead of adjacent records.
    handoff_from = None
    history_so_far = state.get("history") or []
    if (
        history_so_far
        and state.get("status") == "handoff_needed"
        and state.get("last_provider") not in (None, provider)
    ):
        handoff_from = history_so_far[-1].get("run_dir")
    if args.execute:
        moved = archive_old_current_entries(state)
        if moved:
            # The shifted prompt_context_offsets must reach disk before the
            # (possibly minutes-long) provider call: state is otherwise only
            # saved after it, and an interruption in between would leave
            # offsets pointing past the end of the now-shorter log.
            save_state(state)
            print(f"Archived {moved} characters of older .handoff/current.md entries to {ARCHIVE_DIR}/")
    prompt = build_prompt(provider, state, user_prompt, reason, full=full_prompt)
    command = provider_command(provider, state, model_override_arg(args.model))

    if not args.execute:
        NEXT_PROMPT_FILE.write_text(prompt, encoding="utf-8")
        print("Preview mode: no provider was invoked and no tokens were spent.")
        print(f"Provider: {provider}")
        print(f"Command: {command_to_display(command)}")
        print(f"Prompt written to: {NEXT_PROMPT_FILE}")
        print("Add --execute to run it.")
        return 0

    run_id_base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{provider}"
    run_id = run_id_base
    attempt = 1
    while True:
        run_dir = RUNS_DIR / run_id
        try:
            # exist_ok=False: two turns of one provider that start within the
            # same second must not share (and overwrite) a log directory.
            run_dir.mkdir(parents=True)
            break
        except FileExistsError:
            attempt += 1
            run_id = f"{run_id_base}-{attempt}"
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started_at = utc_now()
    started_clock = time.monotonic()
    print(f"Running {provider}; logs will be written to {run_dir}")
    allow_all = provider == "agy" and agy_allow_writes_enabled()
    if allow_all:
        print(f"WARNING: agy runs with every tool call auto-approved ({AGY_ALLOW_WRITES_ENV} / --agy-allow-writes).")
    stdout_path = run_dir / "stdout.jsonl"
    # The provider writes its stdout straight into the run's stdout.jsonl
    # instead of a pipe, so the file grows while it works and the Web UI can show
    # what it is doing right now (run_progress.py). It is read back below, so
    # everything downstream sees the same text a pipe would have given it.
    live_stdout = stdout_path.open("wb")
    try:
        process = subprocess.run(
            command,
            input=prompt,
            text=True,
            # encoding/errors: `prompt` folds in this project's own docs
            # (docs/shared-agent-contract.md etc., liberally using em
            # dashes and other Unicode punctuation) plus arbitrary user
            # text -- without an explicit encoding, subprocess falls back
            # to locale.getpreferredencoding() for the stdin write, which
            # is cp949 (not UTF-8) on a Korean-locale Windows machine and
            # raises UnicodeEncodeError on the very first non-cp949
            # character, before the provider CLI is ever actually run.
            # Reproduced directly (2026-08-14): a plain "테스트" prompt
            # still crashed here, since the *doc content* folded into
            # `prompt`, not the user's own text, contained the offending
            # character. errors="replace" also covers the read-back
            # direction (a provider's real stdout/stderr) so a stray
            # malformed byte can't crash an otherwise-successful run --
            # the same never-crash-on-decode posture as
            # decode_timeout_output() elsewhere in this file.
            encoding="utf-8",
            errors="replace",
            stdout=live_stdout,
            stderr=subprocess.PIPE,
            cwd=Path.cwd(),
            timeout=None if args.timeout_seconds == 0 else args.timeout_seconds,
            check=False,
        )
        exit_code = process.returncode
        stdout = process.stdout  # None: it went to live_stdout
        stderr = process.stderr
    except FileNotFoundError:
        exit_code = 127
        stdout = ""
        stderr = f"{provider} command not found"
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = decode_timeout_output(exc.stdout)
        stderr = decode_timeout_output(exc.stderr) or "provider timed out"
    finally:
        live_stdout.close()
    # Text that came back through subprocess (a mocked run) still has to be
    # written; what the provider already wrote to the file is the log as it
    # was, and is left alone -- rewriting it would risk losing it (a timeout's
    # partial output) in the moment between truncating and writing.
    returned_stdout = bool(stdout)
    if not returned_stdout:
        # What the provider wrote to the file (also what it had written when a
        # timeout killed it).
        stdout = read_text_lossy(stdout_path)

    duration_seconds = round(time.monotonic() - started_clock, 2)
    if returned_stdout:
        stdout_path.write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    if provider == "codex":
        parsed = summarize_codex(parse_jsonl(stdout))
    elif provider == "claude":
        parsed = summarize_claude(parse_jsonl(stdout))
    else:
        # single JSON object, not a JSONL stream; may land on stdout
        # (success) or stderr (fatal error) -- see summarize_agy()
        parsed = summarize_agy(stdout, stderr, exit_code)
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
        "duration_seconds": duration_seconds,
        **({"handoff_from": handoff_from} if handoff_from else {}),
        **({"permissions": "allow-all"} if allow_all else {}),
        "session_id": session_id,
        "usage": parsed.get("usage"),
        "cost_usd": parsed.get("cost_usd"),
        "final_text": ((parsed.get("final_text") or excerpt(stderr)) + (f"\n\n{parsed['note']}" if parsed.get("note") else "")),
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
        if args.auto_fallback and fallback == provider:
            # next_available_provider() hands back `provider` itself when
            # nothing else is installed (only one CLI present, or none at
            # all). Recursing would just re-run the same provider and
            # append a duplicate failure record, so stop at the first one.
            print(f"Auto-fallback enabled, but no other installed provider to switch to; not retrying {provider}.")
        elif args.auto_fallback:
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
                full_prompt=full_prompt,
            )
            return run_provider(fallback, next_args, state, handoff_reason)
        # Carry the ORIGINAL user_prompt into the fallback, not a generic
        # placeholder -- the handoff_reason is already conveyed separately
        # via build_prompt()'s reason_block, so replacing the actual
        # request/attachments here just means the fallback provider has no
        # idea what the user actually asked for.
        next_prompt = build_prompt(fallback, state, user_prompt, handoff_reason, full=full_prompt)
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


# --- Interactive terminal chat (`chat`) ---------------------------------
#
# A thin REPL over the same path the Web UI and `run --execute` use
# (run_command -> run_provider), so state.json, session resumption,
# auto-fallback, and the compact prompt all behave identically; it only adds
# a line-oriented front end and a per-turn token readout.

CHAT_HELP = """Type a message and press Enter to send it. End a line with \\ to continue on the next line.

  /provider [auto|codex|claude|agy]   show or set which provider runs the next turn
  /fallback [on|off]                     show or set auto-fallback to another CLI on a limit
  /full [on|off]                         show or set full-prompt mode (inline the docs/ files)
  /tokens                                token totals for this chat session
  /stats                                 runs, handoffs, tokens and time recorded in this workspace
  /status                                bridge state and installed CLIs
  /help                                  this text
  /exit                                  leave (Ctrl-D also works)

Token numbers: "sent" is an estimate of what the bridge put in the prompt (characters / 4); "reported"
is whatever usage the provider CLI itself returned, when it returned one."""

ESTIMATED_CHARS_PER_TOKEN = 4


def estimate_tokens(char_count: int) -> int:
    return (char_count + ESTIMATED_CHARS_PER_TOKEN - 1) // ESTIMATED_CHARS_PER_TOKEN


def usage_token_counts(usage: Any) -> dict[str, int]:
    """Best-effort sum of the input/output/cached token fields found anywhere
    in a provider's `usage` object. Only field names Codex, Claude Code and agy are
    known to emit are recognised (plus agy's `cache_read_tokens`); anything
    else yields {} and the caller shows no reported figure rather than a
    guessed one."""
    names = {
        "inputtokens": "input",
        "outputtokens": "output",
        "cachedinputtokens": "cached",
        "cachereadinputtokens": "cached",
        "cachereadtokens": "cached",
    }
    totals: dict[str, int] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                bucket = names.get(str(key).replace("_", "").lower())
                if bucket and isinstance(item, (int, float)) and not isinstance(item, bool):
                    totals[bucket] = totals.get(bucket, 0) + int(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(usage)
    return totals


def format_token_counts(counts: dict[str, int]) -> str:
    parts = [f"{label} {counts[key]:,}" for key, label in (("input", "in"), ("output", "out"), ("cached", "cached")) if key in counts]
    return ", ".join(parts)


def record_prompt_chars(record: dict[str, Any]) -> int:
    run_dir = record.get("run_dir")
    if not run_dir:
        return 0
    try:
        return len((Path(run_dir) / "prompt.md").read_text(encoding="utf-8"))
    except OSError:
        return 0


class ChatSession:
    """State for one `chat` run; kept separate from the input loop so tests
    can drive turns without a terminal."""

    def __init__(self, provider: str, auto_fallback: bool, full_prompt: bool, model: str | None, timeout_seconds: int):
        self.provider = provider
        self.auto_fallback = auto_fallback
        self.full_prompt = full_prompt
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.turns = 0
        self.sent_tokens = 0
        self.reported = {"input": 0, "output": 0, "cached": 0}

    def run_turn(self, text: str, out: "Any" = print) -> int:
        args = argparse.Namespace(
            provider=self.provider,
            prompt=text,
            prompt_file=None,
            execute=True,
            auto_fallback=self.auto_fallback,
            timeout_seconds=self.timeout_seconds,
            model=self.model,
            instruction_type="continue",
            full_prompt=self.full_prompt,
        )
        before = len(load_state().get("history", []))
        out(f"... running {self.provider}")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run_command(args)
        records = load_state().get("history", [])[before:]
        if not records:
            # Nothing was recorded (e.g. the workspace run lock timed out):
            # show whatever the run printed so the failure is not silent.
            out(buffer.getvalue().strip() or f"run exited {code} without recording a result")
            return code
        for record in records:
            self.turns += 1
            sent = estimate_tokens(record_prompt_chars(record))
            reported = usage_token_counts(record.get("usage"))
            self.sent_tokens += sent
            for key in self.reported:
                self.reported[key] += reported.get(key, 0)
            status = "handoff needed" if record.get("handoff_needed") else "ok"
            out(f"[{record.get('provider')}] exit {record.get('exit_code')} - {status} ({record.get('reason')})")
            if record.get("permissions") == "allow-all":
                # run_provider()'s own warning went into `buffer` above and is
                # dropped when a record exists, so repeat it for every run.
                out(f"WARNING: {record.get('provider')} ran with every tool call auto-approved ({AGY_ALLOW_WRITES_ENV} / --agy-allow-writes).")
            final_text = (record.get("final_text") or "").strip()
            if final_text:
                out(final_text)
            line = f"tokens: sent ~{sent:,}"
            if reported:
                line += f" | reported {format_token_counts(reported)}"
            if record.get("cost_usd") is not None:
                line += f" | cost ${record['cost_usd']}"
            out(line)
        if len(records) > 1:
            out(f"(auto-fallback ran {len(records)} providers this turn)")
        return code

    def totals_line(self) -> str:
        line = f"this session: {self.turns} run(s), sent ~{self.sent_tokens:,}"
        reported = {k: v for k, v in self.reported.items() if v}
        if reported:
            line += f" | reported {format_token_counts(reported)}"
        return line

    def handle_command(self, line: str, out: "Any" = print) -> bool:
        """Handle a `/command`; returns False when the chat should end."""
        name, _, rest = line.strip().partition(" ")
        rest = rest.strip().lower()
        if name in ("/exit", "/quit"):
            return False
        if name == "/help":
            out(CHAT_HELP)
        elif name == "/provider":
            if rest:
                if rest not in ("auto",) + PROVIDERS:
                    out(f"unknown provider '{rest}' (choose auto, {', '.join(PROVIDERS)})")
                else:
                    self.provider = rest
            out(f"provider: {self.provider}")
        elif name in ("/fallback", "/full"):
            attribute = "auto_fallback" if name == "/fallback" else "full_prompt"
            if rest in ("on", "off"):
                setattr(self, attribute, rest == "on")
            elif rest:
                out(f"usage: {name} [on|off]")
            out(f"{name[1:]}: {'on' if getattr(self, attribute) else 'off'}")
        elif name == "/stats":
            out(format_stats(compute_stats(load_state().get("history", []))))
        elif name == "/tokens":
            out(self.totals_line())
        elif name == "/status":
            state = load_state()
            installed = ", ".join(f"{p}={'yes' if shutil.which(p) else 'no'}" for p in PROVIDERS)
            out(f"workspace: {Path.cwd()}")
            out(f"status: {state.get('status')} | last provider: {state.get('last_provider')} | next 'auto': {choose_auto_provider(state)}")
            out(f"installed CLIs: {installed}")
        else:
            out(f"unknown command {name} (try /help)")
        return True


def read_chat_message(input_fn: "Any") -> str:
    """Read one message: a line, or several when a line ends with a
    backslash. EOFError and KeyboardInterrupt propagate with the partially
    typed text dropped, so a cancelled message is never returned (and so
    never sent to a provider)."""
    lines = [input_fn("> ")]
    while lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1]
        lines.append(input_fn(". "))
    return "\n".join(lines).strip()


def chat_command(args: argparse.Namespace, input_fn: "Any" = input, out: "Any" = print) -> int:
    session = ChatSession(args.provider or "auto", args.auto_fallback, args.full_prompt, args.model, args.timeout_seconds)
    try:
        import readline  # noqa: F401  (line editing and history; unavailable on Windows)
    except ImportError:
        pass
    installed = ", ".join(p for p in PROVIDERS if shutil.which(p)) or "none found"
    out(f"agent-handoff-bridge {BRIDGE_VERSION} chat - workspace {Path.cwd()}")
    out(f"provider: {session.provider} | installed CLIs: {installed} | /help for commands")
    if agy_allow_writes_enabled():
        out("WARNING: agy will run with every tool call auto-approved (file writes and shell commands run immediately).")
    while True:
        try:
            text = read_chat_message(input_fn)
        except EOFError:
            # End of input (Ctrl-D, closed pipe), including in the middle of a
            # multi-line message: leave without sending anything half-typed.
            out("")
            break
        except KeyboardInterrupt:
            # Ctrl-C while typing discards the message being typed and
            # returns to a fresh prompt.
            out("")
            continue
        if not text:
            continue
        if text.startswith("/"):
            if not session.handle_command(text, out):
                break
            continue
        try:
            session.run_turn(text, out)
        except KeyboardInterrupt:
            # subprocess.run() kills the provider CLI it started when it is
            # interrupted, before any result is recorded, so nothing was
            # written to state.json or current.md for this turn.
            out(
                "interrupted - the provider process was stopped and this turn was not recorded "
                "(anything it had already started, such as shell commands or edits, may have completed)"
            )
    out(session.totals_line())
    return 0


def compute_stats(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-provider run counts, handoff reasons, reported tokens and wall
    time from `state["history"]`, plus how often a handed-off turn ended
    without another handoff.

    "ok" means only that no handoff signal was detected (classify_handoff()
    returned handoff_needed False) -- it is not a check that the work is
    correct. Tokens are summed exactly as each CLI reports them and are not
    comparable across providers (Codex's input count includes cache reads,
    Claude's does not), and no price is applied: subscription quota and API
    token prices are different units.

    "per ok run" is computed over the *measured* runs only: the tokens (or
    seconds) of the runs that reported them, divided by the number of ok runs
    among those same runs. Dividing measured spend by ALL ok runs would
    understate the cost whenever some runs (older records, a failed run whose
    CLI printed no usage) have no figure. `tokens_complete` / `seconds_complete`
    say whether every run of the provider was measured; when not, the figure is
    only an estimate from the measured subset. It is None when no ok run is in
    that subset.

    Handoff hops are the records the bridge itself marked `handoff_from` --
    a run started while the previous run's handoff was still unresolved and
    on a different provider. Records written before that field existed are
    not counted, because adjacent records alone do not show that one run
    actually took over the other's task."""
    providers: dict[str, dict[str, Any]] = {}
    for record in history:
        name = str(record.get("provider") or "?")
        entry = providers.setdefault(
            name,
            {
                "runs": 0,
                "ok": 0,
                "handoffs": 0,
                "reasons": {},
                "tokens": {"input": 0, "output": 0, "cached": 0},
                "runs_with_usage": 0,
                "ok_with_usage": 0,
                "seconds": 0.0,
                "runs_timed": 0,
                "ok_timed": 0,
            },
        )
        is_ok = not record.get("handoff_needed")
        entry["runs"] += 1
        if is_ok:
            entry["ok"] += 1
        else:
            entry["handoffs"] += 1
            label = str(record.get("reason") or "unknown").split(":", 1)[0].strip() or "unknown"
            entry["reasons"][label] = entry["reasons"].get(label, 0) + 1
        counts = usage_token_counts(record.get("usage"))
        if counts:
            entry["runs_with_usage"] += 1
            entry["ok_with_usage"] += int(is_ok)
            for key, value in counts.items():
                entry["tokens"][key] += value
        duration = record.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            entry["seconds"] += float(duration)
            entry["runs_timed"] += 1
            entry["ok_timed"] += int(is_ok)
    for entry in providers.values():
        billed = entry["tokens"]["input"] + entry["tokens"]["output"]
        entry["tokens_per_ok_run"] = round(billed / entry["ok_with_usage"]) if entry["ok_with_usage"] else None
        entry["seconds_per_ok_run"] = round(entry["seconds"] / entry["ok_timed"], 1) if entry["ok_timed"] else None
        entry["tokens_complete"] = entry["runs_with_usage"] == entry["runs"]
        entry["seconds_complete"] = entry["runs_timed"] == entry["runs"]
        entry["seconds"] = round(entry["seconds"], 1)
    hops = hops_ok = 0
    for record in history:
        if record.get("handoff_from"):
            hops += 1
            if not record.get("handoff_needed"):
                hops_ok += 1
    return {"runs": len(history), "providers": providers, "handoff_hops": hops, "handoff_hops_ok": hops_ok}


STATS_NOTE = (
    "ok = no handoff signal detected, not proof the work is correct. Tokens are as each CLI reports them "
    "(not comparable across providers; no prices applied). \"per ok run\" uses only the runs that were "
    "measured; \"partial\" means some runs had no figure, so it is an estimate."
)


def format_stats(stats: dict[str, Any]) -> str:
    if not stats["runs"]:
        return "No runs recorded in this workspace yet."
    lines = [f"Runs recorded: {stats['runs']}"]
    for name, entry in sorted(stats["providers"].items()):
        lines.append("")
        lines.append(f"{name}: {entry['runs']} run(s), {entry['ok']} ok, {entry['handoffs']} handoff")
        if entry["reasons"]:
            lines.append("  handoff reasons: " + ", ".join(f"{k} x{v}" for k, v in sorted(entry["reasons"].items())))
        if entry["runs_with_usage"]:
            lines.append(
                f"  tokens ({entry['runs_with_usage']}/{entry['runs']} runs reported): "
                + format_token_counts({k: v for k, v in entry["tokens"].items() if v})
            )
        else:
            lines.append("  tokens: none reported")
        if entry["runs_timed"]:
            lines.append(f"  time ({entry['runs_timed']}/{entry['runs']} runs timed): {entry['seconds']}s total")
        per = []
        if entry["tokens_per_ok_run"] is not None:
            partial = "" if entry["tokens_complete"] else f" (partial: {entry['runs_with_usage']}/{entry['runs']} runs)"
            per.append(f"{entry['tokens_per_ok_run']:,} in+out tokens{partial}")
        if entry["seconds_per_ok_run"] is not None:
            partial = "" if entry["seconds_complete"] else f" (partial: {entry['runs_timed']}/{entry['runs']} runs)"
            per.append(f"{entry['seconds_per_ok_run']}s{partial}")
        if per:
            lines.append("  per ok run (measured runs' spend / their ok runs): " + ", ".join(per))
    lines.append("")
    if stats["handoff_hops"]:
        lines.append(
            f"Handoffs: {stats['handoff_hops']} linked hop(s), {stats['handoff_hops_ok']} ended without another handoff"
        )
    else:
        lines.append("Handoffs: no linked hops recorded")
    lines.append(f"Note: {STATS_NOTE}")
    return "\n".join(lines)


def stats_command(args: argparse.Namespace) -> int:
    stats = compute_stats(load_state().get("history", []))
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print(format_stats(stats))
    return 0


def status(_: argparse.Namespace) -> int:
    state = load_state()
    print(json.dumps(state, indent=2, ensure_ascii=False))
    if CURRENT_FILE.exists():
        print(f"\nCurrent packet: {CURRENT_FILE}")
    return 0


def check_update_command(_: argparse.Namespace) -> int:
    """CLI wrapper around check_for_update() -- prints its result as one
    line of JSON. Structure audit: handoff_webui.py used to be the one
    consumer that imported and called check_for_update() in-process while
    every other bridge-invoking consumer only ever reached the bridge's
    behavior through a subprocess call to this CLI; this subcommand closes
    that asymmetry."""
    print(json.dumps(check_for_update(), ensure_ascii=False))
    return 0


def next_provider_command(args: argparse.Namespace) -> int:
    """CLI wrapper around next_available_provider() -- prints the chosen
    provider name (plain text; a single token needs no JSON envelope)."""
    print(next_available_provider(args.current))
    return 0


def resolve_auto_provider_command(_: argparse.Namespace) -> int:
    """CLI wrapper around choose_auto_provider() -- reads this workspace's
    state.json (via the already-applied --workspace chdir, same as
    status()) and prints which real provider "auto" currently resolves
    to."""
    print(choose_auto_provider(load_state()))
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
    p_run.add_argument(
        "--full-prompt",
        action="store_true",
        help="Inline the full docs/ protocol files and a 20,000-character log tail instead of the compact default.",
    )
    p_run.add_argument(
        "--agy-allow-writes",
        action="store_true",
        help=f"Let agy write files and run commands without prompting (passes --dangerously-skip-permissions; same as {AGY_ALLOW_WRITES_ENV}=1).",
    )
    p_run.add_argument("--timeout-seconds", type=int, default=0, help="0 means no timeout.")
    p_run.add_argument("--model", help="Model label to record; exact model strings are also passed to the provider.")
    p_run.add_argument(
        "--instruction-type", choices=INSTRUCTION_TYPES, default="continue", help="Instruction type for this run."
    )
    p_run.set_defaults(func=run_command)

    p_chat = sub.add_parser("chat", help="Interactive terminal chat that runs providers through the bridge.")
    # default None (not "auto") so parse_cli_args() can tell an omitted provider
    # from an explicit one; it turns None into "auto".
    p_chat.add_argument("provider", nargs="?", choices=("auto",) + PROVIDERS, default=None)
    p_chat.add_argument(
        "--auto-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Switch to another installed CLI on a quota/rate-limit/context signal (default: on).",
    )
    p_chat.add_argument("--full-prompt", action="store_true", help="Inline the full docs/ files in cold-start prompts.")
    p_chat.add_argument(
        "--agy-allow-writes",
        action="store_true",
        help=f"Let agy write files and run commands without prompting (passes --dangerously-skip-permissions; same as {AGY_ALLOW_WRITES_ENV}=1).",
    )
    p_chat.add_argument("--timeout-seconds", type=int, default=0, help="Per-provider timeout; 0 means no timeout.")
    p_chat.add_argument("--model", help="Model label to record; exact model strings are also passed to the provider.")
    p_chat.set_defaults(func=chat_command)

    p_stats = sub.add_parser("stats", help="Summarize runs, handoffs, reported tokens and time in this workspace.")
    p_stats.add_argument("--json", action="store_true", help="Print the figures as JSON.")
    p_stats.set_defaults(func=stats_command)

    p_status = sub.add_parser("status", help="Print bridge state.")
    p_status.set_defaults(func=status)

    p_check = sub.add_parser("check", help="Validate bridge files without calling model providers.")
    p_check.set_defaults(func=check)

    p_check_update = sub.add_parser("check-update", help="Check for a newer release; prints JSON.")
    p_check_update.set_defaults(func=check_update_command)

    p_next_provider = sub.add_parser("next-provider", help="Print the next available provider after <current>.")
    p_next_provider.add_argument("current", choices=PROVIDERS)
    p_next_provider.set_defaults(func=next_provider_command)

    p_resolve_auto = sub.add_parser(
        "resolve-auto-provider", help="Print which provider 'auto' currently resolves to for this workspace."
    )
    p_resolve_auto.set_defaults(func=resolve_auto_provider_command)

    return parser


def parse_cli_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """`parser.parse_args`, plus the optional-positional ordering fix that
    Python 3.11 needs. Before 3.12, argparse matches an `nargs="?"` positional
    (run's prompt, chat's provider) as empty as soon as an option follows the
    required positionals, so `run codex --execute "text"` -- the form the docs
    use -- failed with "unrecognized arguments: text". Python 3.12+ parses it
    correctly and never reaches the fallback below."""
    args, extras = parser.parse_known_args(argv)
    if len(extras) == 1 and not extras[0].startswith("-"):
        value = extras[0]
        if args.command == "run" and not args.prompt:
            args.prompt = value
            return args
        if args.command == "chat" and args.provider is None and value in ("auto",) + PROVIDERS:
            args.provider = value
            return args
    if extras:
        parser.error("unrecognized arguments: " + " ".join(extras))
    if args.command == "chat" and args.provider is None:
        args.provider = "auto"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(build_parser(), argv)
    if getattr(args, "agy_allow_writes", False):
        # Through the environment so the nested auto-fallback call and any
        # subprocess started from here see it exactly like a user-set variable.
        os.environ[AGY_ALLOW_WRITES_ENV] = "1"
    chdir_workspace(args.workspace, create=args.command == "install")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
