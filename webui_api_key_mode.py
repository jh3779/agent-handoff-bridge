"""API-key mode (Phase 4, docs/design-system/roadmap.md, SCR-06/
components.html §14, resolves CFL-12). A provider with no local CLI can be
reached over its vendor HTTP API directly instead, using a saved key
(webui_credentials.py) -- full tool-use turn loop
(read_file/write_file/edit_file/run_shell), not just plain chat, as of
CFL-17/DEC-21. Codex/Claude/Gemini each get their own call_X_api()
function (call_openai_responses_api/call_anthropic_messages_api/
call_gemini_api) with the same request/response shape, since this is one
of the places this project deliberately keeps per-provider logic
symmetric rather than a single branchy function.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from webui_chat_storage import list_available_months, read_month_messages
from webui_common import WorkspaceError, utc_now
from webui_workspace import read_file_preview, safe_join


# Deliberately empty for *both* providers, not just OpenAI/Codex: an
# earlier version hardcoded a Claude default on the reasoning that model
# IDs from this session's own environment context were "safe to default
# to" -- a real review round pointed out that's an internal, undated
# assumption, not a citable, externally-verifiable source the way
# docs/research.md/docs/research-api-key-mode.md's other claims are, and
# a wrong/deprecated default would silently break every CLI-less Claude
# user with no clear model config error to point at. Requiring an
# explicit model from both providers (via POST /api/provider-key) is the
# more honest, defensible choice than guessing one that can't be sourced.
API_KEY_MODE_DEFAULT_MODELS: dict[str, str] = {}

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# {model} is the bare model ID (e.g. "gemini-2.5-flash"), no "models/"
# prefix -- confirmed against https://ai.google.dev/api/generate-content.
# Auth via the `x-goog-api-key` header (also confirmed there), not the
# `?key=` query-string form the same docs mention as an alternative --
# a header keeps this consistent with the other two providers (both
# header-based) and never puts the secret in a URL that could end up in
# a proxy/log line.
GEMINI_GENERATE_CONTENT_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_KEY_MODE_TIMEOUT_SECONDS = 120
API_KEY_MODE_MAX_HISTORY_MESSAGES = 20
API_KEY_MODE_MAX_TOKENS = 4096
# Small on purpose: POST /api/provider-key's validation call only needs a
# single short word back to prove the key/model combination actually
# works -- it is never a real chat turn.
API_KEY_VALIDATION_MAX_TOKENS = 16


def build_api_message_history(workspace: Path, prompt: str, now: datetime) -> list[dict]:
    """Anthropic's Messages API and OpenAI's Responses API are both
    stateless per call (docs/research-api-key-mode.md) -- there is no
    `codex exec resume`/`claude --resume` equivalent, so conversation
    continuity has to come from resending prior turns ourselves.

    Scans months backward (newest first, merged into chronological order),
    same pattern as collect_recent_turns() -- a bare
    read_month_messages(workspace, month_key(now)) would silently drop all
    prior context on the first message(s) of a new UTC month, the same
    class of bug Phase 3 already had to fix once for that other function.
    Stops once API_KEY_MODE_MAX_HISTORY_MESSAGES raw messages are
    collected, to bound how many months a long-lived project ever needs
    to read.

    Drops the bare current-turn log entry in favor of `prompt`
    (build_run_prompt()'s text+attachments string, which the bare log
    entry doesn't carry -- POST /api/chat always runs before POST
    /api/run, so that entry already exists by the time this is called).

    Anthropic's Messages API requires strict user/assistant alternation.
    A single CLI turn can produce two consecutive "agent" chat-log entries
    when --auto-fallback chains providers (_run_provider_via_bridge_locked()'s
    own docstring documents this, and POST /api/run appends one "agent"
    message per resulting record) -- if that workspace later loses its
    CLI(s) and falls into API-key mode within the same history window,
    replaying those as two separate assistant turns would violate
    alternation and fail with a 400. Consecutive same-role entries
    (including the final `prompt` against whatever role ends up last) are
    merged (text joined) instead of kept separate, so replay can never
    violate alternation regardless of how the log was produced.
    """
    turns: list[dict] = []
    for month in reversed(list_available_months(workspace)):
        turns = [m for m in read_month_messages(workspace, month) if m.get("role") in ("user", "agent")] + turns
        if len(turns) >= API_KEY_MODE_MAX_HISTORY_MESSAGES:
            break
    if turns and turns[-1].get("role") == "user":
        turns = turns[:-1]
    turns = turns[-API_KEY_MODE_MAX_HISTORY_MESSAGES:]

    messages: list[dict] = []

    def _append(role: str, content: str) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{content}".strip()
        else:
            messages.append({"role": role, "content": content})

    for turn in turns:
        _append("user" if turn["role"] == "user" else "assistant", turn.get("text") or "")
    _append("user", prompt)
    return messages


# docs/research-api-key-mode.md notes that the official SDKs auto-retry
# transient failures (connection errors, rate limits, 5xx) with backoff,
# honoring `retry-after` -- a hand-rolled urllib client doesn't get that
# for free, so a small bounded retry is applied here rather than surfacing
# every transient blip as a user-visible chat failure the CLI path
# wouldn't have.
API_KEY_MODE_MAX_RETRIES = 2  # total attempts = 1 + this
API_KEY_MODE_RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 529}
API_KEY_MODE_RETRY_BASE_DELAY_SECONDS = 1.0


def _sleep(seconds: float) -> None:
    """Seam so tests can avoid real delays -- same reasoning as
    _http_post_json()'s seam over urllib."""
    time.sleep(seconds)


