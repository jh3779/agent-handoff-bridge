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

Structure audit (2026-08-15): this file used to be a single 2600+ line
module. The domain logic now lives in sibling webui_*.py modules --
webui_common.py (subprocess boundary + shared utilities),
webui_workspace.py (file tree/preview, workspace validation),
webui_chat_storage.py (chat history + recent-workspaces registry),
webui_credentials.py (API-key storage), webui_api_key_mode.py (the
CLI-less provider HTTP clients + tool loop), webui_bridge_run.py
(dispatching a real run, CLI or API-key mode). This file keeps the HTTP
routing layer (build_handler), AppState/Api, and the process entry point.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from handoff_bridge import PROVIDERS

from webui_api_key_mode import validate_provider_api_key
from webui_bridge_run import (
    RunAlreadyInProgressError,
    _RUN_LOCK,
    build_run_prompt,
    classify_run_status,
    run_provider_via_bridge,
)
from webui_chat_storage import (
    CHAT_DIR_RELATIVE,
    CLIENT_WRITABLE_CHAT_ROLES,
    append_chat_message,
    archive_old_months,
    build_history_drawer,
    ensure_chat_gitignore,
    list_available_months,
    read_month_messages,
    touch_registry,
)
from webui_common import (
    AUTO_WORKSPACE_BASE_DIR,
    WorkspaceError,
    _bridge_check_for_update,
    month_key,
    read_shared_context,
    utc_now,
    write_shared_context,
)
from webui_credentials import (
    API_KEY_MODE_PROVIDERS,
    CUSTOM_PROVIDER_API_FORMATS,
    cli_available,
    custom_provider_id,
    read_credentials,
    read_custom_providers,
    save_credential,
    save_custom_provider,
)
from webui_workspace import (
    _WORKSPACE_CREATE_LOCK,
    create_workspace_for_first_message,
    list_tree_entries,
    read_file_preview,
    resolve_startup_workspace,
    validate_workspace_candidate,
)

# Phase 7b M6: a stable, OS-independent stderr marker for "the port is
# already in use" -- src-tauri/src/lib.rs used to detect this by matching
# raw OSError text (POSIX's "Address already in use" vs. Windows'
# WSAEADDRINUSE wording, which can itself be localized per system
# language), which a review round pointed out was fragile. Printing this
# fixed string ourselves, once, means the Rust side never has to guess at
# what a given Python/OS combination's exception text looks like.
PORT_CONFLICT_MARKER = "AHB_PORT_CONFLICT"

try:
    import webview  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - depends on optional local install
    webview = None  # type: ignore[assignment]
    WEBVIEW_IMPORT_ERROR = exc
else:
    WEBVIEW_IMPORT_ERROR = None

