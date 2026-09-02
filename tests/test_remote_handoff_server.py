#!/usr/bin/env python3
"""Unit tests for remote_handoff_server.py's argv-safety, token, and
task-JSON logic. Covers the findings a full-project review reported for this
module: an empty/corrupted token file silently disabling remote-server auth,
user-controlled task/prompt text reaching handoff_bridge.py's argv with no
"--" separator, TimeoutExpired.stdout/.stderr staying `bytes` and crashing
the JSON write, non-atomic/unguarded task-JSON reads and writes, and a
Gemini-excluding stale copy of handoff_bridge.PROVIDERS.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402
import remote_handoff_server as rhs  # noqa: E402


class LoadOrCreateTokenTests(unittest.TestCase):
    def test_empty_existing_token_file_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("   \n", encoding="utf-8")

            token = rhs.load_or_create_token(None, token_file)

            self.assertTrue(token)
            self.assertEqual(token_file.read_text(encoding="utf-8").strip(), token)

    def test_nonempty_existing_token_file_is_reused_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("real-token-value\n", encoding="utf-8")

            token = rhs.load_or_create_token(None, token_file)

            self.assertEqual(token, "real-token-value")


class RunTaskArgvSafetyTests(unittest.TestCase):
    """run_task() builds subprocess argv for handoff_bridge.py; user-supplied
    task/prompt text must always land after a "--" separator so it can never
    be parsed as a flag (e.g. a prompt of "--execute" bypassing the server's
    own --allow-execute gate)."""

    def _run_task_capturing_argv(self, task: dict) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run_command(task, args, timeout):
            calls.append(list(args))
            return 0

        server = types.SimpleNamespace(task_timeout=30)
        with mock.patch.object(rhs, "run_command", side_effect=fake_run_command), \
             mock.patch.object(rhs, "update_task"):
            rhs.run_task(task, server)
        return calls

    def test_task_and_prompt_text_land_after_a_separator(self):
        task = {
            "id": "t1",
            "workspace": "/tmp/ws",
            "task": "do the thing",
            "prompt": "please continue",
            "provider": "auto",
            "primary": "codex",
            "execute": False,
            "auto_fallback": True,
            "commands": [],
        }
        _, init_args, run_args = self._run_task_capturing_argv(task)

        self.assertEqual(init_args, ["init", "--primary", "codex", "--", "do the thing"])
        self.assertEqual(run_args, ["run", "auto", "--", "please continue"])

    def test_task_and_prompt_starting_with_dash_stay_positional(self):
        task = {
            "id": "t2",
            "workspace": "/tmp/ws",
            "task": "--execute",
            "prompt": "--execute",
            "provider": "codex",
            "primary": "codex",
            "execute": False,
            "auto_fallback": False,
            "commands": [],
        }
        calls = self._run_task_capturing_argv(task)
        install_args, init_args, run_args = calls

        self.assertEqual(init_args, ["init", "--primary", "codex", "--", "--execute"])
        self.assertEqual(run_args, ["run", "codex", "--", "--execute"])
        # Only one real "--execute" flag could ever appear before the "--"
        # separator, and here execute=False means none should.
        self.assertNotIn("--execute", run_args[: run_args.index("--")])


class WriteReadJsonTests(unittest.TestCase):
    def test_read_json_survives_corrupt_file_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            path.write_text('{"id": "t1", "status": "run', encoding="utf-8")  # truncated
            self.assertEqual(rhs.read_json(path), {})

    def test_write_json_then_read_json_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            rhs.write_json(path, {"id": "t1", "status": "completed"})
            self.assertEqual(rhs.read_json(path), {"id": "t1", "status": "completed"})

    def test_write_json_never_leaves_a_partial_file_on_disk(self):
        # write_json() now routes through handoff_bridge.atomic_write_text()
        # (temp file + os.replace) instead of a bare write_text(), so a
        # concurrent reader can never observe a truncated file.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.json"
            rhs.write_json(path, {"id": "t1"})
            leftover_temp_files = list(Path(tmp).glob(".task.json.*.tmp"))
            self.assertEqual(leftover_temp_files, [])


class RunCommandTimeoutDecodeTests(unittest.TestCase):
    """run_command() now delegates to handoff_bridge.short_run() (a
    structure audit found this file's own subprocess.run() wrapper
    reimplemented handoff_bridge.py's short_run() independently) -- so
    these mock the underlying subprocess.run() in handoff_bridge's own
    namespace, the same real dependency short_run() actually has."""

    def test_bytes_timeout_output_does_not_crash_the_json_write(self):
        # Regression: TimeoutExpired.stdout/.stderr can still be `bytes`
        # even with text=True on the subprocess.run() call above (CPython
        # builds the exception from raw pipe buffers on the timeout path).
        # Storing that bytes value straight into command_record used to
        # blow up inside write_json()'s json.dumps() call, uncaught, wedging
        # the task at its last good status forever.
        task = {"id": "t1", "workspace": "/tmp/ws", "commands": []}
        timeout_exc = subprocess.TimeoutExpired(cmd=["run"], timeout=1, output=b"partial\xffbytes", stderr=b"stderr\xffbytes")
        with mock.patch.object(hb.subprocess, "run", side_effect=timeout_exc), mock.patch.object(rhs, "update_task"):
            exit_code = rhs.run_command(task, ["run", "codex"], timeout=1)

        self.assertEqual(exit_code, 124)
        # Must be plain str (json.dumps()-safe), not bytes, and must not
        # raise decoding invalid UTF-8 either.
        command_record = task["commands"][-1]
        self.assertIsInstance(command_record["stdout"], str)
        self.assertIsInstance(command_record["stderr"], str)
        json.dumps(command_record)  # must not raise

    def test_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine -- to decode this subprocess's stdout/stderr,
        # which reflects handoff_bridge.py's own output for arbitrary
        # task/prompt content.
        task = {"id": "t1", "workspace": "/tmp/ws", "commands": []}
        with mock.patch.object(
            hb.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run_spy, mock.patch.object(rhs, "update_task"):
            rhs.run_command(task, ["run", "codex"], timeout=1)
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")


class ProviderListTests(unittest.TestCase):
    def test_gemini_is_a_valid_provider_and_primary(self):
        # Regression: PROVIDERS/PRIMARY_PROVIDERS used to be separate
        # hardcoded copies that never picked up Gemini when it was added to
        # handoff_bridge.PROVIDERS, silently rejecting it on this server.
        self.assertIn("gemini", rhs.PROVIDERS)
        self.assertIn("gemini", rhs.PRIMARY_PROVIDERS)


class NormalizeTaskTests(unittest.TestCase):
    def _fake_server(self, allow_roots):
        return types.SimpleNamespace(allow_execute=False, allow_roots=allow_roots)

    def test_gemini_provider_and_primary_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = self._fake_server([Path(tmp).resolve()])
            task = rhs.normalize_task(
                {"task": "do it", "provider": "gemini", "primary": "gemini", "workspace": tmp}, server
            )
        self.assertEqual(task["provider"], "gemini")
        self.assertEqual(task["primary"], "gemini")

    def test_invalid_provider_error_message_lists_gemini(self):
        # Regression (structure audit): the error message used to be a
        # second hardcoded string ("auto, codex, claude") that fell out of
        # sync with PROVIDERS itself once gemini was added -- telling a
        # caller gemini wasn't allowed when it actually was. Now built
        # from PROVIDERS directly, so it can't drift again.
        server = self._fake_server([Path(".")])
        with self.assertRaises(ValueError) as ctx:
            rhs.normalize_task({"task": "do it", "provider": "not-a-real-provider"}, server)
        self.assertIn("gemini", str(ctx.exception))

    def test_relative_workspace_resolves_against_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                (Path(tmp) / "sub").mkdir()
                server = self._fake_server([Path(tmp).resolve()])
                task = rhs.normalize_task({"task": "do it", "workspace": "sub"}, server)
            finally:
                os.chdir(original_cwd)
        self.assertEqual(task["workspace"], str((Path(tmp) / "sub").resolve()))


class TaskTimeoutDefaultTests(unittest.TestCase):
    """Covers the audit finding that --task-timeout defaulted to 0 ("no
    timeout"), so an unattended automation caller had no way to bound how
    long a worker thread could be tied up."""

    def test_default_is_finite(self):
        args = rhs.build_parser().parse_args([])
        self.assertEqual(args.task_timeout, 1800)
        self.assertGreater(args.task_timeout, 0)

    def test_zero_is_still_selectable_explicitly(self):
        args = rhs.build_parser().parse_args(["--task-timeout", "0"])
        self.assertEqual(args.task_timeout, 0)


class FakeRequestHandler:
    """Duck-types just enough of BaseHTTPRequestHandler (self.headers,
    self.rfile) for read_json_body() to run without a real socket -- a real
    oversized upload over a real loopback socket is its own source of
    flakiness (the server can legitimately RST the connection while the
    client is still mid-send of a multi-MB body that was never going to be
    read), which is beside the point of this specific unit."""

    def __init__(self, body: bytes, content_length: int | None = None):
        self.headers = {"Content-Length": str(len(body) if content_length is None else content_length)}
        self.rfile = io.BytesIO(body)


class ReadJsonBodySizeLimitTests(unittest.TestCase):
    """Covers the audit finding that read_json_body() only checked
    `length <= 0`, unlike the Web UI's equivalent 2 MB cap
    (handoff_webui.py's _read_json_body())."""

    def test_oversized_content_length_is_rejected(self):
        fake = FakeRequestHandler(b"{}", content_length=rhs.MAX_BODY_BYTES + 1)
        with self.assertRaises(ValueError) as ctx:
            rhs.Handler.read_json_body(fake)
        self.assertIn("oversized", str(ctx.exception))

    def test_content_length_at_the_cap_is_accepted(self):
        payload = json.dumps({"task": "x" * (rhs.MAX_BODY_BYTES - 20)}).encode("utf-8")
        self.assertLessEqual(len(payload), rhs.MAX_BODY_BYTES)
        fake = FakeRequestHandler(payload)
        body = rhs.Handler.read_json_body(fake)
        self.assertIn("task", body)

    def test_normal_sized_body_still_parses(self):
        payload = json.dumps({"task": "a small task"}).encode("utf-8")
        fake = FakeRequestHandler(payload)
        self.assertEqual(rhs.Handler.read_json_body(fake), {"task": "a small task"})


if __name__ == "__main__":
    unittest.main()
