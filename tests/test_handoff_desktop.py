#!/usr/bin/env python3
"""Unit tests for handoff_desktop.py's argv-building logic.

handoff_desktop.py is a Tkinter GUI (HandoffDesktop subclasses tk.Tk when
tkinter is available, or plain `object` when it's not -- see TK_BASE). These
tests never instantiate a real HandoffDesktop widget tree (which would need
a live Tk display and isn't practical to run headlessly/in CI). Instead they
call the unbound HandoffDesktop.run_args() method against a minimal
duck-typed stand-in object exposing just the *_var.get() / text_value()
surface run_args() actually touches -- exercising the real argv-building
code path without any GUI machinery.

Covers two fixes:
1. PROVIDERS / PRIMARY_PROVIDERS are derived from handoff_bridge.PROVIDERS
   (gemini included) instead of stale hardcoded tuples.
2. run_args() passes the turn prompt via --prompt-file (not a bare trailing
   positional after --instruction-type) and uses "--model=value" (equals
   form), matching the fix already applied in handoff_webui.py.

Run with: python3 -m unittest tests.test_handoff_desktop -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge
import handoff_desktop as hdsk  # noqa: E402


class _FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _FakeDesktop:
    """Duck-typed stand-in exposing only what HandoffDesktop.run_args() uses."""

    def __init__(self, provider="auto", instruction_type="continue", model="", execute=False, auto_fallback=True, prompt="Continue the task."):
        self.provider_var = _FakeVar(provider)
        self.instruction_type_var = _FakeVar(instruction_type)
        self.model_var = _FakeVar(model)
        self.execute_var = _FakeVar(execute)
        self.auto_fallback_var = _FakeVar(auto_fallback)
        self.prompt_text = object()  # opaque; text_value() below ignores it
        self._prompt = prompt

    def text_value(self, _widget):
        return self._prompt


class ProvidersDerivedFromBridgeTests(unittest.TestCase):
    def test_providers_matches_bridge_providers_plus_auto(self):
        self.assertEqual(hdsk.PROVIDERS, ("auto",) + handoff_bridge.PROVIDERS)

    def test_primary_providers_matches_bridge_providers_exactly(self):
        # handoff_bridge.py's own `init --primary` accepts the full
        # PROVIDERS set with no restricted subset, so PRIMARY_PROVIDERS
        # (the GUI's "Primary" dropdown) shouldn't be narrower either.
        self.assertEqual(hdsk.PRIMARY_PROVIDERS, handoff_bridge.PROVIDERS)
        self.assertIn("gemini", hdsk.PRIMARY_PROVIDERS)

    def test_gemini_is_selectable(self):
        self.assertIn("gemini", hdsk.PROVIDERS)


class RunArgsShapeTests(unittest.TestCase):
    def test_prompt_passed_via_prompt_file_not_trailing_positional(self):
        fake = _FakeDesktop(prompt="hello world")
        args, prompt_path = hdsk.HandoffDesktop.run_args(fake)
        try:
            self.assertNotIn("hello world", args)
            self.assertIn("--prompt-file", args)
            path_in_args = Path(args[args.index("--prompt-file") + 1])
            self.assertEqual(path_in_args, prompt_path)
            self.assertTrue(prompt_path.exists())
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), "hello world")
        finally:
            prompt_path.unlink(missing_ok=True)

    def test_model_uses_equals_form_not_separate_flag(self):
        fake = _FakeDesktop(model="-weird-model-name")
        args, prompt_path = hdsk.HandoffDesktop.run_args(fake)
        try:
            self.assertIn("--model=-weird-model-name", args)
            self.assertNotIn("--model", args)
        finally:
            prompt_path.unlink(missing_ok=True)

    def test_execute_and_auto_fallback_flags(self):
        fake = _FakeDesktop(execute=True, auto_fallback=True)
        args, prompt_path = hdsk.HandoffDesktop.run_args(fake)
        try:
            self.assertIn("--execute", args)
            self.assertIn("--auto-fallback", args)
        finally:
            prompt_path.unlink(missing_ok=True)

    def test_no_model_flag_when_model_blank(self):
        fake = _FakeDesktop(model="  ")
        args, prompt_path = hdsk.HandoffDesktop.run_args(fake)
        try:
            self.assertFalse(any(a.startswith("--model") for a in args))
        finally:
            prompt_path.unlink(missing_ok=True)

    def test_run_bridge_cleans_up_prompt_file_after_worker_completes(self):
        # run_bridge() spawns a background thread; give it a moment to run
        # and unlink the temp file via its finally-block cleanup path.
        import threading
        import time
        from unittest import mock

        fake = _FakeDesktop(prompt="cleanup check")
        args, prompt_path = hdsk.HandoffDesktop.run_args(fake)
        self.assertTrue(prompt_path.exists())

        # Minimal stand-in that has just enough surface for run_bridge():
        # bridge_command(), append_log(), set_busy(), and a Tk-less `after`
        # that runs its callback synchronously instead of scheduling it on
        # a (nonexistent, headless) Tk event loop.
        class _FakeRunner:
            def bridge_command(self, args):
                return ["true"]  # a real no-op subprocess, not mocked

            def append_log(self, *a, **k):
                pass

            def set_busy(self, *a, **k):
                pass

            def after(self, _delay, func, *fargs):
                func(*fargs)

            def finish_command(self, *a, **k):
                pass

        runner = _FakeRunner()
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            hdsk.HandoffDesktop.run_bridge(runner, args, "test", cleanup_path=prompt_path)
        # Worker runs on a daemon thread; wait briefly for it to finish.
        for _ in range(50):
            if not prompt_path.exists():
                break
            time.sleep(0.02)
        self.assertFalse(prompt_path.exists())


if __name__ == "__main__":
    unittest.main()
