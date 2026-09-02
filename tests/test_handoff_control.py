#!/usr/bin/env python3
"""Unit tests for handoff_control.py.

Covers two fixes:
1. PROVIDERS is derived from handoff_bridge.PROVIDERS (gemini included)
   instead of a stale hardcoded ("auto", "codex", "claude") tuple.
2. run_with_prompt() passes the turn prompt via --prompt-file (not a bare
   trailing positional after --instruction-type), which is the fix for a
   confirmed cross-argparse-version parsing inconsistency (see
   handoff_webui.py's own --prompt-file comment for the same issue).

Run with: python3 -m unittest tests.test_handoff_control -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge
import handoff_control as hc  # noqa: E402


class ProvidersDerivedFromBridgeTests(unittest.TestCase):
    def test_providers_matches_bridge_providers_plus_auto(self):
        self.assertEqual(hc.PROVIDERS, ("auto",) + handoff_bridge.PROVIDERS)

    def test_gemini_is_selectable(self):
        self.assertIn("gemini", hc.PROVIDERS)

    def test_primary_choices_in_build_parser_include_gemini(self):
        parser = hc.build_parser()
        primary_action = next(a for a in parser._actions if a.dest == "primary")
        self.assertEqual(tuple(primary_action.choices), handoff_bridge.PROVIDERS)
        self.assertIn("gemini", primary_action.choices)


class AskProviderTests(unittest.TestCase):
    def test_accepts_gemini_from_input(self):
        with mock.patch("builtins.input", return_value="gemini"):
            self.assertEqual(hc.ask_provider("auto"), "gemini")

    def test_reprompts_on_unknown_value(self):
        with mock.patch("builtins.input", side_effect=["bogus", "codex"]):
            self.assertEqual(hc.ask_provider("auto"), "codex")


def _capture_run_bridge():
    """Shared helper: a fake run_bridge() that records its argv instead of
    spawning the real handoff_bridge.py subprocess."""
    captured = {}

    def fake_run_bridge(workspace, bridge_args):
        captured["workspace"] = workspace
        captured["args"] = bridge_args
        return 0

    return captured, fake_run_bridge


class AskPrimaryProviderTests(unittest.TestCase):
    """Covers the audit finding that initialize_task() used ask_provider()
    (which accepts "auto") for a value passed straight to `init --primary`,
    which has never accepted "auto" -- an avoidable CLI error."""

    def test_rejects_auto_and_reprompts(self):
        with mock.patch("builtins.input", side_effect=["auto", "codex"]):
            self.assertEqual(hc.ask_primary_provider("codex"), "codex")

    def test_accepts_gemini(self):
        with mock.patch("builtins.input", return_value="gemini"):
            self.assertEqual(hc.ask_primary_provider("codex"), "gemini")

    def test_initialize_task_never_offers_auto_as_primary(self):
        captured, fake_run_bridge = _capture_run_bridge()
        with mock.patch.object(hc, "run_bridge", fake_run_bridge), mock.patch(
            "builtins.input", side_effect=["do the thing", "codex", ""]
        ):
            hc.initialize_task(Path("/tmp/ws"))
        args = captured["args"]
        self.assertEqual(args[args.index("--primary") + 1], "codex")


class RunWithPromptArgvShapeTests(unittest.TestCase):
    """Exercise the argv-building logic in isolation, without spawning the
    real handoff_bridge.py subprocess (run_bridge is monkeypatched)."""

    def _capture(self):
        captured = {}

        def fake_run_bridge(workspace, bridge_args):
            captured["workspace"] = workspace
            captured["args"] = bridge_args
            return 0

        return captured, fake_run_bridge

    def test_prompt_passed_via_prompt_file_not_trailing_positional(self):
        captured, fake_run_bridge = self._capture()
        with mock.patch.object(hc, "run_bridge", fake_run_bridge):
            rc = hc.run_with_prompt(Path("/tmp/ws"), ["run", "auto", "--instruction-type", "continue"], "hello world")
        self.assertEqual(rc, 0)
        args = captured["args"]
        # The prompt text itself must never appear as a bare positional.
        self.assertNotIn("hello world", args)
        self.assertIn("--prompt-file", args)
        prompt_path = Path(args[args.index("--prompt-file") + 1])
        # File must be cleaned up after run_bridge() returns.
        self.assertFalse(prompt_path.exists())

    def test_prompt_file_contains_the_prompt_while_bridge_runs(self):
        seen_content = {}

        def fake_run_bridge(workspace, bridge_args):
            prompt_path = Path(bridge_args[bridge_args.index("--prompt-file") + 1])
            seen_content["text"] = prompt_path.read_text(encoding="utf-8")
            return 0

        with mock.patch.object(hc, "run_bridge", fake_run_bridge):
            hc.run_with_prompt(Path("/tmp/ws"), ["run", "auto"], "the real turn prompt")
        self.assertEqual(seen_content["text"], "the real turn prompt")

    def test_model_uses_equals_form(self):
        captured, fake_run_bridge = self._capture()
        with mock.patch.object(hc, "run_bridge", fake_run_bridge), mock.patch(
            "builtins.input", side_effect=["auto", "-weird-model-name", "hi"]
        ):
            hc.preview_run(Path("/tmp/ws"))
        args = captured["args"]
        self.assertIn("--model=-weird-model-name", args)
        # Must never be split into a separate ["--model", value] pair, which
        # would let argparse swallow a "-"-prefixed model as the next flag.
        self.assertNotIn("--model", args)


if __name__ == "__main__":
    unittest.main()
