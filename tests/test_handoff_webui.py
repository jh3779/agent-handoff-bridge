#!/usr/bin/env python3
"""Unit + live-server tests for handoff_webui.py.

This is the MVP's actual security boundary (path traversal into the host
filesystem via HTTP query params), so it gets both unit coverage on the
pure logic and a real end-to-end request against a live server -- the same
"don't just trust it, run it" pattern used for the release zip in
docs/release-process.md. Run with:
python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_webui as webui  # noqa: E402


class ChooseUiModeTests(unittest.TestCase):
    def test_prefers_native_when_webview_available(self):
        self.assertEqual(webui.choose_ui_mode(prefer_browser=False, webview_available=True), "native")

    def test_falls_back_to_browser_when_webview_unavailable(self):
        self.assertEqual(webui.choose_ui_mode(prefer_browser=False, webview_available=False), "browser")

    def test_browser_flag_forces_browser_even_if_webview_available(self):
        self.assertEqual(webui.choose_ui_mode(prefer_browser=True, webview_available=True), "browser")

    def test_browser_flag_with_no_webview_is_still_browser(self):
        self.assertEqual(webui.choose_ui_mode(prefer_browser=True, webview_available=False), "browser")


class IsLoopbackHostTests(unittest.TestCase):
    def test_ipv4_loopback_is_allowed(self):
        self.assertTrue(webui.is_loopback_host("127.0.0.1"))

    def test_localhost_is_allowed(self):
        self.assertTrue(webui.is_loopback_host("localhost"))

    def test_ipv6_loopback_is_allowed(self):
        self.assertTrue(webui.is_loopback_host("::1"))

    def test_wildcard_bind_is_rejected(self):
        self.assertFalse(webui.is_loopback_host("0.0.0.0"))

    def test_lan_address_is_rejected(self):
        self.assertFalse(webui.is_loopback_host("192.168.1.50"))

    def test_empty_string_is_rejected(self):
        self.assertFalse(webui.is_loopback_host(""))


class MainRefusesNonLoopbackHostTests(unittest.TestCase):
    """Integration-level: main() must refuse before ever opening a socket,
    so this must return fast and never hang waiting on a server thread."""

    def test_main_returns_error_for_wildcard_host_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = webui.main(["--workspace", tmp, "--host", "0.0.0.0", "--no-browser"])
        self.assertEqual(exit_code, 1)

    def test_main_returns_error_for_lan_host_without_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = webui.main(["--workspace", tmp, "--host", "192.168.1.50", "--no-browser"])
        self.assertEqual(exit_code, 1)

    def test_host_validation_happens_before_workspace_validation(self):
        # A bad host with a bad workspace should fail on the host check
        # (cheaper, and the more important guard) -- not silently pass
        # through to the workspace check first.
        exit_code = webui.main(["--workspace", "/does/not/exist", "--host", "0.0.0.0", "--no-browser"])
        self.assertEqual(exit_code, 1)


class SafeJoinTests(unittest.TestCase):
    def test_relative_path_within_root_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "file.txt").write_text("hi", encoding="utf-8")
            resolved = webui.safe_join(root, "sub/file.txt")
            self.assertEqual(resolved, (root / "sub" / "file.txt").resolve())

    def test_empty_path_resolves_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(webui.safe_join(root, ""), root.resolve())

    def test_dotdot_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            with self.assertRaises(webui.WorkspaceError):
                webui.safe_join(root, "../../etc/passwd")

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(webui.WorkspaceError):
                webui.safe_join(root, "/etc/passwd")

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = root / "escape"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this platform/runner")
            with self.assertRaises(webui.WorkspaceError):
                webui.safe_join(root, "escape/secret.txt")


class ListTreeEntriesTests(unittest.TestCase):
    def test_dirs_before_files_alphabetical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zeta.txt").write_text("z", encoding="utf-8")
            (root / "alpha.txt").write_text("a", encoding="utf-8")
            (root / "beta_dir").mkdir()
            entries = webui.list_tree_entries(root, "")
            names = [e["name"] for e in entries]
            self.assertEqual(names, ["beta_dir", "alpha.txt", "zeta.txt"])
            self.assertEqual(entries[0]["type"], "dir")
            self.assertEqual(entries[1]["type"], "file")

    def test_excluded_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "src").mkdir()
            names = [e["name"] for e in webui.list_tree_entries(root, "")]
            self.assertNotIn(".git", names)
            self.assertIn("src", names)

    def test_nonexistent_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui.WorkspaceError):
                webui.list_tree_entries(Path(tmp), "nope")

    def test_file_path_raises_not_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(webui.WorkspaceError):
                webui.list_tree_entries(root, "f.txt")


class ReadFilePreviewTests(unittest.TestCase):
    def test_text_file_returns_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello world", encoding="utf-8")
            preview = webui.read_file_preview(root, "notes.md")
            self.assertEqual(preview["content"], "hello world")
            self.assertFalse(preview["truncated"])

    def test_binary_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
            with self.assertRaises(webui.WorkspaceError):
                webui.read_file_preview(root, "bin.dat")

    def test_oversized_file_is_truncated_not_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_max = webui.MAX_FILE_BYTES
            webui.MAX_FILE_BYTES = 10
            try:
                (root / "big.txt").write_text("0123456789" * 5, encoding="utf-8")
                preview = webui.read_file_preview(root, "big.txt")
                self.assertTrue(preview["truncated"])
                self.assertEqual(len(preview["content"]), 10)
            finally:
                webui.MAX_FILE_BYTES = original_max

    def test_directory_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            with self.assertRaises(webui.WorkspaceError):
                webui.read_file_preview(root, "sub")


class LiveServerTests(unittest.TestCase):
    """Exercises the actual HTTP layer, not just the pure functions above."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "README.md").write_text("# hello\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")

        cls.state = webui.AppState(root)
        handler = webui.build_handler(cls.state)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_index_page_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"Agent Handoff Bridge", resp.read())

    def test_api_info(self):
        status, data = self._get("/api/info")
        self.assertEqual(status, 200)
        self.assertIn("workspace", data)

    def test_api_tree_lists_real_entries(self):
        status, data = self._get("/api/tree?path=")
        self.assertEqual(status, 200)
        names = {e["name"] for e in data["entries"]}
        self.assertEqual(names, {"README.md", "src"})

    def test_api_file_returns_content(self):
        status, data = self._get("/api/file?path=README.md")
        self.assertEqual(status, 200)
        self.assertEqual(data["content"], "# hello\n")

    def test_api_tree_traversal_rejected_over_http(self):
        status, data = self._get("/api/tree?path=../../../../etc")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_api_file_absolute_path_rejected_over_http(self):
        status, data = self._get("/api/file?path=/etc/passwd")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_post_is_rejected(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/tree", method="POST", data=b"{}")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        with ctx.exception:
            self.assertEqual(ctx.exception.code, 405)


class ValidateWorkspaceCandidateTests(unittest.TestCase):
    def test_valid_absolute_directory_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = webui.validate_workspace_candidate(tmp)
            self.assertEqual(resolved, Path(tmp).resolve())

    def test_empty_path_rejected(self):
        with self.assertRaises(webui.WorkspaceError):
            webui.validate_workspace_candidate("")

    def test_relative_path_rejected(self):
        with self.assertRaises(webui.WorkspaceError):
            webui.validate_workspace_candidate("relative/dir")

    def test_nonexistent_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui.WorkspaceError):
                webui.validate_workspace_candidate(str(Path(tmp) / "does-not-exist"))

    def test_file_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "f.txt"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(webui.WorkspaceError):
                webui.validate_workspace_candidate(str(file_path))


