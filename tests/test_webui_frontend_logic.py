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
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
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
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
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
class RunProgressTextTests(unittest.TestCase):
    """The running bubble's text, built from /api/run-progress (run_progress.py)."""

    I18N_JS = APP_JS.parent / "i18n.js"

    def text(self, progress, seconds, lang="en"):
        # Real translations: i18n.js itself, with localStorage answering for `lang`.
        script = "\n".join(
            [
                "const localStorage = {getItem() { return " + json.dumps("en" if lang == "en" else None) + "; }};",
                self.I18N_JS.read_text(encoding="utf-8"),
                _function_source("runProgressText"),
                f"process.stdout.write(JSON.stringify(runProgressText({json.dumps(progress)}, {seconds}, AHB_I18N.t)));",
            ]
        )
        result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_kind_detail_and_elapsed_time(self):
        self.assertEqual(self.text({"kind": "command", "detail": "make test"}, 7.9), "Running command: make test · 7s")

    def test_no_detail_means_no_colon(self):
        self.assertEqual(self.text({"kind": "thinking", "detail": ""}, 0), "Thinking… · 0s")

    def test_minutes_after_sixty_seconds(self):
        self.assertEqual(self.text({"kind": "writing"}, 125), "Writing the answer… · 2m 5s")

    def test_before_the_first_answer_and_for_unknown_kinds_it_says_working(self):
        self.assertEqual(self.text(None, 3), "Working… · 3s")
        self.assertEqual(self.text({"kind": "from-a-newer-server", "detail": "x"}, 3), "Working…: x · 3s")

    def test_korean(self):
        self.assertEqual(self.text({"kind": "read", "detail": "a.py"}, 61, "ko"), "파일 읽는 중: a.py · 1분 1초")


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