def _retry_delay_seconds(exc: BaseException, attempt: int) -> float:
    retry_after = getattr(exc, "headers", None) and exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass  # not a plain seconds value (e.g. an HTTP-date) -- fall through to the default backoff
    return API_KEY_MODE_RETRY_BASE_DELAY_SECONDS * attempt


def _http_post_json(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, dict]:
    """Thin seam around urllib so tests can substitute a fake transport
    instead of making a real network call -- the same reason
    _run_provider_via_bridge_locked() shells out to a fake `codex`/`claude`
    script in tests rather than calling a real provider.

    Retries a rate-limited/server-error/network-transient failure up to
    API_KEY_MODE_MAX_RETRIES times before giving up -- everything else
    (auth errors, bad request, header rejection, a malformed response
    body) returns immediately since retrying wouldn't change the outcome.
    """
    encoded = json.dumps(body).encode("utf-8")
    attempt = 0
    while True:
        request = urllib.request.Request(url, data=encoded, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https:// constants above)
                raw = response.read().decode("utf-8")
                try:
                    return response.status, json.loads(raw)
                except json.JSONDecodeError:
                    # A 200 with an unparseable body (e.g. a proxy sitting
                    # in front of the real API) -- status 0 (never a real
                    # HTTP status) so callers' `if status != 200` branch
                    # still treats this as a failure instead of silently
                    # reading an empty reply out of the malformed body.
                    return 0, {"error": {"type": "api_error", "message": f"non-JSON response body: {raw[:500]}"}}
        except urllib.error.HTTPError as exc:
            if exc.code in API_KEY_MODE_RETRY_STATUS_CODES and attempt < API_KEY_MODE_MAX_RETRIES:
                attempt += 1
                _sleep(_retry_delay_seconds(exc, attempt))
                continue
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, {"error": {"type": "api_error", "message": payload or exc.reason}}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # ValueError (below) is deliberately NOT included here even
            # though json.JSONDecodeError (handled above, inside the try
            # block) is technically a ValueError subclass -- ordering
            # matters: that inner try/except already claims it before
            # execution would ever reach this or the next clause.
            if attempt < API_KEY_MODE_MAX_RETRIES:
                attempt += 1
                _sleep(_retry_delay_seconds(exc, attempt))
                continue
            raise
        except ValueError:
            # http.client raises a bare ValueError (not HTTPError/URLError)
            # for a header value containing forbidden characters -- e.g. a
            # saved API key with an embedded CR/LF makes it straight into
            # the `x-api-key`/`Authorization` header unescaped. httplib's
            # own exception text embeds the offending header VALUE
            # verbatim (the key itself, here), so unlike every other
            # branch in this function -- which forwards the vendor's real
            # response/exception text -- that text must never be returned
            # as-is. Retrying wouldn't help (the key is still malformed),
            # so this returns immediately rather than going through the
            # retry loop above.
            return 0, {
                "error": {
                    "type": "invalid_request",
                    "message": "request headers were rejected -- the API key or model contains characters not allowed in an HTTP header value",
                }
            }


# --- Agentic tool loop (CFL-17/DEC-21) ------------------------------------
#
# docs/research-api-key-mode.md's "Open Scope Question" laid out three
# sizes of API-key mode; Phase 4 shipped the smallest (chat-only, DEC-13),
# explicitly deferring the largest -- a bridge-built tool loop giving
# file-edit/shell-exec parity with CLI mode -- as CFL-17, since a
# bridge-controlled shell-exec tool is "a new sandboxing/security surface
# this project doesn't have today." The design interview that resolved
# CFL-17 chose to build file tools and the shell tool together in one
# pass, and to extend DEC-02 (confirm only the first send per session)
# to cover every tool call this loop makes rather than adding a stronger
# per-call confirmation -- i.e. once a session's first message is
# confirmed, this loop can read/write/edit any file under the workspace
# and run any shell command in it, with no further per-call gate. This
# mirrors the trust level already extended to CLI mode: `codex`/`claude`
# subprocesses invoked elsewhere in this file already have full local
# shell access when they actually run, so a shell tool with no allowlist
# is not a new tier of trust, just a bridge-built equivalent of what CLI
# mode already does. `run_shell` sets `cwd=workspace` (where it starts,
# matching CLI mode's own subprocess cwd) but is not a sandbox -- a
# review round flagged an earlier draft of this comment for implying
# otherwise. An absolute path or `..` still reaches anywhere the user
# account itself can, the same as it would for a real terminal, or for
# `codex`/`claude` run directly outside this bridge.

# Bounds how many tool calls a single turn can make before this loop gives
# up and returns whatever text exists -- without this, a confused model
# issuing tool calls that never satisfy it could loop indefinitely,
# burning API cost with no user-visible progress. 15 is generous enough
# for a real multi-step edit (a handful of reads, a couple of edits, one
# or two shell commands to verify) while still being a hard stop, not a
# tuned/benchmarked number.
MAX_TOOL_ITERATIONS = 15

# subprocess.run(..., shell=True) timeout for the run_shell tool -- reuses
# API_KEY_MODE_TIMEOUT_SECONDS' value rather than inventing a second
# magic number, since both bound "how long this project will wait on one
# step of an API-key-mode turn before giving up."
TOOL_EXEC_TIMEOUT_SECONDS = API_KEY_MODE_TIMEOUT_SECONDS

# A tool result this long would dominate the next call's context (and
# cost) for little benefit -- long build/test output is usually only
# interesting at the head or tail, and a truncation note (never a silent
# cut) tells the model and the persisted chat transcript alike that this
# happened.
TOOL_OUTPUT_MAX_CHARS = 4000

# One list of {name, description, params, required} feeds both vendors'
# schema builders below -- Anthropic's input_schema and OpenAI's
# parameters shapes differ, but the underlying tool set must never drift
# between them, so it's defined once here instead of twice.
_TOOL_SPECS: list[dict] = [
    {
        "name": "read_file",
        "description": "Read a text file's contents from the workspace. path is relative to the workspace root.",
        "params": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
        "required": ["path"],
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing one in the workspace with the given content.",
        "params": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "Full contents to write."},
        },
        "required": ["path", "content"],
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_string with new_string in an existing file. "
            "old_string must match exactly once in the file -- include enough surrounding "
            "context to make it unique, or the edit is rejected."
        ),
        "params": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "old_string": {"type": "string", "description": "Exact text to replace; must appear exactly once in the file."},
            "new_string": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old_string", "new_string"],
    },
    {
        "name": "run_shell",
        "description": "Run a shell command in the workspace directory. Returns its exit code, stdout, and stderr.",
        "params": {"command": {"type": "string", "description": "Shell command to execute."}},
        "required": ["command"],
    },
]