class HasHandoffMarkerTests(unittest.TestCase):
    def test_true_when_dot_handoff_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            self.assertTrue(webui.has_handoff_marker(root))

    def test_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(webui.has_handoff_marker(Path(tmp)))

    def test_false_when_dot_handoff_is_a_file_not_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").write_text("not a dir", encoding="utf-8")
            self.assertFalse(webui.has_handoff_marker(root))


class ResolveStartupWorkspaceTests(unittest.TestCase):
    def test_explicit_valid_path_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, error = webui.resolve_startup_workspace(tmp, Path("/irrelevant"))
            self.assertIsNone(error)
            self.assertEqual(workspace, Path(tmp).resolve())

    def test_explicit_invalid_path_is_an_error_not_auto_create(self):
        # DEC-04: an explicit --workspace typo must fail loudly, never
        # silently fall into the "no workspace" auto-create flow -- an
        # explicit path means the user is confident about where they meant
        # to point.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            workspace, error = webui.resolve_startup_workspace(str(missing), Path("/irrelevant"))
            self.assertIsNone(workspace)
            self.assertIsNotNone(error)
            self.assertIn("does not exist", error)

    def test_no_arg_with_initialized_cwd_resolves_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".handoff").mkdir()
            workspace, error = webui.resolve_startup_workspace(None, cwd)
            self.assertIsNone(error)
            self.assertEqual(workspace, cwd.resolve())

    def test_no_arg_with_uninitialized_cwd_returns_no_workspace_not_an_error(self):
        # 2026-08-04 DEC-04 revision: "cwd invalid" barely ever happens (a
        # running process's cwd essentially always exists), which would
        # make the whole Phase 2 flow unreachable in practice -- the real
        # condition is "not yet an initialized handoff workspace".
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)  # no .handoff/ -- e.g. launcher double-clicked from Downloads
            workspace, error = webui.resolve_startup_workspace(None, cwd)
            self.assertIsNone(workspace)
            self.assertIsNone(error)


class SlugifyForFolderNameTests(unittest.TestCase):
    def test_ascii_text_becomes_hyphenated_slug(self):
        self.assertEqual(webui.slugify_for_folder_name("Fix the deploy script"), "Fix-the-deploy-script")

    def test_korean_text_is_preserved(self):
        # DEC-05's whole point: unlike a typical ASCII-only slugify library
        # that would strip/transliterate this to nothing, \w is
        # Unicode-aware and keeps Hangul -- matching the wireframe's own
        # example folder name.
        slug = webui.slugify_for_folder_name("배포 스크립트에 있는 버그를 점검해줘.")
        self.assertEqual(slug, "배포-스크립트에-있는-버그를-점검해줘")

    def test_empty_text_becomes_untitled(self):
        self.assertEqual(webui.slugify_for_folder_name(""), "untitled")

    def test_whitespace_only_becomes_untitled(self):
        self.assertEqual(webui.slugify_for_folder_name("   \n\t  "), "untitled")

    def test_punctuation_only_becomes_untitled(self):
        self.assertEqual(webui.slugify_for_folder_name("... !!! ???"), "untitled")

    def test_long_text_is_truncated_to_max_length(self):
        slug = webui.slugify_for_folder_name("word " * 30)
        self.assertLessEqual(len(slug), webui.MAX_SLUG_LENGTH)
        self.assertFalse(slug.endswith("-"))


