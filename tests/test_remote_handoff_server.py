#!/usr/bin/env python3
"""Unit tests for remote_handoff_server.py's argv-safety and token logic.

Focused on the two security-relevant bugs a full-project review found and
this test file was added to close: an empty/corrupted token file silently
disabling remote-server auth, and user-controlled task/prompt text being
appended to handoff_bridge.py's argv with no "--" separator (letting a
value like "--execute" be parsed as the real flag instead of content).
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    unittest.main()