WEBUI_ROOT = Path(__file__).resolve().parent / "webui"


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
                if workspace is None:
                    self._send_json(200, {"workspace": None, "name": None})
                else:
                    self._send_json(200, {"workspace": str(workspace), "name": workspace.name or str(workspace)})
            elif parsed.path == "/api/tree":
                if workspace is None:
                    # Nothing to browse yet -- an empty tree, not an error;
                    # the "no workspace" screen is the expected state here.
                    self._send_json(200, {"path": rel_path, "entries": []})
                else:
                    try:
                        entries = list_tree_entries(workspace, rel_path)
                        self._send_json(200, {"path": rel_path, "entries": entries})
                    except WorkspaceError as exc:
                        self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/file":
                if workspace is None:
                    self._send_json(400, {"error": "no workspace selected"})
                else:
                    try:
                        preview = read_file_preview(workspace, rel_path)
                        self._send_json(200, preview)
                    except WorkspaceError as exc:
                        self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/chat":
                if workspace is None:
                    month = (query.get("month", [""])[0]).strip() or month_key(utc_now())
                    self._send_json(200, {"month": month, "months": [], "messages": []})
                else:
                    month = (query.get("month", [""])[0]).strip() or month_key(utc_now())
                    messages = read_month_messages(workspace, month)
                    self._send_json(
                        200, {"month": month, "months": list_available_months(workspace), "messages": messages}
                    )
            elif parsed.path == "/api/history":
                self._send_json(200, {"groups": build_history_drawer(workspace)})
            elif parsed.path == "/api/providers":
                # Backs SCR-06/components.html §14's connection panel. Never
                # includes the raw key -- only whether one is configured.
                # Covers the full (Gemini-included) PROVIDERS for CLI
                # detection -- Phase 5 finally resolves what SCR-06
                # originally shipped as a "미확인" placeholder badge for
                # Gemini into a real one. `api_key_mode_supported` is now
                # True for Gemini too (DEC-25) -- API_KEY_MODE_PROVIDERS
                # was extended to include it, so the frontend offers a key
                # field for it exactly like codex/claude.
                credentials = read_credentials()
                providers = []
                for provider in PROVIDERS:
                    supports_api_key_mode = provider in API_KEY_MODE_PROVIDERS
                    entry = credentials.get(provider) if supports_api_key_mode else None
                    providers.append(
                        {
                            "provider": provider,
                            "cli_detected": cli_available(provider),
                            "api_key_mode_supported": supports_api_key_mode,
                            "api_key_configured": entry is not None,
                            "model": (entry or {}).get("model") if entry else None,
                        }
                    )
                # Custom providers (DEC-26) have no CLI concept at all --
                # cli_detected/api_key_mode_supported are meaningless for
                # them (always API-key mode), so they get their own
                # response key rather than forcing those two fields into
                # every entry of `providers` above.
                custom_providers = [
                    {
                        "provider": custom_provider_id(name),
                        "name": name,
                        "api_format": entry["api_format"],
                        "base_url": entry["base_url"],
                        "model": entry["model"],
                        "api_key_configured": True,
                    }
                    for name, entry in sorted(read_custom_providers().items())
                ]
                self._send_json(200, {"providers": providers, "custom_providers": custom_providers})
            elif parsed.path == "/api/update-check":
                # Phase 6 (SCR-07): reads the cached result of the
                # background check main() kicked off at startup -- never
                # runs `gh` itself on this request path, so this is
                # always a fast, synchronous read regardless of network
                # conditions.
                #
                # `checked` (review fix: a real race, not a nitpick) lets
                # the frontend tell "still checking" (poll again shortly)
                # apart from "checked, no update" (stop asking) -- the
                # real `gh` call is network I/O and can easily still be
                # in flight by the time the page's first request for this
                # arrives, especially right after server startup.
                #
                # Read `checked` BEFORE `info`, opposite of the write
                # order in _check_for_update_in_background() (info then
                # checked) -- a second review found this matters: if the
                # background thread's two writes land in the gap between
                # this handler's own two reads, reading `info` first
                # could observe the pre-write `None` alongside a
                # freshly-written `checked = True`, reporting "checked,
                # no update" for a request that actually raced a real
                # update being found. Reading `checked` first instead
                # means the only possible stale read is `checked = False`
                # paired with a not-yet-visible `info` -- which just tells
                # the polling client to ask again, the safe direction to
                # be wrong in (under-reporting readiness self-corrects on
                # the next poll; over-reporting readiness with stale data
                # does not, since the client stops polling on `checked:
                # true`).
                checked = state.update_checked
                # Only look at `info` at all once `checked` was observed
                # True -- if the writer hasn't gotten there yet, `info`
                # isn't meaningful regardless of what value happens to be
                # sitting in it, so this never spreads a value the
                # `checked: false` response shouldn't be making claims
                # about either way. Once checked, `info` is always a dict
                # with a `status` field (CFL-18: "available"/"current"/
                # "unavailable" -- check_for_update() never returns None
                # anymore, precisely so "genuinely current" and "couldn't
                # check at all" are distinguishable here instead of both
                # collapsing into the same response).
                info = state.update_info if checked else None
                self._send_json(200, {"checked": checked, **(info or {})})
            elif parsed.path == "/api/shared-context":
                # DEC-27: `.handoff/shared-context.md`, free-form per-
                # workspace context folded into every provider call
                # (CLI mode via handoff_bridge.py's build_prompt(),
                # API-key mode via run_provider_via_api_key() -- see
                # webui_common.read_shared_context()'s own docstring).
                # No workspace yet: empty, not an error, same posture as
                # /api/chat's "no workspace selected" branch above.
                text = read_shared_context(workspace) if workspace is not None else ""
                self._send_json(200, {"text": text})
            else:
                # _send_json(), not send_error(): every other /api/* branch
                # in do_GET responds with a JSON body, and webui/app.js's
                # fetchJSON() unconditionally calls res.json() on whatever
                # comes back -- send_error()'s HTML body would surface as a
                # confusing JSON-parse error instead of a real "not found"
                # message if this branch is ever actually reached (a typo'd
                # path, a stale cached bundle hitting a renamed endpoint).
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/chat":
                try:
                    body = self._read_json_body()
                    role = str(body.get("role") or "user")
                    if role not in CLIENT_WRITABLE_CHAT_ROLES:
                        # "agent" is only ever written by POST /api/run, right
                        # after a real provider call -- accepting it here
                        # would let a client forge a fake agent reply with no
                        # provider having actually run.
                        raise WorkspaceError(f"role must be one of {CLIENT_WRITABLE_CHAT_ROLES}")
                    if role == "user" and _RUN_LOCK.locked():
                        # pair_messages_into_turns() (Phase 3) walks the chat
                        # log in append order and attaches each agent reply
                        # to whichever user message it saw most recently --
                        # a second tab/client posting a new user message
                        # while another run is still in flight could get
                        # that in-flight run's eventual reply misattributed
                        # to the newer message once it lands in the drawer.
                        # Reject outright rather than let two turns overlap;
                        # this is a plain (non-atomic) check-then-append, not
                        # airtight against a run starting in the gap, but it
                        # closes the realistic window (the run's full
                        # duration) down to a few instructions.
                        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
                    text = str(body.get("text") or "")
                    attachments = body.get("attachments") or []
                    if not isinstance(attachments, list):
                        raise WorkspaceError("attachments must be a list")
                    if state.workspace is None:
                        # DEC-05: creation is deferred all the way to here --
                        # the first *user* message, regardless of whether the
                        # "새 폴더 자동 생성" button was clicked first. A
                        # "system"-role post can't carry a summary and
                        # shouldn't silently create a workspace as a
                        # side effect of something other than the user
                        # actually sending something.
                        if role != "user":
                            raise WorkspaceError("no workspace selected")
                        # Double-checked locking: _WORKSPACE_CREATE_LOCK
                        # serializes creation, and re-checking workspace is
                        # None *after* acquiring it means a request that lost
                        # the race just uses the workspace the winner already
                        # created, instead of creating a second one.
                        with _WORKSPACE_CREATE_LOCK:
                            if state.workspace is None:
                                state.workspace = create_workspace_for_first_message(text, attachments)
                                touch_registry(state.workspace, utc_now())
                    message = append_chat_message(state.workspace, role, text, attachments, utc_now())
                    self._send_json(200, message)
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/open-folder":
                try:
                    if _RUN_LOCK.locked():
                        # A provider run writes into whatever workspace was
                        # active when it started and persists into that
                        # workspace's chat log when it finishes -- switching
                        # state.workspace out from under an in-flight run
                        # would misdirect where that write (and everything
                        # the client renders once the run resolves) ends up.
                        raise RunAlreadyInProgressError("a provider run is already in progress; wait for it to finish")
                    body = self._read_json_body()
                    candidate = validate_workspace_candidate(str(body.get("path") or ""))
                    state.workspace = candidate
                    ensure_chat_gitignore(candidate)
                    archive_old_months(candidate, utc_now())
                    touch_registry(candidate, utc_now())
                    self._send_json(200, {"workspace": str(candidate), "name": candidate.name or str(candidate)})
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/run":
                try:
                    body = self._read_json_body()
                    provider = str(body.get("provider") or "auto")
                    if provider not in ("auto",) + PROVIDERS:
                        raise WorkspaceError(f"invalid provider: {provider}")
                    text = str(body.get("text") or "").strip()
                    attachments = body.get("attachments") or []
                    if not isinstance(attachments, list):
                        raise WorkspaceError("attachments must be a list")
                    if not text and not attachments:
                        raise WorkspaceError("text or attachments required")
                    if state.workspace is None:
                        # Normal flow always creates the workspace as a side
                        # effect of the preceding POST /api/chat -- this is
                        # only reachable via a client bug or direct API use
                        # that skips it.
                        raise WorkspaceError("no workspace selected")
                    model = body.get("model") or None
                    workspace = state.workspace
                    prompt = build_run_prompt(text, attachments)
                    records = run_provider_via_bridge(workspace, provider, prompt, model, "continue")
                    messages = []
                    for record in records:
                        status = classify_run_status(record["handoff_needed"], record["reason"])
                        agent_text = record.get("final_text") or f"(exit {record['exit_code']}, no output)"
                        messages.append(
                            append_chat_message(
                                workspace,
                                "agent",
                                agent_text,
                                [],
                                utc_now(),
                                provider=record["provider"],
                                status=status,
                                reason=record["reason"],
                            )
                        )
                    self._send_json(200, {"messages": messages})
                except RunAlreadyInProgressError as exc:
                    self._send_json(409, {"error": str(exc)})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/provider-key":
                try:
                    body = self._read_json_body()
                    provider = str(body.get("provider") or "")
                    if provider not in API_KEY_MODE_PROVIDERS:
                        raise WorkspaceError(f"invalid provider: {provider}")
                    key = str(body.get("key") or "").strip()
                    model = str(body.get("model") or "").strip() or None
                    # An empty key removes the credential (save_credential()'s
                    # contract) -- lets the connection panel's same "저장"
                    # action double as "disconnect API key mode".
                    #
                    # A non-empty key is verified with a real, minimal call
                    # (validate_provider_api_key()) *before* it is ever
                    # written to disk -- previously any non-empty string was
                    # saved unconditionally and only found to be wrong (or
                    # right) the next time the user actually tried to chat.
                    # This needs a model to call with, and
                    # API_KEY_MODE_DEFAULT_MODELS is deliberately empty for
                    # both providers (no built-in default, DEC-13/15) -- so a
                    # model is now required in the same request as a
                    # non-empty key, not merely recommended. Removal (empty
                    # key) skips validation entirely; there is nothing to
                    # verify when disconnecting.
                    confirmation: str | None = None
                    if key:
                        if not model:
                            raise WorkspaceError(
                                f"a model is required to save and verify a {provider} API key "
                                "(no built-in default exists for this provider)"
                            )
                        result = validate_provider_api_key(provider, key, model)
                        if not result["ok"]:
                            raise WorkspaceError(result["message"])
                        confirmation = result["text"]
                    # Unlike touch_registry() (best-effort, failure only
                    # logged -- always called *after* a real state change it
                    # shouldn't be allowed to desync from), this write IS the
                    # entire point of the request: the user is actively
                    # trying to save/remove a key, so a write failure
                    # (permissions, full disk, base dir exists as a file)
                    # must reach them as a real error, not disappear.
                    # save_credential() doesn't wrap OSError itself; do it
                    # here so it becomes a normal WorkspaceError -> 400 like
                    # every other failure this endpoint (and this whole
                    # feature) produces, instead of an uncaught exception
                    # crashing this request's thread with no JSON response.
                    try:
                        save_credential(provider, key, model)
                    except OSError as exc:
                        raise WorkspaceError(f"failed to save API key: {exc}") from exc
                    response = {"provider": provider, "api_key_configured": bool(key), "model": model if key else None}
                    if confirmation is not None:
                        response["verified"] = True
                        response["confirmation"] = confirmation
                    self._send_json(200, response)
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/custom-provider":
                # DEC-26. Same shape/contract as /api/provider-key above --
                # empty `key` removes (save_custom_provider()'s own
                # contract, doubling as "delete this custom provider" so
                # there's no separate delete endpoint), a non-empty key is
                # validated with a real minimal call before ever being
                # written to disk. Name/format/base_url/model validation
                # (blank name, unknown api_format, missing scheme, missing
                # model) lives in save_custom_provider()/
                # validate_custom_provider_name() -- both raise a plain
                # ValueError with a client-facing message, caught here the
                # same way WorkspaceError is caught everywhere else.
                try:
                    body = self._read_json_body()
                    name = str(body.get("name") or "")
                    key = str(body.get("key") or "").strip()
                    model = str(body.get("model") or "").strip() or None
                    base_url = str(body.get("base_url") or "").strip()
                    api_format = str(body.get("api_format") or "")
                    confirmation: str | None = None
                    if key:
                        if api_format not in CUSTOM_PROVIDER_API_FORMATS:
                            raise WorkspaceError(f"api_format must be one of: {', '.join(CUSTOM_PROVIDER_API_FORMATS)}")
                        if not base_url.startswith(("http://", "https://")):
                            raise WorkspaceError("base_url must start with http:// or https://")
                        if not model:
                            raise WorkspaceError("a model is required to save and verify a custom provider")
                        result = validate_provider_api_key(name, key, model, api_format=api_format, base_url=base_url)
                        if not result["ok"]:
                            raise WorkspaceError(result["message"])
                        confirmation = result["text"]
                    try:
                        save_custom_provider(name, key, model, base_url, api_format)
                    except OSError as exc:
                        raise WorkspaceError(f"failed to save custom provider: {exc}") from exc
                    except ValueError as exc:
                        raise WorkspaceError(str(exc)) from exc
                    response = {
                        "provider": custom_provider_id(name.strip()),
                        "api_key_configured": bool(key),
                        "model": model if key else None,
                    }
                    if confirmation is not None:
                        response["verified"] = True
                        response["confirmation"] = confirmation
                    self._send_json(200, response)
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            elif parsed.path == "/api/shared-context":
                try:
                    if state.workspace is None:
                        raise WorkspaceError("no workspace selected")
                    body = self._read_json_body()
                    text = str(body.get("text") or "")
                    try:
                        write_shared_context(state.workspace, text)
                    except OSError as exc:
                        raise WorkspaceError(f"failed to save shared context: {exc}") from exc
                    self._send_json(200, {"text": text})
                except WorkspaceError as exc:
                    self._send_json(400, {"error": str(exc)})
            else:
                # _send_json(), matching do_GET's fallback fix above and
                # every other /api/* branch's JSON-error contract.
                self._send_json(405, {"error": "unsupported POST endpoint"})

    return Handler