class BuildAutoWorkspaceNameTests(unittest.TestCase):
    def test_uses_text_when_present(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui.build_auto_workspace_name("Fix bug", [], now)
        self.assertEqual(name, "2026-08-04-Fix-bug")

    def test_falls_back_to_attachment_name_when_no_text(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui.build_auto_workspace_name("", [{"name": "report.pdf"}], now)
        self.assertEqual(name, "2026-08-04-report-pdf")

    def test_falls_back_to_untitled_when_neither_text_nor_attachments(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui.build_auto_workspace_name("", [], now)
        self.assertEqual(name, "2026-08-04-untitled")


class ResolveTaskForFirstMessageTests(unittest.TestCase):
    def test_uses_text_when_present(self):
        self.assertEqual(webui.resolve_task_for_first_message("Fix bug", []), "Fix bug")

    def test_attachments_only_produces_a_meaningful_task_not_a_generic_placeholder(self):
        # Regression: the folder name already fell back to the attachment's
        # name (build_auto_workspace_name), but state.json's task -- which
        # feeds every future prompt's "## Task" section -- used to ignore
        # that and always record the generic placeholder for an
        # attachments-only first message.
        task = webui.resolve_task_for_first_message("", [{"name": "report.pdf"}])
        self.assertIn("report.pdf", task)
        self.assertNotEqual(task, "Continue the current handoff task.")

    def test_falls_back_to_placeholder_when_neither_text_nor_attachments(self):
        self.assertEqual(webui.resolve_task_for_first_message("", []), "Continue the current handoff task.")


class CreateWorkspaceForFirstMessageTests(unittest.TestCase):
    """AUTO_WORKSPACE_BASE_DIR is patched to a tempdir for every test here
    -- these must never touch the real ~/Documents/Agent Handoff Bridge/ on
    whatever machine runs the suite."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name) / "Agent Handoff Bridge"
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_creates_directory_under_base_dir_with_date_and_slug(self):
        workspace = webui.create_workspace_for_first_message("Fix the deploy script", [])
        self.assertEqual(workspace.parent, self.base_dir)
        self.assertTrue(workspace.is_dir())
        self.assertIn("Fix-the-deploy-script", workspace.name)

    def test_runs_init_and_produces_state_json_with_task_set_to_the_message(self):
        workspace = webui.create_workspace_for_first_message("investigate the flaky test", [])
        state_path = workspace / ".handoff" / "state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["task"], "investigate the flaky test")

    def test_attachments_only_message_records_a_meaningful_task(self):
        workspace = webui.create_workspace_for_first_message("", [{"name": "report.pdf"}])
        state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
        self.assertIn("report.pdf", state["task"])

    def test_produces_current_md_too_not_just_state_json(self):
        # init_handoff() writes both unconditionally on success -- the
        # explicit post-condition check in create_workspace_for_first_message()
        # relies on that being true.
        workspace = webui.create_workspace_for_first_message("hello", [])
        self.assertTrue((workspace / ".handoff" / "current.md").exists())

    def test_collision_appends_numeric_suffix_and_never_reuses_the_folder(self):
        first = webui.create_workspace_for_first_message("same summary", [])
        second = webui.create_workspace_for_first_message("same summary", [])
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue(second.name.endswith("-2"))

    def test_ensures_chat_gitignore_is_written(self):
        workspace = webui.create_workspace_for_first_message("hello", [])
        self.assertTrue((workspace / ".handoff" / "webui" / ".gitignore").exists())

    def test_message_matching_an_init_flag_name_is_still_treated_as_the_task(self):
        # Without "--" before the task in the subprocess argv, argparse
        # would consume a first message that's literally "--no-install"
        # (or any other real flag of `init`) as that option instead of the
        # positional task, and fail with "the following arguments are
        # required: task" -- a real user message shouldn't be able to
        # break scaffolding just by looking like a CLI flag.
        workspace = webui.create_workspace_for_first_message("--no-install", [])
        state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["task"], "--no-install")

    def test_base_dir_creation_failure_becomes_a_workspace_error(self):
        # AUTO_WORKSPACE_BASE_DIR.mkdir()/new_workspace.mkdir() used to sit
        # outside the try block -- an OSError there (e.g. the base dir path
        # exists as a *file*, permissions, a full disk) would propagate
        # uncaught instead of becoming the same clean WorkspaceError -> 400
        # JSON every other failure path here produces.
        self.base_dir.parent.mkdir(parents=True, exist_ok=True)
        self.base_dir.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(webui.WorkspaceError):
            webui.create_workspace_for_first_message("hello", [])

    def test_init_subprocess_failure_raises_and_cleans_up_the_directory(self):
        # Regression: the subprocess result used to be discarded entirely --
        # a failing `init` (bad permissions, disk full, a bug in
        # handoff_bridge.py) would silently leave a half-scaffolded
        # directory that append_chat_message() then wrote into as if it
        # were a real workspace.
        failed = mock.Mock(returncode=1, stdout="", stderr="boom: disk full")
        with mock.patch("handoff_webui.subprocess.run", return_value=failed):
            with self.assertRaises(webui.WorkspaceError) as ctx:
                webui.create_workspace_for_first_message("hello", [])
        self.assertIn("boom: disk full", str(ctx.exception))
        # and it didn't leave an orphaned empty folder behind
        self.assertEqual(list(self.base_dir.iterdir()), [])

    def test_init_subprocess_timeout_raises_and_cleans_up_the_directory(self):
        with mock.patch(
            "handoff_webui.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="handoff_bridge.py", timeout=30),
        ):
            with self.assertRaises(webui.WorkspaceError):
                webui.create_workspace_for_first_message("hello", [])
        self.assertEqual(list(self.base_dir.iterdir()), [])

    def test_exit_zero_without_the_expected_handoff_files_is_still_a_failure(self):
        # Defense in depth: don't trust the exit code alone. A "successful"
        # init that -- for whatever reason -- didn't actually produce
        # .handoff/state.json or .handoff/current.md must not be confirmed
        # as a real workspace (docs/architecture.md: those are the durable
        # handoff surface).
        fake_success_but_did_nothing = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("handoff_webui.subprocess.run", return_value=fake_success_but_did_nothing):
            with self.assertRaises(webui.WorkspaceError) as ctx:
                webui.create_workspace_for_first_message("hello", [])
        self.assertIn(".handoff/", str(ctx.exception))
        self.assertEqual(list(self.base_dir.iterdir()), [])


class CreateWorkspaceConcurrencyTests(unittest.TestCase):
    """A real live server + real concurrent HTTP requests -- verifies the
    exact race an adversarial review reproduced: two near-simultaneous
    first messages (double-clicked Send, two browser tabs against the same
    server) both observing AppState.workspace as None. AUTO_WORKSPACE_BASE_DIR
    is patched to a tempdir so this never touches the real
    ~/Documents/Agent Handoff Bridge/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name) / "Agent Handoff Bridge"
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

        self.state = webui.AppState(None)
        handler = webui.build_handler(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_concurrent_first_messages_create_exactly_one_workspace(self):
        url = f"http://127.0.0.1:{self.port}/api/chat"
        payload = json.dumps({"role": "user", "text": "same topic", "attachments": []}).encode("utf-8")
        statuses = []
        errors = []
        lock = threading.Lock()

        def send():
            try:
                req = urllib.request.Request(
                    url, data=payload, method="POST", headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
            except Exception as exc:  # pragma: no cover - failure path surfaced via errors list
                with lock:
                    errors.append(str(exc))
                return
            with lock:
                statuses.append(status)

        threads = [threading.Thread(target=send) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(statuses, [200] * 8)
        # Exactly one workspace directory got created, not one per request
        # -- the bug this regression-tests produced up to 8 real folders on
        # disk with AppState.workspace pointing at only one of them.
        self.assertEqual(len(list(self.base_dir.iterdir())), 1)
        self.assertIsNotNone(self.state.workspace)


class ChatStorageTests(unittest.TestCase):
    def test_append_then_read_current_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
            saved = webui.append_chat_message(root, "user", "hello", [], now)
            self.assertEqual(saved["role"], "user")
            self.assertEqual(saved["text"], "hello")
            self.assertIn("id", saved)

            messages = webui.read_month_messages(root, "2026-08")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["text"], "hello")

    def test_multiple_appends_preserve_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "first", [], now)
            webui.append_chat_message(root, "user", "second", [], now)
            messages = webui.read_month_messages(root, "2026-08")
            self.assertEqual([m["text"] for m in messages], ["first", "second"])

    def test_invalid_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui.WorkspaceError):
                webui.append_chat_message(Path(tmp), "assistant", "hi", [], utc_now_for_test())

    def test_read_missing_month_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui.read_month_messages(Path(tmp), "2020-01"), [])

    def test_list_available_months_sees_both_plain_and_compressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat_dir = webui.chat_dir(root)
            chat_dir.mkdir(parents=True)
            (chat_dir / "2026-07.jsonl.gz").write_bytes(b"")
            (chat_dir / "2026-08.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(webui.list_available_months(root), ["2026-07", "2026-08"])

    def test_archive_compresses_past_months_but_not_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "old message", [], july)
            webui.append_chat_message(root, "user", "new message", [], august)

            archived = webui.archive_old_months(root, august)

            self.assertEqual(archived, ["2026-07"])
            chat_dir = webui.chat_dir(root)
            self.assertTrue((chat_dir / "2026-07.jsonl.gz").exists())
            self.assertFalse((chat_dir / "2026-07.jsonl").exists())
            self.assertTrue((chat_dir / "2026-08.jsonl").exists())

    def test_archived_month_still_readable_after_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "archive me", [], july)
            webui.archive_old_months(root, august)

            messages = webui.read_month_messages(root, "2026-07")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["text"], "archive me")

    def test_archive_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "msg", [], july)
            first = webui.archive_old_months(root, august)
            second = webui.archive_old_months(root, august)
            self.assertEqual(first, ["2026-07"])
            self.assertEqual(second, [])  # nothing left to compress

    def test_archive_compresses_every_past_month_not_just_the_last_one(self):
        # Regression test: a prior version of archive_old_months() had
        # path.unlink()/archived.append(month) indented one level too
        # shallow, outside the for-loop -- so only the last-iterated month
        # actually got archived. A single-old-month test can't catch that
        # (the "last iteration" and "only iteration" are the same thing),
        # so this one uses three.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            may_ = datetime(2026, 5, 10, tzinfo=timezone.utc)
            june = datetime(2026, 6, 10, tzinfo=timezone.utc)
            july = datetime(2026, 7, 10, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "may", [], may_)
            webui.append_chat_message(root, "user", "june", [], june)
            webui.append_chat_message(root, "user", "july", [], july)
            webui.append_chat_message(root, "user", "august", [], august)

            archived = webui.archive_old_months(root, august)

            self.assertEqual(sorted(archived), ["2026-05", "2026-06", "2026-07"])
            chat_dir = webui.chat_dir(root)
            for month in ("2026-05", "2026-06", "2026-07"):
                self.assertTrue((chat_dir / f"{month}.jsonl.gz").exists(), f"{month} not compressed")
                self.assertFalse((chat_dir / f"{month}.jsonl").exists(), f"{month} plain file still present")
            self.assertTrue((chat_dir / "2026-08.jsonl").exists())
            for month in ("2026-05", "2026-06", "2026-07"):
                messages = webui.read_month_messages(root, month)
                self.assertEqual(len(messages), 1, f"{month} lost its message")