def _run_node_with(functions, body: str, result_var: str = "out"):
    script = "\n".join(
        [_function_source(name) for name in functions]
        + [body, f"Promise.resolve({result_var}).then((v) => process.stdout.write(JSON.stringify(v)));"]
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class ApiTransportTests(unittest.TestCase):
    """In the desktop app the page's requests go through the shell's api_request
    command instead of fetch (no network port); in a browser they stay fetches."""

    FUNCTIONS = ("decodeBase64Utf8", "createApiFetch")

    def run_ipc(self, response, init="{}", url='"/api/x"'):
        body = f"""
        const calls = [];
        const invoke = async (command, args) => {{ calls.push({{ command, args }}); return {json.dumps(response)}; }};
        const apiFetch = createApiFetch(invoke, () => {{ throw new Error("plain fetch used"); }});
        const out = (async () => {{
          const res = await apiFetch({url}, {init});
          let json = null, text = null;
          try {{ json = await res.json(); }} catch (e) {{ json = "unparseable"; }}
          text = await res.text();
          return {{ calls, ok: res.ok, status: res.status, json, text }};
        }})();
        """
        return _run_node_with(self.FUNCTIONS, body)

    def test_a_get_becomes_an_api_request_command(self):
        out = self.run_ipc({"status": 200, "headers": {}, "body": '{"a": 1}', "encoding": "utf-8"})
        self.assertEqual(out["calls"][0]["command"], "api_request")
        self.assertEqual(
            out["calls"][0]["args"],
            {"request": {"method": "GET", "path": "/api/x", "headers": {}, "body": None}},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["json"], {"a": 1})

    def test_a_post_carries_its_headers_and_body(self):
        init = '{ method: "POST", headers: { "X-AHB-Session": "s1", "Content-Type": "application/json" }, body: "{\\"text\\":\\"hi\\"}" }'
        out = self.run_ipc({"status": 200, "body": "{}", "encoding": "utf-8"}, init=init)
        request = out["calls"][0]["args"]["request"]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"]["X-AHB-Session"], "s1")
        self.assertEqual(request["body"], '{"text":"hi"}')

    def test_error_statuses_are_not_ok_but_their_json_is_still_readable(self):
        # The page does `if (!res.ok) throw new Error(data.error ...)`.
        out = self.run_ipc({"status": 409, "body": '{"error": "run in progress"}', "encoding": "utf-8"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 409)
        self.assertEqual(out["json"], {"error": "run in progress"})

    def test_base64_bodies_decode_as_utf8(self):
        import base64

        encoded = base64.b64encode("한글 ✓".encode("utf-8")).decode("ascii")
        out = self.run_ipc({"status": 200, "body": encoded, "encoding": "base64"})
        self.assertEqual(out["text"], "한글 ✓")

    def test_without_the_shell_the_plain_fetch_is_used_untouched(self):
        out = _run_node_with(
            self.FUNCTIONS,
            """
            const plain = () => "plain";
            const out = createApiFetch(null, plain) === plain;
            """,
        )
        self.assertTrue(out)


class TransportWiringTests(unittest.TestCase):
    def test_the_page_makes_no_direct_fetch_calls(self):
        # Every request must go through apiFetch so the desktop app's portless
        # transport covers it; a stray fetch() would silently hit a port that no
        # longer exists. The one plain fetch is the browser fallback.
        text = APP_JS.read_text(encoding="utf-8")
        direct = [
            line.strip()
            for line in text.splitlines()
            if "fetch(" in line and "apiFetch" not in line and "createApiFetch" not in line and not line.strip().startswith("//")
        ]
        self.assertEqual(direct, ["(url, init) => fetch(url, init)"])

    def test_the_update_link_uses_the_external_opener(self):
        text = APP_JS.read_text(encoding="utf-8")
        self.assertIn("openExternal(latestUpdateInfo.url)", text)
        self.assertNotIn("window.open(latestUpdateInfo.url", text)


# The source zips carry webui/ but not the Tauri shell, so the checks that read
# src-tauri/ run only in a full checkout. The test is skipped only when the whole
# directory is absent: a checkout that lost one file in it still fails loudly.
HAS_TAURI_SOURCES = (Path(__file__).resolve().parent.parent / "src-tauri").is_dir()
needs_tauri_sources = unittest.skipUnless(HAS_TAURI_SOURCES, "src-tauri/ is not part of this copy")


class ContentSecurityPolicyTests(unittest.TestCase):
    """The desktop app loads webui/ under a Content-Security-Policy that has no
    'unsafe-inline' (src-tauri/tauri.conf.json). These pin what makes that
    possible, so a later edit cannot silently break the app's page."""

    ROOT = Path(__file__).resolve().parent.parent
    INDEX = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")

    def csp(self):
        config = json.loads((self.ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        return config["app"]["security"]["csp"]

    def test_the_page_has_no_inline_script_style_or_event_handlers(self):
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", self.INDEX), [], "inline <script>")
        self.assertEqual(re.findall(r'\sstyle="', self.INDEX), [], "inline style attribute")
        self.assertEqual(re.findall(r"<style\b", self.INDEX), [], "inline <style>")
        self.assertEqual(re.findall(r'\son[a-z]+="', self.INDEX), [], "inline event handler")

    def test_the_javascript_never_sets_a_style_attribute_as_text(self):
        for name in ("app.js", "i18n.js", "theme-init.js"):
            text = (self.ROOT / "webui" / name).read_text(encoding="utf-8")
            self.assertNotIn('setAttribute("style"', text, name)
            self.assertNotIn("cssText", text, name)
            self.assertNotIn("eval(", text, name)

    @needs_tauri_sources
    def test_the_tauri_csp_has_no_inline_or_eval_allowance(self):
        csp = self.csp()
        self.assertIsInstance(csp, str, "csp must be set (it used to be null)")
        directives = {part.split()[0]: part.split()[1:] for part in csp.split(";") if part.strip()}
        for name in ("script-src", "style-src", "default-src"):
            self.assertNotIn("'unsafe-inline'", directives.get(name, []), name)
            self.assertNotIn("'unsafe-eval'", directives.get(name, []), name)
        self.assertEqual(directives["script-src"], ["'self'"])
        self.assertNotIn("*", csp.replace("'self'", ""))
        self.assertEqual(directives["object-src"], ["'none'"])

    @needs_tauri_sources
    def test_the_tauri_csp_still_lets_the_page_reach_the_ipc_and_its_fonts(self):
        directives = {part.split()[0]: part.split()[1:] for part in self.csp().split(";") if part.strip()}
        self.assertIn("ipc:", directives["connect-src"])
        self.assertIn("http://ipc.localhost", directives["connect-src"])  # the same channel on Windows
        # app.css @imports these two stylesheets (which pull in their font files).
        css = (self.ROOT / "webui" / "app.css").read_text(encoding="utf-8")
        for host in re.findall(r"@import url\(\"(https://[^/]+)", css):
            self.assertIn(host, directives["style-src"], host)
        self.assertTrue(any(h.startswith("https://") for h in directives["font-src"]))

    @needs_tauri_sources
    def test_the_desktop_shell_has_no_http_mode_left(self):
        # The window shows only the bundled page and talks to the sidecar over
        # stdin/stdout. A `remote` capability entry or an External window URL would
        # bring back a localhost page with the app's permissions.
        capability = json.loads((self.ROOT / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8"))
        self.assertNotIn("remote", capability)
        shell = (self.ROOT / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
        code = "\n".join(line for line in shell.splitlines() if not line.lstrip().startswith("//"))
        for gone in ("WebviewUrl::External", "--no-browser", "8787", "AHB_TRANSPORT", "SERVER_URL", "port_conflict"):
            self.assertNotIn(gone, code, gone)
        self.assertIn('.args(["--stdio"])', code)
        self.assertIn("WebviewUrl::App", code)

    def test_every_local_script_and_stylesheet_exists_in_webui(self):
        for ref in re.findall(r'(?:src|href)="(/[^"]+)"', self.INDEX):
            self.assertTrue((self.ROOT / "webui" / ref.lstrip("/")).is_file(), ref)


class FillsTheWindowTests(unittest.TestCase):
    """The page must use the whole window. It used to be a card (max 1600x1000,
    page padding, border, rounded corners, shadow) centered on a grey page, which
    looked like a block floating in the middle of a bigger window."""

    CSS = (Path(__file__).resolve().parent.parent / "webui" / "app.css").read_text(encoding="utf-8")

    def rule(self, selector):
        match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", self.CSS)
        self.assertIsNotNone(match, f"no {selector} rule")
        return match.group(1)

    def test_the_app_container_is_as_big_as_the_window_and_has_no_card_styling(self):
        app = self.rule(".app")
        self.assertRegex(app, r"width:\s*100%")
        self.assertRegex(app, r"height:\s*100%")
        for card_property in ("max-width", "max-height", "border-radius", "box-shadow", "border:", "margin"):
            self.assertNotIn(card_property, app, card_property)

    def test_the_page_does_not_center_the_app_or_pad_around_it(self):
        body = self.rule("body")
        for centering in ("align-items", "justify-content", "padding", "display: flex"):
            self.assertNotIn(centering, body, centering)

    def test_no_viewport_sized_cap_is_left_on_the_app_layout(self):
        self.assertNotIn("min(1600px", self.CSS)
        self.assertNotIn("min(1000px", self.CSS)


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
