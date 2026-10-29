"""Behavior of the two small pure helpers in webui/app.js that decide what the
titlebar model input shows and when to re-ask /api/providers.

app.js is one browser-bound IIFE with no JS test harness in this repo, so the
helpers are cut out of the real file by name and run in Node's vm sandbox.
Skipped where Node is not installed.

Run with: python3 -m unittest tests.test_webui_frontend_logic -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "webui" / "app.js"
NODE = shutil.which("node")


def _function_source(name: str) -> str:
    text = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"^  function " + re.escape(name) + r"\(.*?\n  \}\n", text, re.S | re.M)
    if not match:
        raise AssertionError(f"function {name} not found in webui/app.js")
    return match.group(0)


def _run_node(expression: str):
    script = "\n".join(
        [
            _function_source("resolveTitlebarModel"),
            _function_source("needsModelRepoll"),
            f"process.stdout.write(JSON.stringify({expression}));",
        ]
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class ResolveTitlebarModelTests(unittest.TestCase):
    def resolve(self, meta, default, show=True):
        return _run_node(f"resolveTitlebarModel({json.dumps(meta)}, {json.dumps(default)}, {json.dumps(show)})")

    def test_a_tab_that_inherits_follows_the_saved_default(self):
        self.assertEqual(self.resolve({"model": "gpt-5-codex", "modelInherited": True}, "gpt-6.1-sol"), "gpt-6.1-sol")

    def test_clearing_the_default_clears_every_inheriting_tab(self):
        # The reported bug: "자동으로" removed the saved model but a tab kept sending it.
        for default in (None, ""):
            self.assertEqual(self.resolve({"model": "gpt-5-codex", "modelInherited": True}, default), "")

    def test_a_tab_without_the_flag_counts_as_inheriting(self):
        self.assertEqual(self.resolve({"model": "old"}, None), "")

    def test_a_value_the_user_typed_is_kept(self):
        self.assertEqual(self.resolve({"model": "my-pick", "modelInherited": False}, None), "my-pick")
        self.assertEqual(self.resolve({"model": "my-pick", "modelInherited": False}, "other-default"), "my-pick")

    def test_a_user_who_cleared_the_field_stays_cleared(self):
        self.assertEqual(self.resolve({"model": "", "modelInherited": False}, "some-default"), "")

    def test_hidden_input_is_always_empty(self):
        self.assertEqual(self.resolve({"model": "x", "modelInherited": False}, "d", show=False), "")


@unittest.skipUnless(NODE, "node is not installed")
class NeedsModelRepollTests(unittest.TestCase):
    def repoll(self, providers, attempt, max_polls=6):
        return _run_node(f"needsModelRepoll({json.dumps(providers)}, {attempt}, {max_polls})")

    def test_polls_again_only_while_a_list_is_being_fetched(self):
        refreshing = [{"models_refreshing": False}, {"models_refreshing": True}]
        settled = [{"models_refreshing": False}, {}]
        self.assertTrue(self.repoll(refreshing, 0))
        self.assertFalse(self.repoll(settled, 0))

    def test_it_gives_up_after_the_bound(self):
        refreshing = [{"models_refreshing": True}]
        self.assertTrue(self.repoll(refreshing, 5, 6))
        self.assertFalse(self.repoll(refreshing, 6, 6))


if __name__ == "__main__":
    unittest.main()