class AppState:
    """Mutable holder for the active workspace so "Open Folder" can switch
    it at runtime without restarting the server.

    `workspace` is `None` in Phase 2's "no workspace" state (DEC-04) --
    every handler that reads it must handle that case explicitly rather
    than assuming a `Path`.
    """

    def __init__(self, workspace: Path | None):
        self.workspace = workspace
        # Phase 6 (SCR-07): written once by a background thread started in
        # main() shortly after startup, read by GET /api/update-check.
        # Plain attributes, not behind a lock: single write-once-then-
        # read-many values, and CPython attribute assignment is already
        # atomic with respect to concurrent reads from other request
        # threads.
        #
        # update_checked distinguishes "the background check hasn't
        # finished yet" from "it finished and found nothing newer" --
        # both used to collapse into the same `update_info is None`,
        # which a review correctly flagged as a real race: the real `gh`
        # subprocess call is network I/O (can easily take a few seconds),
        # while the page load + its first GET /api/update-check can
        # finish well before that -- webui/app.js used to check exactly
        # once at boot, so a frontend that happened to ask before the
        # background check finished would permanently treat "still
        # checking" as "no update," even once the real answer arrived
        # moments later. app.js now polls while `checked` is false
        # instead of asking only once.
        #
        # update_info is None until update_checked is True, and always a
        # dict (never None) once it is -- check_for_update() (CFL-18)
        # always returns a dict with a `status` field
        # ("available"/"current"/"unavailable"), so "genuinely current"
        # and "couldn't check at all" stay distinguishable all the way
        # through to the frontend instead of both collapsing into the
        # same falsy value.
        self.update_checked = False
        self.update_info: dict | None = None


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
        description="MVP web UI: browse/switch workspace, chat, and run Codex/Claude (Phase 1: POST /api/run)."
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Initial workspace folder. Switchable at runtime. Defaults to the current "
        "directory if it's already an initialized handoff workspace (has .handoff/); "
        "otherwise starts in the Phase 2 'no workspace' state instead of assuming cwd "
        "is the intended project.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Must be loopback (127.0.0.1/localhost/::1) -- this server has no auth, so non-loopback hosts are refused at startup.",
    )
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