def _write_fake_provider(bin_dir: Path, name: str, script_body: str) -> None:
    script = bin_dir / name
    script.write_text(script_body, encoding="utf-8")
    script.chmod(0o755)


class FakeProviderPathMixin:
    """Prepends a temp dir with fake `codex`/`claude` shell scripts onto
    PATH so run_provider_via_bridge()'s real subprocess call resolves to
    them instead of any real provider CLI -- deterministic, no tokens
    spent, no network. Skips on platforms without a POSIX shell (this
    project's CI only runs ubuntu-latest today, but keep this honest)."""

    def setUpFakeProviders(self):
        if os.name != "posix" or not shutil.which("sh"):
            self.skipTest("POSIX shell not available for fake provider scripts")
        self.fake_bin = Path(tempfile.mkdtemp())
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.fake_bin}{os.pathsep}{self._old_path}"
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        os.environ["PATH"] = self._old_path
        shutil.rmtree(self.fake_bin, ignore_errors=True)


FAKE_CODEX_SUCCESS = """#!/bin/sh
cat >/dev/null
cat <<'EOF'
{"type": "thread.started", "thread_id": "fake-codex-session"}
{"type": "item.completed", "item": {"type": "agent_message", "text": "fake codex reply"}}
{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
EOF
"""

FAKE_CODEX_RATE_LIMITED = """#!/bin/sh
cat >/dev/null
echo "Error: rate limit exceeded (429)"
exit 1
"""