def anthropic_tool_definitions() -> list[dict]:
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "input_schema": {"type": "object", "properties": spec["params"], "required": spec["required"]},
        }
        for spec in _TOOL_SPECS
    ]


def openai_tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "properties": spec["params"],
                "required": spec["required"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        for spec in _TOOL_SPECS
    ]


def gemini_tool_definitions() -> list[dict]:
    # One Tool object holding every functionDeclaration, not one Tool per
    # function -- confirmed against https://ai.google.dev/api/generate-content
    # (a request's `tools` array example groups all declarations under a
    # single `functionDeclarations` list). No `additionalProperties`/
    # `strict` here -- those are OpenAI-specific "strict mode" fields
    # Gemini's function-calling schema doesn't define.
    return [
        {
            "functionDeclarations": [
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": {"type": "object", "properties": spec["params"], "required": spec["required"]},
                }
                for spec in _TOOL_SPECS
            ]
        }
    ]


def _tool_read_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    try:
        result = read_file_preview(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    content = result["content"]
    # MAX_FILE_BYTES (read_file_preview()'s own cap, ~256KB) bounds how
    # much this project will ever read off disk for a preview -- a
    # review round pointed out that's not the same as bounding how much
    # ends up in a single tool_result feeding the *next* API call, which
    # is what TOOL_OUTPUT_MAX_CHARS is actually for (same reasoning
    # _tool_run_shell() already applies to its own output). Without this,
    # a model reading a handful of large files in one turn could still
    # blow past the context/cost this constant exists to bound.
    truncated = result["truncated"] or len(content) > TOOL_OUTPUT_MAX_CHARS
    if len(content) > TOOL_OUTPUT_MAX_CHARS:
        content = content[:TOOL_OUTPUT_MAX_CHARS]
    note = "\n(truncated)" if truncated else ""
    return f"{content}{note}"


def _tool_write_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    content = tool_input.get("content")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    if not isinstance(content, str):
        return "error: 'content' is required"
    try:
        target = safe_join(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: failed to write {path}: {exc}"
    return f"wrote {len(content)} characters to {path}"


def _tool_edit_file(workspace: Path, tool_input: dict) -> str:
    path = tool_input.get("path")
    old_string = tool_input.get("old_string")
    new_string = tool_input.get("new_string")
    if not isinstance(path, str) or not path:
        return "error: 'path' is required"
    if not isinstance(old_string, str) or not old_string:
        return "error: 'old_string' is required and must be non-empty"
    if not isinstance(new_string, str):
        return "error: 'new_string' is required"
    try:
        target = safe_join(workspace, path)
    except WorkspaceError as exc:
        return f"error: {exc}"
    if not target.exists() or not target.is_file():
        return f"error: not a file: {path}"
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"error: failed to read {path}: {exc}"
    count = current.count(old_string)
    if count == 0:
        return f"error: old_string not found in {path}"
    if count > 1:
        return f"error: old_string matches {count} locations in {path} -- must match exactly once, add more surrounding context"
    try:
        target.write_text(current.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return f"error: failed to write {path}: {exc}"
    return f"edited {path}"


def _tool_run_shell(workspace: Path, tool_input: dict) -> str:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return "error: 'command' is required"
    # A review round noted this timeout only guarantees killing the
    # immediate subprocess (Python's own TimeoutExpired handling), not a
    # whole process tree -- a command that backgrounds work or forks
    # descendants could leave some of them running past
    # TOOL_EXEC_TIMEOUT_SECONDS. Cross-platform process-group cleanup
    # (os.killpg on POSIX, a job object on Windows) is a real, larger
    # change than this fix round's scope; documented here as a known,
    # accepted gap rather than silently left unstated -- same posture
    # DEC-21 already takes toward run_shell having no command allowlist.
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            # Without an explicit encoding, subprocess falls back to
            # locale.getpreferredencoding() to decode stdout/stderr -- not
            # UTF-8 on a non-UTF-8-locale Windows machine -- and a model's
            # shell command (or the workspace files it reads) can easily
            # produce non-ASCII output. See run_provider()'s matching fix
            # in handoff_bridge.py for the confirmed crash this class of
            # bug produces elsewhere in this project.
            encoding="utf-8",
            errors="replace",
            timeout=TOOL_EXEC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {TOOL_EXEC_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"error: failed to run command: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    truncated = len(output) > TOOL_OUTPUT_MAX_CHARS
    if truncated:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n(output truncated)"
    return f"exit code: {result.returncode}\n{output}"


_TOOL_EXECUTORS = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "run_shell": _tool_run_shell,
}


def execute_tool_call(workspace: Path, name: str, tool_input: dict) -> str:
    """Never raises -- a bug in one tool executor, or a malformed
    `tool_input` from the model, must degrade to an error string the
    model can see and react to, not crash the turn loop mid-conversation."""
    executor = _TOOL_EXECUTORS.get(name)
    if executor is None:
        return f"error: unknown tool '{name}'"
    try:
        return executor(workspace, tool_input if isinstance(tool_input, dict) else {})
    except Exception as exc:  # noqa: BLE001 -- see docstring
        return f"error: tool '{name}' raised an unexpected exception: {exc}"


def _escape_fence(text: str) -> str:
    # webui/app.js's DEC-03 renderer matches exactly ``` as a fence
    # delimiter (renderTextWithCodeBlocks()'s regex has no longer-fence
    # escape hatch to fall back to) -- a review round pointed out that
    # if tool output or arguments ever contain their own ``` run (a file
    # whose content includes a fenced block, a shell command that prints
    # one), this project's only audit trail for tool activity could be
    # cut off mid-way, hiding what actually ran. A zero-width space
    # between each backtick in any 3+ run breaks the literal-```
    # match without being visible to a human reading the transcript.
    return re.sub(r"`{3,}", lambda m: "\u200b".join(m.group(0)), text)


def _truncate_for_transcript(text: str) -> str:
    # TOOL_OUTPUT_MAX_CHARS already bounds what execute_tool_call()'s
    # *results* feed into the next API call (read_file/run_shell), but a
    # review round found the *arguments* side had no equivalent cap --
    # write_file's `content` or edit_file's `new_string` can be
    # arbitrarily long, and those land in the transcript verbatim via
    # json.dumps(tool_input). A large-but-completely-normal file write
    # would otherwise inflate every subsequent API call's context (and
    # this project's persisted chat log) for no benefit -- the file
    # itself is still on disk in full; the transcript only needs to show
    # that the write happened, not replay the entire payload.
    if len(text) > TOOL_OUTPUT_MAX_CHARS:
        return text[:TOOL_OUTPUT_MAX_CHARS] + "... (truncated for transcript)"
    return text


def _tool_call_transcript_block(name: str, raw_args: str, result_text: str) -> str:
    # Reuses DEC-03 (fenced ```code``` blocks are the only markdown this
    # project's chat bubbles render specially) instead of inventing a new
    # message role/schema just to show tool activity -- this folds
    # straight into final_text and renders with zero frontend changes.
    safe_args = _escape_fence(_truncate_for_transcript(raw_args))
    safe_result = _escape_fence(_truncate_for_transcript(result_text))
    return f"```\n$ {name}({safe_args})\n{safe_result}\n```"


def _error_with_transcript(transcript_parts: list[str], message: str) -> dict:
    # A review round found that a mid-loop API failure (network error,
    # non-200) used to discard `transcript_parts` outright -- if a tool
    # with real side effects (write_file, edit_file, run_shell) already
    # ran on an earlier iteration before the *next* API call failed, the
    # record of what actually executed vanished along with it. DEC-21's
    # whole rationale for skipping a per-tool-call confirmation is that
    # tool activity stays visible in the persisted chat log after the
    # fact -- that guarantee is void if a failure can silently erase it,
    # so any already-accumulated transcript is prepended to the error
    # message here rather than dropped.
    if not transcript_parts:
        return {"ok": False, "message": message}
    transcript_so_far = "\n\n".join(transcript_parts)
    return {"ok": False, "message": f"{transcript_so_far}\n\n{message}"}


def call_anthropic_messages_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Returns {"ok": True, "text": str} or {"ok": False, "message": str} --
    `message` is built only from the response body/exception text, never
    from `api_key`, so a saved key can never leak into a chat log entry via
    an error message. If one or more tools already executed before a
    later call in the same turn failed, `message` is prefixed with the
    transcript of what ran (_error_with_transcript()) -- a mid-turn API
    failure must never silently erase the record of a write_file/
    edit_file/run_shell that already had a real effect.

    Runs the tool-use turn loop (CFL-17/DEC-21; request/response shapes
    -- tools[].input_schema, tool_choice.disable_parallel_tool_use,
    tool_use/tool_result content blocks -- confirmed against
    https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
    before implementing, not assumed): sends `messages` plus this
    project's tool schemas, executes any `tool_use` block(s) Claude
    returns via execute_tool_call(), feeds the results back, and repeats
    until Claude stops requesting tools or MAX_TOOL_ITERATIONS actual
    tool calls have been executed (not just HTTP round trips -- a
    self-review round pointed out that bounding round trips alone
    doesn't bound cost/runaway-loop risk if a single response can carry
    more than one call). A response with no tool_use block returns on
    the first iteration -- the exact single-call behavior this function
    had before CFL-17, so a plain chat turn (no tool calls) is
    unaffected.
    `tool_choice.disable_parallel_tool_use` asks Claude for one tool
    call per turn -- simpler to log and reason about than unwinding
    several simultaneous tool calls, at the cost of an extra round trip
    for a task that could otherwise batch calls -- but this is a hint,
    not a guarantee the API enforces, so every tool_use block in a
    response is still executed (each needs a matching tool_result or
    the next call would 400 on mismatched IDs), matching the defensive
    posture call_openai_responses_api() already needs for the same
    reason on its side.
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    working_messages = list(messages)
    transcript_parts: list[str] = []
    tools = anthropic_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {
            "model": model,
            "max_tokens": API_KEY_MODE_MAX_TOKENS,
            "messages": working_messages,
            "tools": tools,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        try:
            status, data = _http_post_json(ANTHROPIC_MESSAGES_URL, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # _http_post_json() already retried transient failures
            # (API_KEY_MODE_MAX_RETRIES times) before re-raising this.
            return _error_with_transcript(transcript_parts, f"network error calling Anthropic Messages API: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_type = (error or {}).get("type", "api_error")
            error_message = (error or {}).get("message", json.dumps(data, ensure_ascii=False))
            return _error_with_transcript(transcript_parts, f"Anthropic API error ({status} {error_type}): {error_message}")
        content = data.get("content", []) if isinstance(data, dict) else []
        text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        if text:
            transcript_parts.append(text)
        tool_use_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if not tool_use_blocks:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        working_messages.append({"role": "assistant", "content": content})
        tool_results: list[dict] = []
        for tool_use in tool_use_blocks:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            tool_name = tool_use.get("name", "")
            tool_input = tool_use.get("input") or {}
            result_text = execute_tool_call(workspace, tool_name, tool_input)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(tool_name, json.dumps(tool_input, ensure_ascii=False), result_text))
            tool_results.append({"type": "tool_result", "tool_use_id": tool_use.get("id"), "content": result_text})
        working_messages.append({"role": "user", "content": tool_results})


def call_openai_responses_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Same contract and same tool-use turn loop as
    call_anthropic_messages_api() -- see its docstring for the shared
    reasoning (MAX_TOOL_ITERATIONS bounds actual tool *executions*, not
    HTTP round trips -- a response's `output` array can carry more than
    one call, so counting round trips alone wouldn't actually bound
    cost/runaway-loop risk). Request/response shapes -- tools[].type=
    "function"/parameters/strict, function_call output items,
    function_call_output input items -- confirmed against
    https://developers.openai.com/api/docs/guides/function-calling
    before implementing, not assumed. Responses API input items use
    OpenAI's {role, content} shape too, so `messages` (already built for
    the Anthropic call) is reused as-is for the initial `input`.

    No Responses API equivalent of `disable_parallel_tool_use` turned up
    in that research, so a response's `output` array is handled as
    documented: it may contain more than one `function_call` item, and
    each is executed and given a matching `function_call_output` before
    the next call -- OpenAI's contract otherwise has no way to answer
    only some of a batch.
    """
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    working_input = list(messages)
    transcript_parts: list[str] = []
    tools = openai_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {"model": model, "input": working_input, "tools": tools}
        try:
            status, data = _http_post_json(OPENAI_RESPONSES_URL, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return _error_with_transcript(transcript_parts, f"network error calling OpenAI Responses API: {exc}")
        except json.JSONDecodeError as exc:
            return _error_with_transcript(transcript_parts, f"OpenAI API returned a non-JSON response: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_type = (error or {}).get("type", "api_error") if isinstance(error, dict) else "api_error"
            error_message = (
                (error or {}).get("message", json.dumps(data, ensure_ascii=False))
                if isinstance(error, dict)
                else json.dumps(data, ensure_ascii=False)
            )
            return _error_with_transcript(transcript_parts, f"OpenAI API error ({status} {error_type}): {error_message}")
        output = data.get("output", []) if isinstance(data, dict) else []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text = block.get("text", "")
                    if text:
                        transcript_parts.append(text)
        function_calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
        if not function_calls:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        for call in function_calls:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            working_input.append(call)
            name = call.get("name", "")
            raw_args = call.get("arguments", "{}")
            try:
                tool_input = json.loads(raw_args) if isinstance(raw_args, str) else {}
            except json.JSONDecodeError:
                tool_input = {}
            result_text = execute_tool_call(workspace, name, tool_input)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(name, raw_args if isinstance(raw_args, str) else "{}", result_text))
            working_input.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": result_text})


def _gemini_contents_from_messages(messages: list[dict]) -> list[dict]:
    # Gemini's Content shape ({"role", "parts": [{"text": ...}]}) is not
    # the {"role", "content": "..."} shape build_api_message_history()
    # builds for Anthropic/OpenAI -- translated here rather than changing
    # that shared function, since Anthropic and OpenAI's Responses API
    # both genuinely do accept {"role", "content"} directly. Gemini has
    # no "assistant" role; "model" is the equivalent (confirmed against
    # https://ai.google.dev/api/generate-content).
    return [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages]


def call_gemini_api(api_key: str, model: str, messages: list[dict], workspace: Path) -> dict:
    """Same {"ok": True, "text": str} / {"ok": False, "message": str}
    contract and the same tool-use turn loop shape as
    call_anthropic_messages_api()/call_openai_responses_api() -- see
    that function's docstring for the shared MAX_TOOL_ITERATIONS/
    defensive-every-call-block reasoning, which applies here unchanged.

    Request/response shapes (contents[].role/parts, tools[].
    functionDeclarations, a model turn's functionCall part, sending a
    result back as a functionResponse part with role "user", auth via
    the x-goog-api-key header) confirmed against
    https://ai.google.dev/api/generate-content and
    https://ai.google.dev/gemini-api/docs/generate-content/function-calling
    before implementing, not assumed -- DEC-25
    (docs/design-system/flutter-mapping.html#s1c), resolving DEC-15's
    deliberately-left-open "should Gemini get API-key mode too" question.

    Unlike a functionCall's `args` (already a plain object per Gemini's
    schema), a functionResponse's `response` field must itself be a JSON
    *object*, not a bare string -- execute_tool_call() returns plain text
    (the shared, provider-agnostic executor contract all three providers
    use), so it is wrapped as {"result": <text>} here rather than
    changing that shared contract for one provider's stricter schema.
    """
    url = GEMINI_GENERATE_CONTENT_URL_TEMPLATE.format(model=model)
    headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
    working_contents = _gemini_contents_from_messages(messages)
    transcript_parts: list[str] = []
    tools = gemini_tool_definitions()
    tool_calls_executed = 0
    while True:
        body = {"contents": working_contents, "tools": tools}
        try:
            status, data = _http_post_json(url, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return _error_with_transcript(transcript_parts, f"network error calling Gemini API: {exc}")
        if status != 200:
            error = data.get("error") if isinstance(data, dict) else None
            error_status = (error or {}).get("status", "api_error") if isinstance(error, dict) else "api_error"
            error_message = (
                (error or {}).get("message", json.dumps(data, ensure_ascii=False))
                if isinstance(error, dict)
                else json.dumps(data, ensure_ascii=False)
            )
            return _error_with_transcript(transcript_parts, f"Gemini API error ({status} {error_status}): {error_message}")
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not candidates:
            # No candidate at all -- most commonly a blocked prompt
            # (promptFeedback.blockReason), never something to silently
            # treat as an empty-but-successful reply.
            block_reason = (data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
            if block_reason:
                return _error_with_transcript(transcript_parts, f"Gemini blocked the prompt: {block_reason}")
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
        if text:
            transcript_parts.append(text)
        function_calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]
        if not function_calls:
            return {"ok": True, "text": "\n\n".join(transcript_parts)}
        working_contents.append({"role": "model", "parts": parts})
        response_parts: list[dict] = []
        for call in function_calls:
            if tool_calls_executed >= MAX_TOOL_ITERATIONS:
                transcript_parts.append(
                    f"(stopped after {MAX_TOOL_ITERATIONS} tool calls in one turn -- send another message to continue)"
                )
                return {"ok": True, "text": "\n\n".join(transcript_parts)}
            name = call.get("name", "")
            args = call.get("args") or {}
            result_text = execute_tool_call(workspace, name, args)
            tool_calls_executed += 1
            transcript_parts.append(_tool_call_transcript_block(name, json.dumps(args, ensure_ascii=False), result_text))
            function_response = {"name": name, "response": {"result": result_text}}
            if "id" in call:
                # Only some models/responses include this (confirmed:
                # "now always returned... for Gemini 3 models," implying
                # earlier models may omit it) -- echoed back only when
                # present, never fabricated.
                function_response["id"] = call["id"]
            response_parts.append({"functionResponse": function_response})
        working_contents.append({"role": "user", "parts": response_parts})


def validate_provider_api_key(provider: str, api_key: str, model: str) -> dict:
    """Makes one real, minimal, tool-free call to the provider's own API
    with `api_key`/`model` to confirm the key actually works -- POST
    /api/provider-key calls this before save_credential() ever writes a
    non-empty key to disk, so a saved-but-wrong key is never trusted on
    the strength of its shape alone. Same {"ok": True, "text": str} /
    {"ok": False, "message": str} contract as
    call_anthropic_messages_api()/call_openai_responses_api() (`message`
    never contains `api_key`, same invariant), but deliberately skips
    their tool-use turn loop entirely: this has no workspace to act on
    and no reason to grant tool access just to check a key, so it is a
    single HTTP call with no `tools` in the request body at all.
    """
    ping_message = [{"role": "user", "content": "Reply with only the single word: ok"}]
    if provider == "claude":
        url = ANTHROPIC_MESSAGES_URL
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        body = {"model": model, "max_tokens": API_KEY_VALIDATION_MAX_TOKENS, "messages": ping_message}
    elif provider == "gemini":
        url = GEMINI_GENERATE_CONTENT_URL_TEMPLATE.format(model=model)
        headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
        body = {
            "contents": _gemini_contents_from_messages(ping_message),
            "generationConfig": {"maxOutputTokens": API_KEY_VALIDATION_MAX_TOKENS},
        }
    else:
        url = OPENAI_RESPONSES_URL
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        body = {"model": model, "input": ping_message, "max_output_tokens": API_KEY_VALIDATION_MAX_TOKENS}
    try:
        status, data = _http_post_json(url, headers, body, API_KEY_MODE_TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"ok": False, "message": f"network error validating {provider} API key: {exc}"}
    if status != 200:
        error = data.get("error") if isinstance(data, dict) else None
        # Gemini's error shape uses `status` (a string enum like
        # "INVALID_ARGUMENT"/"PERMISSION_DENIED"), not `type` --
        # confirmed against https://ai.google.dev/api/generate-content's
        # error examples.
        error_type = (
            (error or {}).get("status" if provider == "gemini" else "type", "api_error")
            if isinstance(error, dict)
            else "api_error"
        )
        error_message = (
            (error or {}).get("message", json.dumps(data, ensure_ascii=False))
            if isinstance(error, dict)
            else json.dumps(data, ensure_ascii=False)
        )
        return {"ok": False, "message": f"{provider} API key validation failed ({status} {error_type}): {error_message}"}
    text = ""
    if provider == "claude":
        content = data.get("content", []) if isinstance(data, dict) else []
        text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    elif provider == "gemini":
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if candidates:
            content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
            for part in content.get("parts", []) if isinstance(content, dict) else []:
                if isinstance(part, dict) and "text" in part:
                    text += part["text"]
    else:
        for item in data.get("output", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text += block.get("text", "")
    return {"ok": True, "text": text.strip() or "(empty response)"}


def run_provider_via_api_key(
    workspace: Path,
    provider: str,
    prompt: str,
    credential: dict,
    instruction_type: str,
    model_override: str | None = None,
) -> list[dict]:
    """API-key-mode equivalent of _run_provider_via_bridge_locked() -- same
    return shape (one record, same keys) so it flows unchanged into
    classify_run_status()/append_chat_message(). `session_id` and `run_dir`
    are always None: there is no provider-managed session and no local run
    directory in this mode.

    Deliberately does not touch .handoff/state.json/current.md -- those are
    the CLI-handoff-specific durable state files (docs/architecture.md's
    "State Boundaries"), and API-key mode has no CLI session or auto-
    fallback-to-the-other-provider concept for them to record. What
    started as chat-only (DEC-13) now also runs the tool-use turn loop
    (CFL-17/DEC-21, inside call_anthropic_messages_api()/
    call_openai_responses_api() themselves) -- this function's own
    contract doesn't change either way, since a plain chat turn is just
    the loop's zero-tool-calls case.

    `model_override` mirrors CLI mode's per-call --model: takes priority
    over the model saved alongside the credential when the caller supplies
    one (not reachable through the shipped composer UI today, same as CLI
    mode's `model` parameter -- see run_provider_via_bridge()'s docstring).
    """
    model = model_override or credential.get("model") or API_KEY_MODE_DEFAULT_MODELS.get(provider)
    if not model:
        return [
            _api_key_mode_error_record(
                provider,
                None,
                instruction_type,
                f"{provider} API-key mode has no model configured -- set one when saving the API key "
                "(no built-in default exists for this provider; see docs/research-api-key-mode.md)",
            )
        ]
    messages = build_api_message_history(workspace, prompt, utc_now())
    callers = {
        "claude": call_anthropic_messages_api,
        "codex": call_openai_responses_api,
        "gemini": call_gemini_api,
    }
    result = callers[provider](credential["key"], model, messages, workspace)
    if not result["ok"]:
        return [_api_key_mode_error_record(provider, model, instruction_type, result["message"])]
    return [
        {
            "provider": provider,
            "model": model,
            "instruction_type": instruction_type,
            "exit_code": 0,
            "session_id": None,
            "final_text": result["text"] or "(empty response)",
            "handoff_needed": False,
            "reason": "none",
            "run_dir": None,
        }
    ]


def _api_key_mode_error_record(provider: str, model: str | None, instruction_type: str, message: str) -> dict:
    return {
        "provider": provider,
        "model": model or "app-selected default",
        "instruction_type": instruction_type,
        "exit_code": 1,
        "session_id": None,
        "final_text": message,
        "handoff_needed": True,
        "reason": f"tool_failure: api_key_mode: {message}",
        "run_dir": None,
    }