# Same set remote_handoff_server.py checks before allowing --no-auth. This
# server has no auth mechanism at all -- unlike remote_handoff_server.py,
# there is no flag that makes a non-loopback bind acceptable here, so the
# check is unconditional rather than gated behind --no-auth.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _check_for_update_in_background(state: "AppState") -> None:
    """Runs check_for_update() (a real `gh` subprocess call -- network
    I/O, can take a few seconds) off the startup path so server boot and
    the browser/native window opening aren't delayed by it. Matches
    docs/design-system/wireframes.html SCR-07's "앱 시작 시 백그라운드로
    최신 릴리즈 확인" -- once at startup, not on every request.
    check_for_update() itself never raises (gh missing/unauthenticated/
    offline all just resolve to a `{"status": "unavailable", ...}` dict,
    CFL-18), so nothing here needs its own try/except.

    Sets `update_info` before `update_checked` -- a reader that observes
    `update_checked is True` must never see a stale/uninitialized
    `update_info` alongside it."""
    state.update_info = _bridge_check_for_update()
    state.update_checked = True


def main(argv: list[str] | None = None) -> int:
    # Phase 7a (DEC-22): when this process is spawned as a Tauri sidecar
    # (src-tauri/src/lib.rs), the caller waits for a specific line on
    # this process's stdout before it will create the app window at
    # all -- but CPython only line-buffers stdout when it's a real tty;
    # piped to another process (exactly what a sidecar's stdout is),
    # it's fully block-buffered by default, so the readiness print below
    # could sit unflushed in this process's own memory indefinitely
    # (this is a long-running server that never naturally exits to
    # trigger an on-exit flush). Setting PYTHONUNBUFFERED=1 on the
    # sidecar spawn was tried first and empirically did NOT reliably
    # reach this process through PyInstaller's onefile bootloader when
    # actually tested against the built binary -- reconfiguring the
    # stream directly here is unaffected by that and by any other
    # environment-variable propagation question.
    sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    if not is_loopback_host(args.host):
        print(f"refusing to bind to non-loopback host: {args.host!r}", file=sys.stderr)
        print(f"this server has no authentication -- only {sorted(LOOPBACK_HOSTS)} are allowed", file=sys.stderr)
        return 1
    workspace, error = resolve_startup_workspace(args.workspace, Path.cwd())
    if error:
        print(error, file=sys.stderr)
        return 1

    state = AppState(workspace)
    if state.workspace is not None:
        ensure_chat_gitignore(state.workspace)
        archive_old_months(state.workspace, utc_now())
        touch_registry(state.workspace, utc_now())  # DEC-10: CLI startup counts too

    threading.Thread(target=_check_for_update_in_background, args=(state,), daemon=True).start()

    handler = build_handler(state)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        if exc.errno in (48, 98, 10048):  # EADDRINUSE: macOS, Linux, Windows
            print(PORT_CONFLICT_MARKER, file=sys.stderr)
        raise
    url = f"http://{args.host}:{args.port}/"
    if workspace is not None:
        print(f"Agent Handoff Bridge web UI (MVP) serving {workspace}")
    else:
        print("Agent Handoff Bridge web UI (MVP) -- no workspace yet")
        print(f"  Send a message or use Open Folder; a folder is auto-created under {AUTO_WORKSPACE_BASE_DIR} otherwise.")
    print(f"  {url}")
    print("  File browsing + local chat, and POST /api/run actually calls Codex/Claude.")
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
        window_title = f"Agent Handoff Bridge — {workspace.name if workspace is not None else '워크스페이스 없음'}"
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
