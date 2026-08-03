#!/usr/bin/env python3
"""Unit tests for the highest-risk pure logic in handoff_bridge.py.

Run with: python3 -m unittest discover -s tests -v

This is the minimum coverage bar described in docs/quality-gates.md: the
provider fallback/classification logic and the shared-state write path must
have tests, because they have no other safety net (no CI provider calls, no
manual QA step that would catch a silent regression).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402


class ClassifyHandoffTests(unittest.TestCase):
    def test_success_needs_no_handoff(self):
        needed, reason = hb.classify_handoff(0, "all good", "", {})
        self.assertFalse(needed)
        self.assertTrue(reason.startswith("none:"))

    def test_rate_limit_signal_in_stdout(self):
        needed, reason = hb.classify_handoff(0, "Error: 429 too many requests", "", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("rate_limit:"))

    def test_quota_signal_in_stderr(self):
        needed, reason = hb.classify_handoff(1, "", "insufficient quota for this request", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("quota:"))

    def test_auth_signal(self):
        needed, reason = hb.classify_handoff(1, "", "authentication_failed: token expired", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("auth:"))

    def test_tool_failure_signal_by_pattern(self):
        needed, reason = hb.classify_handoff(1, "", "bash: codex: command not found", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"))

    def test_tool_failure_from_exit_code_127(self):
        needed, reason = hb.classify_handoff(127, "", "", {})
        self.assertTrue(needed)
        self.assertEqual(reason, "tool_failure: provider command not found")

    def test_unmatched_nonzero_exit_is_tool_failure(self):
        needed, reason = hb.classify_handoff(2, "no idea what happened", "", {})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("tool_failure:"))

    def test_machine_readable_error_without_known_signal_is_unknown(self):
        needed, reason = hb.classify_handoff(0, "", "", {"errors": [{"type": "error", "message": "boom"}]})
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("unknown:"))

    def test_machine_readable_error_with_known_signal(self):
        needed, reason = hb.classify_handoff(
            0, "", "", {"errors": [{"type": "error", "message": "server overloaded"}]}
        )
        self.assertTrue(needed)
        self.assertTrue(reason.startswith("overloaded:"))

    def test_reason_label_always_in_handoff_labels_or_none(self):
        cases = [
            (0, "", "", {}),
            (1, "429", "", {}),
            (1, "", "quota exceeded", {}),
            (127, "", "", {}),
            (3, "", "", {}),
        ]
        for exit_code, stdout, stderr, parsed in cases:
            _, reason = hb.classify_handoff(exit_code, stdout, stderr, parsed)
            label = reason.split(":", 1)[0]
            self.assertIn(label, hb.HANDOFF_LABELS + ("none",))


class ChooseAutoProviderTests(unittest.TestCase):
    def test_handoff_needed_switches_to_other_provider(self):
        state = {"status": "handoff_needed", "last_provider": "codex", "primary_provider": "codex"}
        self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_handoff_needed_from_claude_switches_to_codex(self):
        state = {"status": "handoff_needed", "last_provider": "claude", "primary_provider": "codex"}
        self.assertEqual(hb.choose_auto_provider(state), "codex")

    def test_prefers_primary_when_available(self):
        state = {"status": "ready", "primary_provider": "claude"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "claude" and "/usr/bin/claude"):
            self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_falls_back_to_any_available_provider(self):
        state = {"status": "ready", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", side_effect=lambda name: name == "claude" and "/usr/bin/claude"):
            self.assertEqual(hb.choose_auto_provider(state), "claude")

    def test_falls_back_to_primary_when_nothing_is_installed(self):
        state = {"status": "ready", "primary_provider": "codex"}
        with mock.patch.object(hb.shutil, "which", return_value=None):
            self.assertEqual(hb.choose_auto_provider(state), "codex")


class ModelOverrideArgTests(unittest.TestCase):
    def test_recording_labels_are_not_passed_through(self):
        for label in ("app-selected default", "provider default", "default", "unknown", "  UNKNOWN  "):
            self.assertIsNone(hb.model_override_arg(label))

    def test_none_and_empty_are_not_passed_through(self):
        self.assertIsNone(hb.model_override_arg(None))
        self.assertIsNone(hb.model_override_arg(""))

    def test_exact_model_id_is_passed_through(self):
        self.assertEqual(hb.model_override_arg("claude-sonnet-5"), "claude-sonnet-5")


class AtomicWriteTests(unittest.TestCase):
    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "file.txt"
            hb.atomic_write_text(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            hb.atomic_write_text(target, "content")
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "file.txt"]
            self.assertEqual(leftovers, [])

    def test_overwrite_replaces_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            hb.atomic_write_text(target, "first")
            hb.atomic_write_text(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second")


class VersionTests(unittest.TestCase):
    def test_cli_version_flag_reports_bridge_version(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "handoff_bridge.py"), "--version"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(hb.BRIDGE_VERSION, result.stdout)


class WriteLockTests(unittest.TestCase):
    def test_lock_is_released_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".write.lock"
            with hb.WriteLock(lock_path, timeout=1):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_second_lock_times_out_while_first_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".write.lock"
            with hb.WriteLock(lock_path, timeout=1):
                with self.assertRaises(TimeoutError):
                    with hb.WriteLock(lock_path, timeout=0.2):
                        pass  # pragma: no cover - should never acquire


if __name__ == "__main__":
    unittest.main()
