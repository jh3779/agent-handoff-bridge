"""Tests for the socket-free Web UI transport (`webui_stdio.py`, `handoff_webui.py --stdio`).

The point of the transport is that the existing route handlers run unchanged, so
most tests drive the real handler class built by `handoff_webui.build_handler`.

Run with: python3 -m unittest tests.test_webui_stdio -v
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_webui as webui  # noqa: E402
import webui_credentials  # noqa: E402
import webui_stdio  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _body(response: dict):
    assert response["encoding"] == "utf-8"
    return json.loads(response["body"])


class RealHandlerTests(unittest.TestCase):
    """Frames dispatched into the real Web UI handler, with state in a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = (Path(tmp.name) / "Agent Handoff Bridge").resolve()
        for patcher in (
            mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", base),
            mock.patch("webui_credentials._fetch_agy_models", return_value=[]),
            mock.patch("webui_credentials._start_agy_refresh"),
            mock.patch.dict(
                webui_credentials._agy_models_cache,
                {"models": [], "ok_at": None, "failed_at": None, "refreshing": False},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = webui.AppState(None)
        self.handler = webui.build_handler(self.state)

    def request(self, method, path, body=None, headers=None):
        frame = {"method": method, "path": path, "headers": headers or {}}
        if body is not None:
            frame["body"] = body if isinstance(body, str) else json.dumps(body)
        return webui_stdio.dispatch(self.handler, frame)

    def test_a_get_route_answers_like_it_does_over_http(self):
        response = self.request("GET", "/api/info")
        self.assertEqual(response["status"], 200)
        self.assertTrue(response["headers"]["content-type"].startswith("application/json"))
        self.assertIn("version", _body(response))

    def test_a_post_then_a_get_see_the_same_state(self):
        posted = self.request("POST", "/api/chat", {"role": "user", "text": "hello", "attachments": []})
        self.assertEqual(posted["status"], 200)
        fetched = self.request("GET", "/api/chat")
        self.assertEqual(fetched["status"], 200)
        texts = [m["text"] for m in _body(fetched)["messages"]]
        self.assertIn("hello", texts)

    def test_the_provider_list_route_works_through_the_transport(self):
        response = self.request("GET", "/api/providers")
        self.assertEqual(response["status"], 200)
        self.assertEqual({p["provider"] for p in _body(response)["providers"]}, {"codex", "claude", "agy"})

    def test_a_cli_model_can_be_saved_and_read_back(self):
        saved = self.request("POST", "/api/cli-model", {"provider": "claude", "model": "opus"})
        self.assertEqual(saved["status"], 200)
        providers = _body(self.request("GET", "/api/providers"))["providers"]
        self.assertEqual({p["provider"]: p["cli_model"] for p in providers}["claude"], "opus")

    def test_error_statuses_come_through(self):
        self.assertEqual(self.request("GET", "/api/nope")["status"], 404)
        bad = self.request("POST", "/api/chat", "{not json")
        self.assertEqual(bad["status"], 400)
        self.assertIn("error", _body(bad))
        self.assertEqual(self.request("POST", "/api/chat")["status"], 400)  # no body at all

    def test_non_ascii_text_survives_the_round_trip(self):
        self.request("POST", "/api/chat", {"role": "user", "text": "한글 메시지 ✓", "attachments": []})
        texts = [m["text"] for m in _body(self.request("GET", "/api/chat"))["messages"]]
        self.assertIn("한글 메시지 ✓", texts)

    def test_the_session_header_selects_the_session(self):
        # The handler answers an unknown session id with a 400, and no header at all
        # means the default session; header names are case-insensitive as over HTTP.
        unknown = self.request("GET", "/api/tree", headers={"x-ahb-session": "no-such-session"})
        self.assertEqual(unknown["status"], 400)
        self.assertIn("unknown session", _body(unknown)["error"])
        unknown_exact_case = self.request("GET", "/api/tree", headers={"X-AHB-Session": "no-such-session"})
        self.assertEqual(unknown_exact_case["status"], 400)
        # No header means the default session, which always exists.
        self.assertNotIn("unknown session", self.request("GET", "/api/tree")["body"])
        self.assertNotIn("unknown session", self.request("GET", "/api/tree", headers={"X-AHB-Session": "default"})["body"])

    def test_static_files_are_served_too(self):
        response = self.request("GET", "/")
        self.assertEqual(response["status"], 200)
        self.assertIn("text/html", response["headers"]["content-type"])
        self.assertIn("<title>", response["body"])

    def test_unsupported_methods_and_malformed_frames_are_rejected(self):
        self.assertEqual(self.request("PUT", "/api/info")["status"], 405)
        self.assertEqual(webui_stdio.dispatch(self.handler, {"method": "GET", "path": "api/info"})["status"], 400)
        self.assertEqual(webui_stdio.dispatch(self.handler, {"method": "GET"})["status"], 400)
        self.assertEqual(
            webui_stdio.dispatch(self.handler, {"method": "GET", "path": "/api/info", "headers": ["x"]})["status"], 400
        )


class _FakeHandler(BaseHTTPRequestHandler):
    """Stands in for the real handler to test transport behavior in isolation."""

    gate = threading.Event()

    def do_GET(self):  # noqa: N802
        if self.path == "/boom":
            raise RuntimeError("handler bug")
        if self.path == "/slow":
            self.gate.wait(10)
        if self.path == "/binary":
            payload = bytes(range(256))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output quiet
        pass


class DispatchBehaviorTests(unittest.TestCase):
    def test_a_handler_exception_becomes_a_500_frame(self):
        response = webui_stdio.dispatch(_FakeHandler, {"method": "GET", "path": "/boom"})
        self.assertEqual(response["status"], 500)
        self.assertIn("handler bug", _body(response)["error"])

    def test_binary_bodies_are_base64(self):
        response = webui_stdio.dispatch(_FakeHandler, {"method": "GET", "path": "/binary"})
        self.assertEqual(response["encoding"], "base64")
        self.assertEqual(base64.b64decode(response["body"]), bytes(range(256)))


class ShutdownTests(unittest.TestCase):
    """EOF on stdin means the shell is gone. A request that is still running has a
    provider CLI under it, so it is finished, not abandoned."""

    def serve_slow_then_eof(self, **kwargs):
        _FakeHandler.gate.clear()
        out = io.StringIO()
        returned = []
        stdin = io.StringIO(json.dumps({"id": "slow", "method": "GET", "path": "/slow"}) + "\n")

        def run():
            webui_stdio.serve(_FakeHandler, stdin, out, "x", **kwargs)
            returned.append(time.monotonic())

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.4)  # EOF has been read by now; the request is still running
        still_waiting = not returned
        released_at = time.monotonic()
        _FakeHandler.gate.set()
        thread.join(10)
        return out, still_waiting, returned, released_at

    def test_serve_waits_for_a_running_request_after_eof(self):
        out, still_waiting, returned, released_at = self.serve_slow_then_eof()
        self.assertTrue(still_waiting, "serve() returned while a request was still running")
        self.assertTrue(returned and returned[0] >= released_at)
        ids = [json.loads(line).get("id") for line in out.getvalue().splitlines()]
        self.assertIn("slow", ids)  # its answer was still written

    def test_a_cap_can_still_be_set(self):
        out, still_waiting, returned, released_at = self.serve_slow_then_eof(wait_for_in_flight=0.05)
        self.assertFalse(still_waiting)  # gave up after the cap

    def test_a_closed_output_pipe_does_not_raise(self):
        class Closed(io.StringIO):
            def write(self, _text):
                raise BrokenPipeError("reader went away")

        code = webui_stdio.serve(
            _FakeHandler, io.StringIO(json.dumps({"id": 1, "method": "GET", "path": "/a"}) + "\n"), Closed(), "x"
        )
        self.assertEqual(code, 0)


class ServeLoopTests(unittest.TestCase):
    def run_serve(self, lines, handler=_FakeHandler):
        out = io.StringIO()
        code = webui_stdio.serve(handler, io.StringIO("".join(line + "\n" for line in lines)), out, "9.9.9")
        frames = [json.loads(line) for line in out.getvalue().splitlines()]
        return code, frames

    def test_ready_event_comes_first_and_ids_are_echoed(self):
        code, frames = self.run_serve(
            [json.dumps({"id": 1, "method": "GET", "path": "/a"}), json.dumps({"id": "two", "method": "GET", "path": "/b"})]
        )
        self.assertEqual(code, 0)
        self.assertEqual(frames[0], {"event": "ready", "version": "9.9.9"})
        by_id = {f["id"]: f for f in frames[1:]}
        self.assertEqual(set(by_id), {1, "two"})
        self.assertEqual(json.loads(by_id[1]["body"]), {"path": "/a"})
        self.assertEqual(json.loads(by_id["two"]["body"]), {"path": "/b"})

    def test_bad_frames_get_an_error_response_and_the_loop_continues(self):
        code, frames = self.run_serve(
            ["not json", "[1, 2]", "", json.dumps({"id": 3, "method": "GET", "path": "/ok"})]
        )
        self.assertEqual(code, 0)
        responses = frames[1:]
        self.assertEqual([r.get("status") for r in responses].count(400), 2)
        self.assertEqual([r for r in responses if r.get("id") == 3][0]["status"], 200)

    def test_a_slow_request_does_not_block_a_fast_one(self):
        # Needed so a minutes-long /api/run leaves the rest of the UI responsive.
        _FakeHandler.gate.clear()
        out = io.StringIO()
        done = threading.Event()

        def run():
            webui_stdio.serve(
                _FakeHandler,
                io.StringIO(
                    json.dumps({"id": "slow", "method": "GET", "path": "/slow"})
                    + "\n"
                    + json.dumps({"id": "fast", "method": "GET", "path": "/fast"})
                    + "\n"
                ),
                out,
                "x",
            )
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and '"fast"' not in out.getvalue():
            time.sleep(0.01)
        ids_before_release = [json.loads(line).get("id") for line in out.getvalue().splitlines()]
        _FakeHandler.gate.set()
        thread.join(10)
        self.assertIn("fast", ids_before_release)
        self.assertNotIn("slow", ids_before_release)  # the fast answer came back first
        final_ids = [json.loads(line).get("id") for line in out.getvalue().splitlines()]
        self.assertIn("slow", final_ids)


class StdioProcessTests(unittest.TestCase):
    """`handoff_webui.py --stdio` as a real subprocess: nothing may listen, and
    stdout must carry frames only."""

    def run_process(self, *extra_args, request=None):
        # The process never has one of the temp dirs as its cwd, and cleanup errors
        # are ignored: on Windows a just-exited process can still pin a directory
        # for a moment, which says nothing about the transport.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home, tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as workspace:
            env = dict(os.environ)
            env["HOME"] = home
            env["USERPROFILE"] = home
            # No agy on the path: the startup prewarm must not run a real `agy models`.
            env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("SystemRoot", "")
            result = subprocess.run(
                [sys.executable, str(ROOT / "handoff_webui.py"), "--stdio", "--workspace", workspace, *extra_args],
                input=request or "",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                env=env,
                cwd=str(ROOT),
            )
            return result, workspace

    def test_a_non_loopback_host_is_ignored_in_stdio_mode(self):
        # Nothing is bound, so --host must not make startup fail (it did, before).
        result, _ = self.run_process("--host", "0.0.0.0")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(frames[0]["event"], "ready")

    def test_the_process_speaks_frames_on_stdout_and_exits_on_eof(self):
        request = json.dumps({"id": 1, "method": "GET", "path": "/api/info"}) + "\n"
        result, workspace = self.run_process(request=request)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        frames = [json.loads(line) for line in lines]  # every stdout line is a frame
        self.assertEqual(frames[0]["event"], "ready")
        response = [f for f in frames if f.get("id") == 1][0]
        self.assertEqual(response["status"], 200)
        self.assertEqual(json.loads(response["body"])["workspace"], str(Path(workspace).resolve()))
        self.assertNotIn("127.0.0.1", result.stdout)  # no banner / URL on the frame channel


if __name__ == "__main__":
    unittest.main()
