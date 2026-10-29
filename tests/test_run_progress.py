"""Live progress of a provider run: event -> {kind, detail} (run_progress.py), the
growing stdout.jsonl that feeds it (handoff_bridge.run_provider), and the
GET /api/run-progress route (handoff_webui.py).

Event shapes are the ones real codex / claude runs wrote into .handoff/runs/.

Run with: python3 -m unittest tests.test_run_progress -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import handoff_bridge as hb  # noqa: E402
import handoff_webui as webui  # noqa: E402
import run_progress as rp  # noqa: E402


def codex_command_started(command):
    return {"type": "item.started", "item": {"id": "item_2", "type": "command_execution", "command": command,
                                             "aggregated_output": "", "exit_code": None, "status": "in_progress"}}


def claude_tool_use(name, tool_input):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}]}}


class DescribeEventTests(unittest.TestCase):
    def test_codex_command_drops_the_shell_wrapper_and_quotes(self):
        step = rp.describe_event("codex", codex_command_started('/bin/zsh -lc "rg -n \'handoff\' src docs"'))
        self.assertEqual(step, {"kind": "command", "detail": "rg -n 'handoff' src docs"})

    def test_a_long_command_is_cut_to_one_short_line(self):
        step = rp.describe_event("codex", codex_command_started("echo " + "x" * 400 + "\nnext line"))
        self.assertEqual(step["kind"], "command")
        self.assertLessEqual(len(step["detail"]), rp.DETAIL_MAX_CHARS)
        self.assertNotIn("\n", step["detail"])
        self.assertTrue(step["detail"].endswith("…"))

    def test_codex_finished_command_means_it_is_thinking_again(self):
        done = {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "status": "completed"}}
        self.assertEqual(rp.describe_event("codex", done), {"kind": "thinking", "detail": ""})

    def test_codex_message_and_turn_events(self):
        message = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
        self.assertEqual(rp.describe_event("codex", message)["kind"], "writing")
        self.assertEqual(rp.describe_event("codex", {"type": "turn.started"})["kind"], "starting")
        self.assertIsNone(rp.describe_event("codex", {"type": "turn.completed", "usage": {}}))
        # an "error" item (for example a skills-budget notice) says nothing about progress
        self.assertIsNone(rp.describe_event("codex", {"type": "item.completed", "item": {"type": "error", "message": "x"}}))

    def test_codex_file_change_names_the_first_file(self):
        event = {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}}
        self.assertEqual(rp.describe_event("codex", event), {"kind": "edit", "detail": "a.py"})

    def test_claude_tools_map_to_kinds_with_the_relevant_input(self):
        cases = [
            ("Bash", {"command": "wc -l x", "description": "count"}, "command", "wc -l x"),
            ("Read", {"file_path": "/repo/a.py"}, "read", "/repo/a.py"),
            ("Edit", {"file_path": "/repo/b.py", "old_string": "a", "new_string": "b"}, "edit", "/repo/b.py"),
            ("Write", {"file_path": "/repo/c.py", "content": "secret body"}, "write", "/repo/c.py"),
            ("Grep", {"pattern": "TODO", "path": "."}, "search", "TODO"),
            ("WebSearch", {"query": "tauri ipc"}, "web", "tauri ipc"),
            ("SomethingNew", {"x": 1}, "tool", "SomethingNew"),
        ]
        for name, tool_input, kind, detail in cases:
            with self.subTest(name):
                self.assertEqual(rp.describe_event("claude", claude_tool_use(name, tool_input)), {"kind": kind, "detail": detail})

    def test_claude_thinking_text_and_tool_results(self):
        thinking = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}}
        text = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "out"}]}}
        self.assertEqual(rp.describe_event("claude", thinking)["kind"], "thinking")
        self.assertEqual(rp.describe_event("claude", text)["kind"], "writing")
        self.assertEqual(rp.describe_event("claude", result)["kind"], "thinking")
        self.assertEqual(rp.describe_event("claude", {"type": "system", "subtype": "init"})["kind"], "starting")
        self.assertIsNone(rp.describe_event("claude", {"type": "system", "subtype": "hook_started"}))
        self.assertIsNone(rp.describe_event("claude", {"type": "rate_limit_event"}))

    def test_the_last_block_of_a_message_is_what_it_is_doing(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]}}
        self.assertEqual(rp.describe_event("claude", event), {"kind": "read", "detail": "x.py"})

    def test_unknown_shapes_and_providers_are_ignored_not_raised(self):
        for event in (None, [], "text", {}, {"type": 5}, {"type": "assistant", "message": "x"},
                      {"type": "item.started", "item": "x"}):
            self.assertIsNone(rp.describe_event("codex", event))
            self.assertIsNone(rp.describe_event("claude", event))
        self.assertIsNone(rp.describe_event("agy", {"type": "assistant"}))


class CurrentStepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "stdout.jsonl"

    def test_newest_recognised_event_wins(self):
        lines = [codex_command_started("ls"), {"type": "turn.completed", "usage": {}}]
        self.path.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")
        self.assertEqual(rp.current_step("codex", self.path), {"kind": "command", "detail": "ls"})

    def test_a_half_written_last_line_and_plain_text_are_skipped(self):
        text = json.dumps(codex_command_started("make test")) + "\nsome plain log line\n" + '{"type": "item.star'
        self.path.write_text(text, encoding="utf-8")
        self.assertEqual(rp.current_step("codex", self.path)["detail"], "make test")

    def test_missing_or_empty_file_is_no_step(self):
        self.assertIsNone(rp.current_step("codex", self.path))
        self.path.write_text("", encoding="utf-8")
        self.assertIsNone(rp.current_step("codex", self.path))

    def test_only_the_tail_of_a_huge_log_is_read(self):
        big = {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x" * 50_000}}
        lines = [json.dumps(codex_command_started("first"))] + [json.dumps(big)] * 20 + [json.dumps(codex_command_started("last"))]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertLess(self.path.stat().st_size, 2_000_000)
        self.assertGreater(self.path.stat().st_size, rp.TAIL_BYTES)
        self.assertEqual(rp.current_step("codex", self.path)["detail"], "last")


class RunDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)

    def make_run(self, name, events=()):
        run_dir = self.workspace / ".handoff" / "runs" / name
        run_dir.mkdir(parents=True)
        (run_dir / "stdout.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def test_the_previous_runs_directory_is_never_mistaken_for_the_current_one(self):
        self.make_run("20261029T060000Z-codex", [codex_command_started("old command")])
        self.assertIsNone(rp.newest_run_dir(self.workspace, "20261029T060100"))
        self.assertEqual(rp.read_progress(self.workspace, "20261029T060100"), {"kind": "working", "detail": ""})

    def test_the_newest_directory_at_or_after_the_start_is_used_including_a_fallback_hop(self):
        self.make_run("20261029T060000Z-codex", [codex_command_started("old")])
        self.make_run("20261029T060100Z-codex", [codex_command_started("first hop")])
        self.make_run("20261029T060130Z-claude", [claude_tool_use("Read", {"file_path": "a.py"})])
        progress = rp.read_progress(self.workspace, "20261029T060100")
        self.assertEqual(progress, {"kind": "read", "detail": "a.py", "provider": "claude"})

    def test_same_second_collision_suffix_is_handled(self):
        self.make_run("20261029T060100Z-codex-2", [codex_command_started("second")])
        progress = rp.read_progress(self.workspace, "20261029T060100")
        self.assertEqual((progress["provider"], progress["detail"]), ("codex", "second"))

    def test_agy_prints_one_envelope_at_the_end_so_it_only_ever_says_working(self):
        self.make_run("20261029T060100Z-agy", [])
        progress = rp.read_progress(self.workspace, "20261029T060100")
        self.assertEqual((progress["kind"], progress["provider"]), ("working", "agy"))

    def test_no_runs_directory_at_all(self):
        self.assertEqual(rp.read_progress(self.workspace, "20261029T060100"), {"kind": "working", "detail": ""})


def provider_script(body):
    return [sys.executable, "-u", "-c", textwrap.dedent(body)]


class LiveStdoutTests(unittest.TestCase):
    """run_provider() must give the provider a real file for stdout so the log
    grows while the CLI works, and still hand the finished text to everything
    downstream exactly as a pipe would have."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._orig_cwd)
        self.workspace = Path(self._tmp.name)

    def args(self, timeout):
        return hb.argparse.Namespace(
            prompt="hello", prompt_file=None, execute=True, auto_fallback=False,
            timeout_seconds=timeout, model=None, instruction_type="continue",
        )

    def state(self):
        return {"task": "hello", "primary_provider": "codex", "status": "ready"}

    def test_the_log_shows_the_first_event_while_the_provider_is_still_running(self):
        script = provider_script(
            """
            import sys, time, json
            sys.stdin.read()
            print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls -la"}}))
            time.sleep(3)
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}}))
            print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
            """
        )
        outcome = {}

        def run():
            with mock.patch.object(hb, "provider_command", return_value=script), contextlib.redirect_stdout(io.StringIO()):
                outcome["exit"] = hb.run_provider("codex", self.args(30), self.state())

        thread = threading.Thread(target=run)
        thread.start()
        try:
            seen = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and seen is None:
                if thread.is_alive():
                    progress = rp.read_progress(self.workspace, "0")
                    if progress.get("kind") == "command":
                        seen = progress
                time.sleep(0.1)
            self.assertTrue(thread.is_alive(), "the provider finished before its progress could be read")
            self.assertEqual(seen, {"kind": "command", "detail": "ls -la", "provider": "codex"})
        finally:
            thread.join(30)
        self.assertEqual(outcome.get("exit"), 0)
        run_dir = next((self.workspace / ".handoff" / "runs").iterdir())
        logged = (run_dir / "stdout.jsonl").read_text(encoding="utf-8")
        self.assertIn("all done", logged)
        self.assertEqual(len(logged.splitlines()), 3)

    def test_a_timeout_keeps_what_the_provider_had_already_written(self):
        script = provider_script(
            """
            import sys, time, json
            sys.stdin.read()
            print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "sleep 99"}}))
            time.sleep(60)
            """
        )
        with mock.patch.object(hb, "provider_command", return_value=script), contextlib.redirect_stdout(io.StringIO()):
            exit_code = hb.run_provider("codex", self.args(2), self.state())
        self.assertEqual(exit_code, 124)
        run_dir = next((self.workspace / ".handoff" / "runs").iterdir())
        self.assertIn("sleep 99", (run_dir / "stdout.jsonl").read_text(encoding="utf-8"))

    def test_stdout_and_stderr_still_reach_the_classifier_and_the_logs(self):
        script = provider_script(
            """
            import sys, json
            sys.stdin.read()
            line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "한글 답변"}}, ensure_ascii=False)
            sys.stdout.buffer.write(line.encode("utf-8") + b"\\n")
            sys.stdout.flush()
            print("warning on stderr", file=sys.stderr)
            """
        )
        with mock.patch.object(hb, "provider_command", return_value=script), contextlib.redirect_stdout(io.StringIO()):
            hb.run_provider("codex", self.args(30), self.state())
        run_dir = next((self.workspace / ".handoff" / "runs").iterdir())
        self.assertIn("한글 답변", (run_dir / "stdout.jsonl").read_text(encoding="utf-8"))
        self.assertIn("warning on stderr", (run_dir / "stderr.log").read_text(encoding="utf-8"))


class RunProgressRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name).resolve()
        self.state = webui.AppState(self.workspace)
        self.server = webui.WebUIServer(("127.0.0.1", 0), webui.build_handler(self.state))
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self):
        with urllib.request.urlopen(self.base + "/api/run-progress", timeout=5) as response:
            return json.loads(response.read())

    def test_not_running_when_no_run_holds_the_workspace_lock(self):
        self.assertEqual(self.get(), {"running": False})

    def test_running_reports_the_current_runs_step_and_ignores_the_previous_run(self):
        runs = self.workspace / ".handoff" / "runs"
        old = runs / "20200101T000000Z-codex"
        old.mkdir(parents=True)
        (old / "stdout.jsonl").write_text(json.dumps(codex_command_started("stale")) + "\n", encoding="utf-8")
        self.state.mark_run_started(self.workspace)
        lock = self.state.get_run_lock_for(self.workspace)
        with lock:
            self.assertEqual(self.get(), {"running": True, "kind": "working", "detail": ""})
            stamp = rp.run_started_prefix(webui.datetime.now(webui.timezone.utc))
            current = runs / f"{stamp}-codex"
            current.mkdir()
            (current / "stdout.jsonl").write_text(json.dumps(codex_command_started("make test")) + "\n", encoding="utf-8")
            self.assertEqual(self.get(), {"running": True, "kind": "command", "detail": "make test", "provider": "codex"})
        self.assertEqual(self.get(), {"running": False})


if __name__ == "__main__":
    unittest.main()