FAKE_CLAUDE_SUCCESS = """#!/bin/sh
cat >/dev/null
cat <<'EOF'
{"type": "system", "subtype": "init", "session_id": "fake-claude-session"}
{"type": "result", "session_id": "fake-claude-session", "result": "fake claude reply", "total_cost_usd": 0.0, "is_error": false}
EOF
"""


class ClassifyRunStatusTests(unittest.TestCase):
    def test_no_handoff_needed_is_success(self):
        self.assertEqual(webui.classify_run_status(False, "none: no handoff signal detected"), "success")

    def test_tool_failure_is_fail(self):
        self.assertEqual(webui.classify_run_status(True, "tool_failure: provider command not found"), "fail")

    def test_unknown_is_fail(self):
        self.assertEqual(webui.classify_run_status(True, "unknown: provider emitted an unrecognized error"), "fail")

    def test_rate_limit_is_handoff(self):
        self.assertEqual(webui.classify_run_status(True, "rate_limit: matched rate_limit signal"), "handoff")

    def test_quota_is_handoff(self):
        self.assertEqual(webui.classify_run_status(True, "quota: matched quota signal"), "handoff")


class BuildRunPromptTests(unittest.TestCase):
    def test_text_only_passes_through_unchanged(self):
        self.assertEqual(webui.build_run_prompt("hello", []), "hello")

    def test_attachment_content_is_included(self):
        prompt = webui.build_run_prompt(
            "look at this", [{"name": "a.py", "path": "a.py", "content": "print(1)", "truncated": False}]
        )
        self.assertIn("look at this", prompt)
        self.assertIn("a.py", prompt)
        self.assertIn("print(1)", prompt)

    def test_truncated_attachment_is_noted(self):
        prompt = webui.build_run_prompt(
            "", [{"name": "big.txt", "path": "big.txt", "content": "...", "truncated": True}]
        )
        self.assertIn("(truncated)", prompt)

    def test_binary_attachment_with_no_content_is_noted_not_dropped(self):
        prompt = webui.build_run_prompt("", [{"name": "image.png", "path": "image.png", "content": None}])
        self.assertIn("image.png", prompt)
        self.assertIn("no preview available", prompt)

    def test_attachment_only_with_no_text_still_produces_a_prompt(self):
        prompt = webui.build_run_prompt("", [{"name": "a.py", "path": "a.py", "content": "x = 1", "truncated": False}])
        self.assertTrue(prompt.strip())
        self.assertIn("x = 1", prompt)


class ReadStateHistoryTests(unittest.TestCase):
    def test_missing_state_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui.read_state_history(Path(tmp)), [])

    def test_malformed_state_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            (root / ".handoff" / "state.json").write_text("not json", encoding="utf-8")
            self.assertEqual(webui.read_state_history(root), [])

    def test_reads_history_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            (root / ".handoff" / "state.json").write_text(
                json.dumps({"history": [{"provider": "codex"}]}), encoding="utf-8"
            )
            self.assertEqual(webui.read_state_history(root), [{"provider": "codex"}])


