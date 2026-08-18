"""Dispatching a real provider run: CLI mode (shells out to
handoff_bridge.py -- the same CLI a human would type -- rather than
importing and calling its functions in-process, since those resolve
.handoff/state.json relative to the *process* cwd via chdir_workspace(),
which isn't safe to call in-process from a ThreadingHTTPServer handler)
with a fallback to webui_api_key_mode.py's API-key mode when a provider's
CLI isn't installed and a key is saved for it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from handoff_bridge import PROVIDERS

from webui_api_key_mode import _api_key_mode_error_record, run_provider_via_api_key
from webui_common import bridge_command_prefix, _bridge_next_provider, _bridge_resolve_auto_provider
from webui_credentials import cli_available, custom_provider_name, is_custom_provider, read_credentials, read_custom_providers

PROVIDER_RUN_TIMEOUT_SECONDS = 600

# run_provider_via_bridge() reads .handoff/state.json's history length
# before the subprocess call and diffs against it after, with no lock in
# between -- two concurrent POST /api/run calls (e.g. the Enter-key path in
# webui/app.js doesn't check whether a run is already in flight) would both
# read the same "before" length, and whichever finishes second would slice
# in the first call's already-persisted record too, duplicating it as a
# second agent chat message. There's only one AppState.workspace server-wide,
# so a single process-wide lock (not a per-workspace one) is correct here --
# every concurrent run is necessarily against the same active workspace.
# A plain threading.Lock, not handoff_bridge.WriteLock: the contention here
# is between HTTP request threads in this one process, not separate CLI
# processes, and WriteLock's 10s default timeout is far too short for a
# provider call that can legitimately take minutes.
_RUN_LOCK = threading.Lock()


class RunAlreadyInProgressError(Exception):
    """Raised instead of silently blocking/racing when a second
    POST /api/run arrives while one is still in flight."""

# Killing the outer handoff_bridge.py wrapper on timeout does NOT kill the
# real codex/claude child it spawned -- subprocess.run() only signals the
# immediate child, not its descendants, since neither process runs in its
# own process group. So the per-provider budget is delegated to the bridge
# itself via --timeout-seconds, which applies the timeout to the actual
# provider subprocess.run() call and can therefore really terminate it.
# This outer wrapper timeout becomes a hard-kill backstop for cases outside
# provider execution (e.g. the bridge process itself hanging on I/O) --
# generous enough to cover two sequential --timeout-seconds budgets (a
# rate-limited first provider auto-falling-back into a second one that also
# times out), plus real slack: each provider call's save_state()/
# append_current() also goes through handoff_bridge.WriteLock, which alone
# can block up to its own 10s timeout under contention (e.g. a second
# browser tab's /api/run racing this one) -- up to 2x that across both
# calls in a fallback chain, on top of ordinary process-startup overhead.
OUTER_SUBPROCESS_TIMEOUT_SECONDS = PROVIDER_RUN_TIMEOUT_SECONDS * 2 + 60


def classify_run_status(handoff_needed: bool, reason: str) -> str:
    """Map a handoff_bridge.py history record's (handoff_needed, reason) to
    one of the three terminal run states from
    docs/design-system/components.html §3/§9. `reason` always starts with
    one of handoff_bridge.HANDOFF_LABELS or "none" -- classify_handoff()'s
    own contract, enforced by scripts/validate_handoff.py.
    """
    if not handoff_needed:
        return "success"
    if reason.startswith("tool_failure") or reason.startswith("unknown"):
        return "fail"
    return "handoff"


def read_state_dict(workspace: Path) -> dict:
    state_path = workspace / ".handoff" / "state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_state_history(workspace: Path) -> list[dict]:
    return read_state_dict(workspace).get("history", [])


def build_run_prompt(text: str, attachments: list[dict]) -> str:
    """Combine the composer text with any attached files into the single
    string that becomes the provider's actual prompt.

    Without this, an attachment only ever reached the chat log (POST
    /api/chat persists it on the "user" message) -- POST /api/run sent just
    `text`, so a file the user thought they'd attached was never part of
    what the provider actually saw. docs/design-system/wireframes.html
    describes attachments as context for "the next message", so that's the
    contract this restores.
    """
    parts = [text] if text else []
    for attachment in attachments:
        name = attachment.get("name") or attachment.get("path") or "attachment"
        content = attachment.get("content")
        header = f"### Attached file: {name}"
        if content is None:
            parts.append(f"{header}\n(binary or unreadable -- no preview available)")
        else:
            note = " (truncated)" if attachment.get("truncated") else ""
            parts.append(f"{header}{note}\n```\n{content}\n```")
    return "\n\n".join(parts)


def run_provider_via_bridge(
    workspace: Path, provider: str, prompt: str, model: str | None, instruction_type: str
) -> list[dict]:
    """Thin locking wrapper around `_run_provider_via_bridge_locked()`.

    Fails fast with `RunAlreadyInProgressError` instead of silently
    blocking (a provider call can legitimately take up to
    `OUTER_SUBPROCESS_TIMEOUT_SECONDS`) or racing (see `_RUN_LOCK`'s
    comment) when a second call arrives while one is already in flight.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
    try:
        return _run_provider_via_bridge_locked(workspace, provider, prompt, model, instruction_type)
    finally:
        _RUN_LOCK.release()


