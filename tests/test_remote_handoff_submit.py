#!/usr/bin/env python3
"""Unit tests for remote_handoff_submit.py's provider selection.

Covers the fix where PROVIDERS (and the --primary argparse choice) were
hardcoded to ("auto", "codex", "claude") / ("codex", "claude"), silently
excluding agy even though handoff_bridge.py's canonical PROVIDERS tuple
supports it. Both are now derived from handoff_bridge.PROVIDERS directly.

Run with: python3 -m unittest tests.test_remote_handoff_submit -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge
import remote_handoff_submit as rhs  # noqa: E402


class ProvidersDerivedFromBridgeTests(unittest.TestCase):
    def test_providers_matches_bridge_providers_plus_auto(self):
        self.assertEqual(rhs.PROVIDERS, ("auto",) + handoff_bridge.PROVIDERS)

    def test_agy_is_selectable_as_provider(self):
        parser = rhs.build_parser()
        provider_action = next(a for a in parser._actions if a.dest == "provider")
        self.assertIn("agy", provider_action.choices)

    def test_agy_is_selectable_as_primary(self):
        parser = rhs.build_parser()
        primary_action = next(a for a in parser._actions if a.dest == "primary")
        self.assertEqual(tuple(primary_action.choices), handoff_bridge.PROVIDERS)
        self.assertIn("agy", primary_action.choices)

    def test_parses_agy_from_argv(self):
        args = rhs.build_parser().parse_args(["do the task", "--provider", "agy", "--primary", "agy"])
        self.assertEqual(args.provider, "agy")
        self.assertEqual(args.primary, "agy")


class AutoFallbackFlagTests(unittest.TestCase):
    """Covers the audit finding that --auto-fallback was store_true/default=True
    with no way to turn it off -- a remote caller could never actually request
    single-provider-only execution even though the server payload/handler
    already supports auto_fallback=False."""

    def test_default_is_true(self):
        args = rhs.build_parser().parse_args(["do the task"])
        self.assertTrue(args.auto_fallback)

    def test_no_auto_fallback_disables_it(self):
        args = rhs.build_parser().parse_args(["do the task", "--no-auto-fallback"])
        self.assertFalse(args.auto_fallback)

    def test_explicit_auto_fallback_still_true(self):
        args = rhs.build_parser().parse_args(["do the task", "--auto-fallback"])
        self.assertTrue(args.auto_fallback)


if __name__ == "__main__":
    unittest.main()