class RunProviderViaBridgeTests(FakeProviderPathMixin, unittest.TestCase):
    def setUp(self):
        self.setUpFakeProviders()

    def test_concurrent_call_fails_fast_instead_of_blocking_or_racing(self):
        # Regression test: run_provider_via_bridge() used to diff
        # .handoff/state.json's history length before/after the subprocess
        # call with no lock -- two concurrent calls could both read the
        # same "before" length and duplicate an already-persisted record.
        # A held _RUN_LOCK must make a second call fail immediately
        # (RunAlreadyInProgressError), not block for the full timeout or
        # silently race.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            webui._RUN_LOCK.acquire()
            try:
                with self.assertRaises(webui.RunAlreadyInProgressError):
                    webui.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            finally:
                webui._RUN_LOCK.release()

    def test_lock_is_released_after_a_normal_call_so_the_next_one_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            webui.run_provider_via_bridge(root, "codex", "first", None, "continue")
            # Would raise RunAlreadyInProgressError if the lock leaked.
            records = webui.run_provider_via_bridge(root, "codex", "second", None, "continue")
            self.assertEqual(len(records), 1)

    def test_delegates_provider_timeout_to_the_bridge(self):
        # Killing only the outer handoff_bridge.py wrapper on timeout does
        # NOT kill the real codex/claude child it spawned (neither process
        # runs in its own process group) -- --timeout-seconds must be
        # forwarded so the bridge applies the timeout to the actual
        # provider subprocess.run() call, which can really terminate it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            with mock.patch("handoff_webui.subprocess.run", wraps=subprocess.run) as spy:
                webui.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            command = spy.call_args.args[0]
            self.assertIn("--timeout-seconds", command)
            idx = command.index("--timeout-seconds")
            self.assertEqual(command[idx + 1], str(webui.PROVIDER_RUN_TIMEOUT_SECONDS))
            self.assertEqual(spy.call_args.kwargs["timeout"], webui.OUTER_SUBPROCESS_TIMEOUT_SECONDS)

    def test_attachment_content_reaches_the_actual_prompt_file(self):
        # build_run_prompt() has its own unit tests (BuildRunPromptTests);
        # this closes the loop by checking run_provider_via_bridge() writes
        # that exact combined text into the --prompt-file the bridge reads
        # from -- not just that the two pieces work in isolation.
        captured = {}
        real_run = subprocess.run  # patching handoff_webui.subprocess.run patches this module's too (same object)

        def _capture_prompt_file_then_run(command, **kwargs):
            idx = command.index("--prompt-file")
            captured["prompt_text"] = Path(command[idx + 1]).read_text(encoding="utf-8")
            return real_run(command, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            prompt = webui.build_run_prompt(
                "do the thing", [{"name": "a.py", "path": "a.py", "content": "print('hi')", "truncated": False}]
            )
            with mock.patch("handoff_webui.subprocess.run", side_effect=_capture_prompt_file_then_run):
                webui.run_provider_via_bridge(root, "codex", prompt, None, "continue")

        self.assertIn("do the thing", captured["prompt_text"])
        self.assertIn("a.py", captured["prompt_text"])
        self.assertIn("print('hi')", captured["prompt_text"])

    def test_successful_run_produces_one_history_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            records = webui.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["provider"], "codex")
            self.assertFalse(records[0]["handoff_needed"])
            self.assertEqual(records[0]["final_text"], "fake codex reply")

    def test_auto_fallback_chains_into_a_second_provider(self):
        # This is the real integration test for Phase 1's headline feature:
        # a rate-limited codex run should auto-fallback into claude within
        # a single run_provider_via_bridge() call, producing two records.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_RATE_LIMITED)
            _write_fake_provider(self.fake_bin, "claude", FAKE_CLAUDE_SUCCESS)
            records = webui.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["provider"], "codex")
            self.assertTrue(records[0]["handoff_needed"])
            self.assertTrue(records[0]["reason"].startswith("rate_limit"))
            self.assertEqual(records[1]["provider"], "claude")
            self.assertFalse(records[1]["handoff_needed"])
            self.assertEqual(records[1]["final_text"], "fake claude reply")

    def test_nonexistent_workspace_falls_back_to_synthetic_record(self):
        # handoff_bridge.py itself exits before ever writing to state.json
        # when --workspace doesn't exist -- run_provider_via_bridge() must
        # still return something rather than an empty list.
        records = webui.run_provider_via_bridge(
            Path("/definitely/does/not/exist"), "codex", "hello", None, "continue"
        )
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["handoff_needed"])
        self.assertIn("tool_failure", records[0]["reason"])

    def test_synthetic_record_resolves_auto_never_persists_auto_literal(self):
        # docs/webui-chat-storage.md: the `provider` field on an agent
        # message is "never `auto`; that's resolved to a real provider
        # before the record exists." The no-history synthetic-record branch
        # of run_provider_via_bridge() used to write the raw `provider`
        # argument straight into the record, so a caller that requested
        # "auto" and hit a subprocess failure before any history was ever
        # written would leak "auto" into a chat-log record. Regression test
        # for resolving it via choose_auto_provider() instead.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        records = webui.run_provider_via_bridge(
            Path("/definitely/does/not/exist"), "auto", "hello", None, "continue"
        )
        self.assertEqual(len(records), 1)
        self.assertIn(records[0]["provider"], ("codex", "claude"))
        self.assertNotEqual(records[0]["provider"], "auto")

    def test_timeout_after_partial_history_appends_synthetic_notice(self):
        # Simulates the outer 600s timeout firing mid-auto-fallback: codex's
        # own record already made it to state.json, but the recursive claude
        # call hangs. Without the timeout branch, the caller would silently
        # see only the codex record and have no idea a fallback ever started.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / ".handoff" / "state.json"
            state_path.parent.mkdir(parents=True)

            def _seed_partial_history_then_hang(*args, **kwargs):
                state_path.write_text(
                    json.dumps(
                        {
                            "history": [
                                {
                                    "provider": "codex",
                                    "model": "app-selected default",
                                    "instruction_type": "continue",
                                    "exit_code": 1,
                                    "session_id": None,
                                    "final_text": "",
                                    "handoff_needed": True,
                                    "reason": "rate_limit: matched rate_limit signal",
                                    "run_dir": None,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                raise subprocess.TimeoutExpired(cmd="handoff_bridge.py", timeout=600)

            with mock.patch("handoff_webui.subprocess.run", side_effect=_seed_partial_history_then_hang):
                records = webui.run_provider_via_bridge(root, "codex", "hello", None, "continue")

            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["provider"], "codex")
            self.assertEqual(records[1]["provider"], "claude")
            self.assertTrue(records[1]["handoff_needed"])
            self.assertTrue(records[1]["final_text"].startswith("Timed out"))


class ApiRunLiveServerTests(FakeProviderPathMixin, unittest.TestCase):
    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = webui.AppState(self.root)
        handler = webui.build_handler(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_run_persists_and_returns_agent_message(self):
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        status, data = self._post("/api/run", {"provider": "codex", "text": "do the thing"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["messages"]), 1)
        message = data["messages"][0]
        self.assertEqual(message["role"], "agent")
        self.assertEqual(message["provider"], "codex")
        self.assertEqual(message["status"], "success")
        self.assertEqual(message["text"], "fake codex reply")

        # and it's actually on disk, readable via GET /api/chat like any
        # other persisted message
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat")
        with urllib.request.urlopen(req, timeout=5) as resp:
            chat = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["role"], "agent")

    def test_run_with_empty_text_is_rejected(self):
        status, data = self._post("/api/run", {"provider": "codex", "text": "   "})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_run_with_no_text_but_an_attachment_is_accepted(self):
        # The composer allows sending an attachment with no typed text
        # (updateSendState() in webui/app.js) -- the server must accept
        # that, not just the client.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        status, data = self._post(
            "/api/run",
            {
                "provider": "codex",
                "text": "",
                "attachments": [{"name": "a.py", "path": "a.py", "content": "print(1)", "truncated": False}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"][0]["status"], "success")

    def test_run_with_no_text_and_no_attachments_is_rejected(self):
        status, data = self._post("/api/run", {"provider": "codex", "text": "", "attachments": []})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_concurrent_run_gets_409_not_a_hang_or_duplicate_message(self):
        webui._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/run", {"provider": "codex", "text": "hi"})
        finally:
            webui._RUN_LOCK.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_run_with_invalid_provider_is_rejected(self):
        status, data = self._post("/api/run", {"provider": "gemini", "text": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class EnsureChatGitignoreTests(unittest.TestCase):
    def test_creates_gitignore_ignoring_everything_under_handoff_webui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            webui.ensure_chat_gitignore(root)
            gitignore_path = root / ".handoff" / "webui" / ".gitignore"
            self.assertTrue(gitignore_path.exists())
            self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "*\n")

    def test_idempotent_does_not_clobber_a_customized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gitignore_path = root / ".handoff" / "webui" / ".gitignore"
            gitignore_path.parent.mkdir(parents=True)
            gitignore_path.write_text("# custom\n*\n", encoding="utf-8")
            webui.ensure_chat_gitignore(root)
            self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "# custom\n*\n")

    def test_append_chat_message_creates_it_even_in_a_workspace_with_no_dot_handoff_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse((root / ".handoff").exists())
            webui.append_chat_message(root, "user", "hi", [], utc_now_for_test())
            self.assertTrue((root / ".handoff" / "webui" / ".gitignore").exists())

    def test_protects_chat_history_from_git_even_with_a_stale_top_level_gitignore(self):
        # Simulates a workspace installed before this repo's own
        # .handoff/.gitignore learned about webui/chat/: the top-level file
        # exists but is "old" (doesn't mention it). The per-directory
        # .handoff/webui/.gitignore must protect the data regardless.
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run([git, "init", "-q"], cwd=root, check=True)
            subprocess.run([git, "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run([git, "config", "user.name", "Test"], cwd=root, check=True)
            (root / ".handoff").mkdir()
            (root / ".handoff" / ".gitignore").write_text("runs/\nstate.json\n", encoding="utf-8")
            (root / "README.md").write_text("# hi\n", encoding="utf-8")
            subprocess.run([git, "add", "-A"], cwd=root, check=True)
            subprocess.run([git, "commit", "-q", "-m", "init"], cwd=root, check=True)

            webui.append_chat_message(root, "user", "should stay untracked", [], utc_now_for_test())

            status = subprocess.run(
                [git, "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
            ).stdout
            self.assertEqual(status.strip(), "", f"chat files showed up in git status: {status!r}")


def utc_now_for_test() -> datetime:
    return datetime(2026, 8, 4, tzinfo=timezone.utc)


class MutableStateLiveServerTests(unittest.TestCase):
    """Each test gets its own workspace + server since these mutate
    AppState.workspace (open-folder) or write to disk (chat) -- sharing a
    server across tests here would make them order-dependent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        (self.root / "README.md").write_text("# root\n", encoding="utf-8")
        self.other = Path(self.tmp.name) / "other"
        self.other.mkdir()
        (self.other / "NOTES.md").write_text("# other\n", encoding="utf-8")

        self.state = webui.AppState(self.root)
        handler = webui.build_handler(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_post_chat_then_get_chat_round_trips(self):
        status, saved = self._post("/api/chat", {"role": "user", "text": "hi there", "attachments": []})
        self.assertEqual(status, 200)
        self.assertEqual(saved["text"], "hi there")

        status, data = self._get("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["text"], "hi there")

    def test_post_chat_persists_to_disk_under_dot_handoff(self):
        self._post("/api/chat", {"role": "user", "text": "persisted", "attachments": []})
        expected_dir = self.root / ".handoff" / "webui" / "chat"
        self.assertTrue(expected_dir.exists())
        self.assertEqual(len(list(expected_dir.glob("*.jsonl"))), 1)

    def test_post_chat_missing_body_rejected(self):
        url = f"http://127.0.0.1:{self.port}/api/chat"
        req = urllib.request.Request(url, method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        with ctx.exception:
            self.assertEqual(ctx.exception.code, 400)

    def test_post_chat_cannot_forge_an_agent_message(self):
        # "agent" messages are only ever supposed to come from POST /api/run
        # right after a real provider call (see docs/webui-chat-storage.md).
        # A client POSTing role="agent" straight to /api/chat must be
        # rejected, not silently accepted as a fake successful reply.
        status, data = self._post(
            "/api/chat", {"role": "agent", "text": "fake success", "attachments": []}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        status, data = self._get("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])

    def test_open_folder_switches_workspace(self):
        status, data = self._post("/api/open-folder", {"path": str(self.other)})
        self.assertEqual(status, 200)
        self.assertEqual(Path(data["workspace"]), self.other.resolve())

        status, tree = self._get("/api/tree?path=")
        self.assertEqual(status, 200)
        names = {e["name"] for e in tree["entries"]}
        # ".handoff" now exists because open-folder proactively creates
        # .handoff/webui/.gitignore -- see test_open_folder_proactively_creates_gitignore_before_any_message.
        self.assertEqual(names, {"NOTES.md", ".handoff"})

    def test_open_folder_proactively_creates_gitignore_before_any_message(self):
        self.assertFalse((self.other / ".handoff").exists())
        status, _ = self._post("/api/open-folder", {"path": str(self.other)})
        self.assertEqual(status, 200)
        self.assertTrue((self.other / ".handoff" / "webui" / ".gitignore").exists())

    def test_open_folder_rejects_nonexistent_path(self):
        status, data = self._post("/api/open-folder", {"path": str(self.other / "nope")})
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        # and the workspace must not have changed
        status, tree = self._get("/api/tree?path=")
        names = {e["name"] for e in tree["entries"]}
        self.assertEqual(names, {"README.md"})

    def test_open_folder_rejects_relative_path(self):
        status, data = self._post("/api/open-folder", {"path": "relative/path"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class NoWorkspaceLiveServerTests(unittest.TestCase):
    """AppState.workspace starts as None (SCR-05 / DEC-04~07) -- every read
    endpoint must degrade gracefully instead of crashing on a None path,
    and POST /api/chat's "user" role is the one path that's supposed to
    auto-create a workspace as a side effect. AUTO_WORKSPACE_BASE_DIR is
    patched to a tempdir so this never touches the real
    ~/Documents/Agent Handoff Bridge/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name) / "Agent Handoff Bridge"
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

        self.state = webui.AppState(None)
        handler = webui.build_handler(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_api_info_reports_null_workspace(self):
        status, data = self._get("/api/info")
        self.assertEqual(status, 200)
        self.assertIsNone(data["workspace"])
        self.assertIsNone(data["name"])

    def test_api_tree_returns_empty_entries_not_an_error(self):
        status, data = self._get("/api/tree?path=")
        self.assertEqual(status, 200)
        self.assertEqual(data["entries"], [])

    def test_api_file_is_rejected_with_a_clear_error(self):
        status, data = self._get("/api/file?path=whatever.txt")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_api_chat_get_returns_empty_history_not_an_error(self):
        status, data = self._get("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["months"], [])

    def test_api_run_is_rejected_when_no_workspace_exists_yet(self):
        status, data = self._post("/api/run", {"provider": "codex", "text": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_posting_a_user_chat_message_auto_creates_a_workspace(self):
        status, message = self._post("/api/chat", {"role": "user", "text": "fix the deploy script", "attachments": []})
        self.assertEqual(status, 200)
        self.assertEqual(message["text"], "fix the deploy script")

        status, info = self._get("/api/info")
        self.assertEqual(status, 200)
        self.assertIsNotNone(info["workspace"])
        self.assertIn("fix-the-deploy-script", info["workspace"].lower())

        # and it's a real, scaffolded workspace on disk under the (patched) base dir
        created = Path(info["workspace"])
        self.assertEqual(created.parent, self.base_dir)
        self.assertTrue((created / ".handoff" / "state.json").exists())

        # subsequent requests see the now-real workspace, not None anymore
        status, tree = self._get("/api/tree?path=")
        self.assertEqual(status, 200)

    def test_posting_a_system_chat_message_without_a_workspace_is_rejected(self):
        # "system" can't carry a folder-name summary and shouldn't silently
        # create a workspace as a side effect of something other than the
        # user actually sending a message.
        status, data = self._post("/api/chat", {"role": "system", "text": "note", "attachments": []})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_second_auto_create_from_a_fresh_state_does_not_collide_with_the_first(self):
        self._post("/api/chat", {"role": "user", "text": "same topic", "attachments": []})
        status1, info1 = self._get("/api/info")

        self.state.workspace = None  # simulate a second fresh "no workspace" session
        self._post("/api/chat", {"role": "user", "text": "same topic", "attachments": []})
        status2, info2 = self._get("/api/info")

        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)
        self.assertNotEqual(info1["workspace"], info2["workspace"])


if __name__ == "__main__":
    unittest.main()
