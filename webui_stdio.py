"""A socket-free transport for the Web UI's request handler.

The desktop app used to open a window onto http://127.0.0.1:8787, i.e. a TCP
port served by this process. `handoff_webui.py --stdio` serves the same routes
without any port: the desktop shell writes one JSON request per line to this
process's stdin and reads one JSON response per line from its stdout.

The route handlers are not rewritten. `dispatch()` builds the real
`BaseHTTPRequestHandler` subclass (`handoff_webui.build_handler`) around
in-memory input and output instead of a socket, calls its `do_GET` / `do_POST` /
`do_DELETE`, and parses the HTTP bytes it wrote back into a frame. Every route,
its validation and its status codes therefore behave exactly as over HTTP.

Frames (one JSON object per line, UTF-8):

    request   {"id": 7, "method": "POST", "path": "/api/run",
               "headers": {"X-AHB-Session": "default"}, "body": "{...json text...}"}
    response  {"id": 7, "status": 200, "headers": {"content-type": "..."},
               "body": "...", "encoding": "utf-8"}      # or "base64" for binary
    event     {"event": "ready", "version": "0.5.1"}    # sent once, at startup

Requests are handled on their own threads, so a minutes-long `/api/run` does not
block the other requests, and responses can come back out of order (match them
by `id`). The process exits when stdin reaches end-of-file, i.e. when the shell
that started it goes away.
"""

from __future__ import annotations

import base64
import email.message
import io
import json
import sys
import threading
import time
from typing import Any, TextIO

SUPPORTED_METHODS = ("GET", "POST", "DELETE")
# On EOF (the shell that started this process went away), requests that are
# already running are NOT abandoned: a /api/run has a provider CLI underneath it,
# and exiting while its thread waits would leave that CLI running with nothing
# watching it. serve() waits for them to finish; the bridge's own per-provider
# timeouts bound how long that can take. Pass a number of seconds to cap it.
WAIT_FOR_IN_FLIGHT = None


def _error_response(status: int, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps({"error": message}, ensure_ascii=False),
        "encoding": "utf-8",
    }


def _is_text(content_type: str) -> bool:
    kind = content_type.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in {"application/json", "application/javascript", "image/svg+xml"}


def _parse_http_response(raw: bytes) -> dict[str, Any]:
    """The status line, headers and body BaseHTTPRequestHandler wrote."""
    head, separator, payload = raw.partition(b"\r\n\r\n")
    if not separator:
        return _error_response(500, "handler produced no response")
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return _error_response(500, "handler produced a malformed status line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    if _is_text(headers.get("content-type", "")):
        return {"status": status, "headers": headers, "body": payload.decode("utf-8", errors="replace"), "encoding": "utf-8"}
    return {"status": status, "headers": headers, "body": base64.b64encode(payload).decode("ascii"), "encoding": "base64"}


def dispatch(handler_class: type, frame: dict[str, Any]) -> dict[str, Any]:
    """Run one request frame through `handler_class` and return the response
    frame (without `id`)."""
    method = str(frame.get("method") or "GET").upper()
    if method not in SUPPORTED_METHODS:
        return _error_response(405, f"method not allowed: {method}")
    path = frame.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        return _error_response(400, "path must be a string starting with /")
    raw_headers = frame.get("headers") or {}
    if not isinstance(raw_headers, dict):
        return _error_response(400, "headers must be an object")
    body = str(frame.get("body") or "").encode("utf-8")

    headers = email.message.Message()
    for name, value in raw_headers.items():
        headers[str(name)] = str(value)
    if body and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(body))

    # The handler class is only ever driven through these attributes (checked
    # against handoff_webui.build_handler); building it with __new__ skips
    # BaseHTTPRequestHandler.__init__, which would want a real socket.
    handler = handler_class.__new__(handler_class)
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.headers = headers
    handler.path = path
    handler.command = method
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.client_address = ("stdio", 0)
    handler.close_connection = True
    try:
        getattr(handler, f"do_{method}")()
    except Exception as exc:  # a handler bug must not take the transport down
        return _error_response(500, f"{type(exc).__name__}: {exc}")
    return _parse_http_response(handler.wfile.getvalue())


def serve(
    handler_class: type,
    stdin: TextIO,
    out: TextIO,
    version: str,
    wait_for_in_flight: float | None = WAIT_FOR_IN_FLIGHT,
) -> int:
    """Read request frames from `stdin` until EOF, answering each on `out`, then
    wait for the requests still running (see WAIT_FOR_IN_FLIGHT)."""
    write_lock = threading.Lock()

    def send(frame: dict[str, Any]) -> None:
        line = json.dumps(frame, ensure_ascii=False) + "\n"
        with write_lock:
            try:
                out.write(line)
                out.flush()
            except (OSError, ValueError):
                # The reader is gone (closed pipe). The work already ran; there
                # is nobody left to tell.
                pass

    def handle(frame: dict[str, Any]) -> None:
        response = dispatch(handler_class, frame)
        response["id"] = frame.get("id")
        send(response)

    send({"event": "ready", "version": version})
    workers: list[threading.Thread] = []
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            send({"id": None, **_error_response(400, "request frame is not valid JSON")})
            continue
        if not isinstance(frame, dict):
            send({"id": None, **_error_response(400, "request frame must be a JSON object")})
            continue
        worker = threading.Thread(target=handle, args=(frame,), daemon=True)
        worker.start()
        workers.append(worker)
        workers = [w for w in workers if w.is_alive()]

    running = [w for w in workers if w.is_alive()]
    if running:
        print(f"[stdio] input closed; waiting for {len(running)} running request(s) to finish", file=sys.stderr)
    deadline = None if wait_for_in_flight is None else time.monotonic() + wait_for_in_flight
    for worker in running:
        worker.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    return 0


def use_utf8_streams() -> None:
    """The frames are UTF-8 JSON; do not let a Windows console code page change
    that. Called before the streams are used."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
