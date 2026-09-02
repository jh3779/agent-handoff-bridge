#!/usr/bin/env python3
"""Unit tests for scripts/handoff_hook.py.

Regression coverage for the append_current() write race described in
docs/quality-gates.md's locking discipline: scripts/handoff_hook.py runs as
its own process on every Claude Code lifecycle event, while
handoff_bridge.py's `run` subcommand (the Codex CLI side) can independently
append to the same .handoff/current.md at the same time. Before this fix,
the hook wrote via a plain unlocked `open("a")`, so a hook append that
landed on disk after handoff_bridge.py's own append_current() had already
read the existing file but before its atomic os.replace() call would be
silently discarded. The hook must contend for the same WriteLock
(handoff_bridge.py, WRITE_LOCK_FILE) that handoff_bridge.append_current()
already holds for its own read-existing-then-atomic-replace.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402
import handoff_hook as hh  # noqa: E402


class RepoRootSubprocessEncodingTests(unittest.TestCase):
    def test_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine. A repo path can plausibly contain non-ASCII
        # characters (a Windows username, a localized folder name).
        with mock.patch.object(
            hh.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="/some/root\n", stderr=""),
        ) as run_spy:
            hh.repo_root()
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")


class AppendCurrentLockingTests(unittest.TestCase):
    """hh.append_current() must acquire the same cross-process WriteLock
    handoff_bridge.py's own append_current() uses for .handoff/current.md,
    not write to it unlocked."""

    def test_append_current_blocks_while_the_shared_lock_is_held(self):
        # Holding handoff_bridge.py's own WriteLock on the same lock file
        # externally must make the hook's write fail to acquire it. The old
        # unlocked `open("a", ...)` implementation never contended for this
        # lock at all, so this would have written through immediately.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            lock_path = root / ".handoff" / ".write.lock"
            with hb.WriteLock(lock_path, timeout=1):
                with mock.patch.object(
                    hh,
                    "WriteLock",
                    lambda path, timeout=10.0: hb.WriteLock(path, timeout=0.2),
                ):
                    with self.assertRaises(TimeoutError):
                        hh.append_current(root, {"hook_event_name": "Stop"})
            # current.md must be untouched by the failed attempt -- no
            # partial/torn write left behind.
            self.assertFalse((root / ".handoff" / "current.md").exists())

    def test_append_current_succeeds_once_the_lock_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            hh.append_current(root, {"hook_event_name": "Stop", "session_id": "abc123"})
            content = (root / ".handoff" / "current.md").read_text(encoding="utf-8")
            self.assertIn("## Hook Event", content)
            self.assertIn("- Session ID: abc123", content)


class AppendCurrentInteropTests(unittest.TestCase):
    """Sanity check that the hook's writer and handoff_bridge.py's own
    writer, run back-to-back against the same file under the shared lock,
    never clobber each other's content (each append is additive, not a
    stale-read-then-replace of the other's data)."""

    def test_interleaved_hook_and_bridge_appends_are_all_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            original_cwd = Path.cwd()
            # handoff_bridge.py's CURRENT_FILE/WRITE_LOCK_FILE are cwd-relative;
            # scripts/handoff_hook.py resolves the same paths from an explicit
            # `root` (via `git rev-parse --show-toplevel`) instead, so cwd
            # must match root for both to agree on the same on-disk file.
            import os

            os.chdir(root)
            try:
                hh.append_current(root, {"hook_event_name": "SessionStart"})
                hb.append_current(
                    {
                        "started_at": "2026-01-01T00:00:00",
                        "provider": "codex",
                        "exit_code": 0,
                        "handoff_needed": False,
                        "reason": "none",
                    }
                )
                hh.append_current(root, {"hook_event_name": "Stop"})
                hb.append_current(
                    {
                        "started_at": "2026-01-01T00:01:00",
                        "provider": "claude",
                        "exit_code": 0,
                        "handoff_needed": False,
                        "reason": "none",
                    }
                )
            finally:
                os.chdir(original_cwd)

            content = (root / ".handoff" / "current.md").read_text(encoding="utf-8")
            self.assertEqual(content.count("## Hook Event"), 2)
            self.assertEqual(content.count("## Run "), 2)
            self.assertIn("- Event: SessionStart", content)
            self.assertIn("- Event: Stop", content)
            self.assertIn("## Run 2026-01-01T00:00:00", content)
            self.assertIn("## Run 2026-01-01T00:01:00", content)


class WriteNextPromptAtomicWriteTests(unittest.TestCase):
    """Covers the audit finding (also previously noted in
    docs/codebase-review.md) that write_next_prompt() used a plain
    write_text() instead of the same WriteLock/atomic_write_text() pattern
    append_current() uses -- a crash mid-write could leave next-prompt.md
    partially written."""

    def test_delegates_to_atomic_write_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            with mock.patch.object(hh, "atomic_write_text", wraps=hh.atomic_write_text) as spy:
                hh.write_next_prompt(root, {"hook_event_name": "StopFailure", "error": "quota"})
            spy.assert_called_once()
            written_path, written_content = spy.call_args.args
            self.assertEqual(written_path, root / ".handoff" / "next-prompt.md")
            self.assertIn("Reason: quota", written_content)

    def test_writes_the_real_file_with_no_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            hh.write_next_prompt(root, {"hook_event_name": "StopFailure"})
            handoff_dir = root / ".handoff"
            self.assertTrue((handoff_dir / "next-prompt.md").exists())
            leftover_tmp_files = [p for p in handoff_dir.iterdir() if p.name != "next-prompt.md"]
            self.assertEqual(leftover_tmp_files, [])


if __name__ == "__main__":
    unittest.main()
