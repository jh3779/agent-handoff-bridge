#!/usr/bin/env python3
"""Smoke test of the server as the desktop app runs it: over stdin/stdout.

The Tauri shell starts the bundled server sidecar with `--stdio` and talks to it
through pipes (src-tauri/src/ipc.rs, webui_stdio.py). The unit tests exercise the
Python source, not the PyInstaller binary that actually ships, so a binary that
fails to read stdin, or that opens a port, would pass all of them. This starts
the real thing the way the app does, sends it requests and checks the answers.

Usage:
  python3 scripts/smoke_sidecar.py <server-command> [args...]
  python3 scripts/smoke_sidecar.py src-tauri/binaries/agent-handoff-bridge-server-<triple>[.exe]
  python3 scripts/smoke_sidecar.py python3 handoff_webui.py          # the source, for a quick check

`--stdio --workspace <temp dir>` is appended. It uses a temporary HOME, so it
never touches the user's data, and makes no network connection. Exits 0 when
every check passes, 1 otherwise (installer-build runs it on every OS and a
failure fails the release build).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

READY_TIMEOUT_SECONDS = 180  # a PyInstaller onefile binary unpacks itself first
REPLY_TIMEOUT_SECONDS = 60
EXIT_TIMEOUT_SECONDS = 30
KOREAN_TEXT = "스모크 테스트에서 보낸 한글 메시지\n둘째 줄"


class SmokeFailure(Exception):
    pass


def _descendants(pid: int) -> list[int]:
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        out = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True, text=True).stdout.split()
        for child in out:
            found.append(int(child))
            frontier.append(int(child))
    return found


def _listening_sockets(pid: int) -> str | None:
    """The TCP sockets listening in `pid`'s process tree, "" for none, or None if
    this machine has no way to tell (Windows, or no pgrep/lsof)."""
    if os.name == "nt" or not (shutil.which("pgrep") and shutil.which("lsof")):
        return None
    tree = [pid] + _descendants(pid)
    result = subprocess.run(
        ["lsof", "-nP", "-a", "-p", ",".join(map(str, tree)), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def run(command: list[str]) -> list[str]:
    """Runs every check; returns the report lines. Raises SmokeFailure."""
    report: list[str] = []
    # The sidecar runs with a temp dir as its cwd, so a path the caller gave
    # relative to *their* cwd (a script, a binary) must be made absolute first.
    command = [os.path.abspath(part) if os.path.exists(part) else part for part in command]
    home = tempfile.mkdtemp(prefix="ahb-smoke-home-")
    workspace = tempfile.mkdtemp(prefix="ahb-smoke-ws-")
    env = dict(os.environ, HOME=home, USERPROFILE=home, PYTHONUNBUFFERED="1", AHB_NO_MODEL_PREWARM="1")
    started = time.monotonic()
    proc = subprocess.Popen(
        [*command, "--stdio", "--workspace", workspace],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
        cwd=workspace,
    )
    stderr_lines: list[str] = []
    threading.Thread(target=lambda: [stderr_lines.append(line) for line in proc.stderr], daemon=True).start()

    def fail(message: str) -> SmokeFailure:
        return SmokeFailure(f"{message}\n--- sidecar stderr (tail) ---\n{''.join(stderr_lines)[-1500:]}")

    def read_frame(timeout: float) -> dict:
        box: dict = {}

        def reader() -> None:
            box["line"] = proc.stdout.readline()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if "line" not in box:
            raise fail(f"no frame within {timeout:.0f}s")
        if not box["line"]:
            raise fail("the sidecar closed its stdout (it exited)")
        try:
            return json.loads(box["line"])
        except ValueError as exc:
            raise fail(f"stdout line is not a JSON frame ({exc}): {box['line'][:200]!r}") from exc

    def call(request_id: int, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        frame: dict = {"id": request_id, "method": method, "path": path, "headers": headers or {}}
        if body is not None:
            frame["body"] = json.dumps(body, ensure_ascii=False)
        proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        answer = read_frame(REPLY_TIMEOUT_SECONDS)
        if answer.get("id") != request_id:
            raise fail(f"answer for the wrong request: wanted id {request_id}, got {answer.get('id')!r}")
        return answer

    try:
        ready = read_frame(READY_TIMEOUT_SECONDS)
        if ready.get("event") != "ready":
            raise fail(f"first frame is not the ready event: {ready}")
        report.append(f"ready after {time.monotonic() - started:.1f}s (version {ready.get('version')})")

        info = call(1, "GET", "/api/info")
        if info["status"] != 200:
            raise fail(f"GET /api/info -> {info['status']}")
        report.append(f"GET /api/info -> 200 ({json.loads(info['body']).get('version')})")

        session = {"Content-Type": "application/json", "X-AHB-Session": "default"}
        posted = call(2, "POST", "/api/chat", {"role": "user", "text": KOREAN_TEXT, "attachments": []}, session)
        if posted["status"] != 200:
            raise fail(f"POST /api/chat -> {posted['status']}: {posted['body'][:200]}")
        chat = call(3, "GET", "/api/chat", headers={"X-AHB-Session": "default"})
        if chat["status"] != 200 or KOREAN_TEXT.split("\n")[0] not in chat["body"]:
            raise fail("the Korean message did not come back from GET /api/chat")
        report.append("POST then GET /api/chat -> 200, non-ASCII text intact")

        providers = call(4, "GET", "/api/providers")
        names = [p["provider"] for p in json.loads(providers["body"])["providers"]]
        if providers["status"] != 200 or sorted(names) != ["agy", "claude", "codex"]:
            raise fail(f"GET /api/providers -> {providers['status']} {names}")
        report.append(f"GET /api/providers -> 200 {names}")

        page = call(5, "GET", "/")
        if page["status"] != 200 or "<title>" not in page["body"]:
            raise fail(f"GET / did not return the page ({page['status']})")
        report.append("GET / -> 200, the page")
        for number, asset in enumerate(("/app.js", "/app.css", "/i18n.js", "/theme-init.js"), start=6):
            served = call(number, "GET", asset)
            if served["status"] != 200 or not served["body"].strip():
                raise fail(f"GET {asset} -> {served['status']}")
        report.append("GET /app.js /app.css /i18n.js /theme-init.js -> 200")

        missing = call(20, "GET", "/api/nope")
        if missing["status"] != 404:
            raise fail(f"GET /api/nope -> {missing['status']}, expected 404")
        report.append("GET /api/nope -> 404")

        listening = _listening_sockets(proc.pid)
        if listening is None:
            report.append("listening sockets: not checked on this OS")
        elif listening:
            raise fail(f"the sidecar opened a listening TCP socket:\n{listening}")
        else:
            report.append("listening TCP sockets in the sidecar's process tree: none")

        proc.stdin.close()  # end of input: the sidecar must exit on its own
        try:
            code = proc.wait(timeout=EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise fail(f"the sidecar did not exit within {EXIT_TIMEOUT_SECONDS}s of its stdin closing") from exc
        if code != 0:
            raise fail(f"the sidecar exited with code {code}")
        report.append("exited by itself, code 0, when stdin closed")
        if "Traceback" in "".join(stderr_lines):
            raise fail("the sidecar printed a Python traceback")
        return report
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    try:
        for line in run(argv):
            print("ok:", line)
    except SmokeFailure as exc:
        print("FAIL:", exc)
        return 1
    print("PASS: the sidecar answers over stdin/stdout and opens no port")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