def _run_provider_via_bridge_locked(
    workspace: Path, provider: str, prompt: str, model: str | None, instruction_type: str
) -> list[dict]:
    """Invoke `handoff_bridge.py run <provider> --execute --auto-fallback`
    against `workspace` and return the new handoff_bridge.py history
    record(s) it appended to .handoff/state.json -- more than one if
    auto-fallback chained into a second provider.

    If the subprocess itself fails before handoff_bridge.py ever gets to
    classify_handoff()/save_state() (e.g. the interpreter can't even start),
    no history record exists to read back -- synthesize one so callers
    always get at least one result to show and persist, instead of silently
    returning nothing.

    Phase 4 (DEC-13/16): before shelling out, check whether this call
    should go over a provider's HTTP API instead -- only when its CLI is
    genuinely absent and a key is saved for it, so every previously-existing
    behavior (a CLI-available provider, or a CLI-missing one with no key
    saved) is completely unchanged by this branch.

    A custom provider (DEC-26, `provider` shaped "custom:<name>") always
    goes through API-key mode -- it has no CLI/binary of its own, so
    none of the cli_available()/auto-fallback logic below applies to it
    at all; this check runs first and returns before any of that.
    """
    if is_custom_provider(provider):
        name = custom_provider_name(provider)
        credential = read_custom_providers().get(name)
        if credential is None:
            return [
                _api_key_mode_error_record(
                    provider, None, instruction_type, f"custom provider {name!r} is not configured (was it deleted?)"
                )
            ]
        return run_provider_via_api_key(workspace, provider, prompt, credential, instruction_type, model)

    if provider == "auto":
        if not any(cli_available(p) for p in PROVIDERS):
            # Only read credentials.json once we already know no CLI is
            # available -- the common case (some CLI installed) never
            # touches disk for a value it would just discard unused.
            credentials = read_credentials()
            api_key_provider = next((p for p in PROVIDERS if p in credentials), None)
            if api_key_provider is None:
                return [
                    _api_key_mode_error_record(
                        # PROVIDERS[0], not the literal "auto" -- the /api/run
                        # handler persists this into the chat log's `provider`
                        # field verbatim (append_chat_message(provider=record
                        # ["provider"])), and "auto" must never end up stored
                        # there (the same invariant the CLI-side synthetic-
                        # record path already enforces for the same reason;
                        # see its own comment a few lines below).
                        PROVIDERS[0],
                        None,
                        instruction_type,
                        "no provider CLI is installed and no API key is configured for any provider -- "
                        "install codex or claude, or open the connection panel to add an API key",
                    )
                ]
            return run_provider_via_api_key(
                workspace, api_key_provider, prompt, credentials[api_key_provider], instruction_type, model
            )
        # At least one CLI exists -- fall through to the existing subprocess
        # path, which already does its own choose_auto_provider() and
        # --auto-fallback among CLI-available providers.
    elif not cli_available(provider):
        # Same reasoning as above: only read credentials.json once the CLI
        # is confirmed unavailable, so the common (CLI installed) case
        # skips the disk read + JSON parse entirely.
        credentials = read_credentials()
        if provider in credentials:
            return run_provider_via_api_key(
                workspace, provider, prompt, credentials[provider], instruction_type, model
            )
        # else (a specific provider with no CLI and no saved key): fall
        # through unchanged -- the subprocess call below fails with
        # FileNotFoundError -> exit_code 127, exactly as it did before this
        # phase existed.

    before = len(read_state_history(workspace))

    # The prompt travels via --prompt-file, not as a trailing argv
    # positional: a long/multi-line prompt as a bare CLI arg risks hitting
    # OS argv-length limits and shows up in the local process list, and
    # (found via a CI-only failure) a bare positional interleaved after
    # `--instruction-type <value>` parses inconsistently across argparse
    # versions -- Python 3.11 rejected it as an unrecognized argument
    # while 3.14 accepted it. A file avoids both problems.
    prompt_fd, prompt_path_str = tempfile.mkstemp(prefix="webui-run-prompt-", suffix=".txt")
    prompt_path = Path(prompt_path_str)
    try:
        with os.fdopen(prompt_fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)

        command = bridge_command_prefix() + [
            "--workspace",
            str(workspace),
            "run",
            provider,
            "--execute",
            "--auto-fallback",
            "--instruction-type",
            instruction_type,
            "--prompt-file",
            str(prompt_path),
            "--timeout-seconds",
            str(PROVIDER_RUN_TIMEOUT_SECONDS),
        ]
        if model:
            # "--model=value", not ["--model", value]: with the latter, a
            # model string that happens to start with "-" would make
            # argparse treat it as the next flag instead of --model's
            # value ("argument --model: expected one argument"). Not
            # reachable through the shipped UI today (it never sends
            # `model`), but --prompt-file/`init ... -- task` already
            # closed the same class of gap elsewhere, so closing it here
            # too rather than leaving it for whenever `model` is wired up.
            command.append(f"--model={model}")

        hit_outer_timeout = False
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                # Without an explicit encoding, subprocess falls back to
                # locale.getpreferredencoding() to decode stdout/stderr --
                # not UTF-8 on a non-UTF-8-locale Windows machine -- and
                # the bridge subprocess's own output can reflect arbitrary
                # provider/prompt content. See run_provider()'s matching
                # fix in handoff_bridge.py for the confirmed crash this
                # class of bug produces.
                encoding="utf-8",
                errors="replace",
                timeout=OUTER_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
            stderr_tail = (result.stderr or "").strip()[-2000:]
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            # The hard-kill backstop actually fired -- the bridge process
            # (and whatever provider subprocess it was waiting on) got
            # killed before it could finish writing/saving anything for
            # that in-flight call. A per-provider --timeout-seconds
            # timeout, by contrast, is caught inside the bridge itself and
            # returns normally with exit_code 124 plus a real saved record,
            # so it never reaches this branch.
            hit_outer_timeout = True
            stderr_tail = f"timed out after {OUTER_SUBPROCESS_TIMEOUT_SECONDS}s"
            exit_code = 124
        except OSError as exc:
            stderr_tail = str(exc)
            exit_code = 127
    finally:
        prompt_path.unlink(missing_ok=True)

    new_records = read_state_history(workspace)[before:]
    if new_records:
        if hit_outer_timeout:
            # This can fire mid-auto-fallback: e.g. codex's own record is
            # already saved but the recursive claude call never got to
            # finish and save its own. Without this, the caller would
            # silently see only the first record and have no idea a second
            # attempt was even in flight.
            #
            # Phase 5 bug, found in review: this used to be a hardcoded
            # "claude" if codex else "codex" binary guess -- other_provider()'s
            # own replacement (next_provider()) generalized the *real*
            # fallback logic in handoff_bridge.py, but this webui-local
            # guess was never updated to match. With PROVIDERS now 3-wide,
            # the recursive fallback call handoff_bridge.py actually made
            # is next_available_provider(new_records[-1]["provider"]) (a
            # second review found run_provider() itself needed the
            # CLI-availability-aware variant, not plain next_provider(),
            # so this guess has to match exactly that or it can name the
            # wrong provider whenever the naive next-in-order pick isn't
            # installed) -- e.g. a claude run needing handoff recurses
            # into gemini, not codex, and skips further if gemini isn't
            # installed either. Reusing the same function here (rather
            # than reimplementing the guess) is the only way this stays
            # correct if PROVIDERS' order or installed set ever changes.
            timed_out_provider = _bridge_next_provider(new_records[-1]["provider"])
            new_records.append(
                {
                    "provider": timed_out_provider,
                    "model": model or "app-selected default",
                    "instruction_type": instruction_type,
                    "exit_code": exit_code,
                    "session_id": None,
                    "final_text": f"Timed out after {OUTER_SUBPROCESS_TIMEOUT_SECONDS}s waiting for a reply.",
                    "handoff_needed": True,
                    "reason": "tool_failure: subprocess did not produce a history record (exit 124)",
                    "run_dir": None,
                }
            )
        return new_records
    # No history record exists to read the real provider back from, so
    # "auto" (schema: docs/webui-chat-storage.md) must still be resolved
    # here -- otherwise a synthetic record could persist "auto" as a
    # chat-log `provider` value, which callers never expect to see.
    resolved_provider = (
        _bridge_resolve_auto_provider(workspace) if provider == "auto" else provider
    )
    return [
        {
            "provider": resolved_provider,
            "model": model or "app-selected default",
            "instruction_type": instruction_type,
            "exit_code": exit_code,
            "session_id": None,
            "final_text": stderr_tail or f"handoff_bridge.py run exited {exit_code} with no output",
            "handoff_needed": True,
            "reason": f"tool_failure: subprocess did not produce a history record (exit {exit_code})",
            "run_dir": None,
        }
    ]

