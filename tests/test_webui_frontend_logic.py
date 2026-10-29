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
            _function_source("staleWarningText"),
            _function_source("applyStaleWarnings"),
            f"process.stdout.write(JSON.stringify({expression}));",
        ]
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _run_node_script(body: str, result_var: str):
    script = "\n".join(
        [
            _function_source("staleWarningText"),
            _function_source("applyStaleWarnings"),
            body,
            f"process.stdout.write(JSON.stringify({result_var}));",
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


@unittest.skipUnless(NODE, "node is not installed")
class StaleWarningTests(unittest.TestCase):
    """A re-poll must be able to surface a newly stale model while the user is
    typing in a model field, without rebuilding (and so wiping) the rows."""

    def run_warnings(self, providers, existing_ids):
        # Fake DOM: only warning elements exist, and any other lookup throws, which
        # proves the function never reaches for an input or anything else.
        script = f"""
        const els = {{}};
        for (const id of {json.dumps(existing_ids)}) els[id] = {{ hidden: true, textContent: "" }};
        const getById = (id) => {{
          if (!id.startsWith("cli-model-warn-")) throw new Error("touched " + id);
          return els[id] || null;
        }};
        const translate = (key, p) => key + "|" + p.model + "|" + p.provider;
        applyStaleWarnings({json.dumps(providers)}, getById, translate, {{ codex: "Codex" }});
        """
        return _run_node_script(script + "\nconst out = els;", "out")

    def test_a_newly_stale_model_shows_its_warning(self):
        out = self.run_warnings(
            [{"provider": "codex", "cli_model": "gpt-5-codex", "model_stale": True}], ["cli-model-warn-codex"]
        )
        self.assertEqual(out["cli-model-warn-codex"]["hidden"], False)
        self.assertEqual(out["cli-model-warn-codex"]["textContent"], "provider.modelStale|gpt-5-codex|Codex")

    def test_the_warning_clears_when_the_model_is_no_longer_stale_or_unset(self):
        for info in (
            {"provider": "codex", "cli_model": "gpt-6.1-sol", "model_stale": False},
            {"provider": "codex", "cli_model": None, "model_stale": True},
        ):
            out = self.run_warnings([info], ["cli-model-warn-codex"])
            self.assertEqual(out["cli-model-warn-codex"], {"hidden": True, "textContent": ""})

    def test_a_provider_without_a_row_is_skipped(self):
        out = self.run_warnings([{"provider": "agy", "cli_model": "x", "model_stale": True}], [])
        self.assertEqual(out, {})


class ProvidersLoaderWiringTests(unittest.TestCase):
    def test_every_providers_request_goes_through_refresh_provider_panel(self):
        # Boot used to fetch /api/providers on its own and never re-polled while agy's
        # list was still loading; one loader means every path re-polls.
        text = APP_JS.read_text(encoding="utf-8")
        self.assertEqual(text.count('fetchJSON("/api/providers")'), 1)
        body = text[text.index("async function refreshProviderPanel("):]
        self.assertIn('fetchJSON("/api/providers")', body[:900])


if __name__ == "__main__":
    unittest.main()
