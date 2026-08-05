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

import io
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
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_creates_directory_under_base_dir_with_date_and_slug(self):
        workspace = webui.create_workspace_for_first_message("Fix the deploy script", [])
        self.assertEqual(workspace.parent, self.base_dir)
        self.assertTrue(workspace.is_dir())
        self.assertIn("Fix-the-deploy-script", workspace.name)

    def test_symlinked_base_dir_is_resolved_to_its_real_target(self):
        # Phase 3 regression: resolve_startup_workspace() and
        # validate_workspace_candidate() (the other two ways
        # AppState.workspace ever gets set) both .resolve(), but this one
        # originally didn't -- Path.home() doesn't resolve symlinks (e.g.
        # ~/Documents under iCloud Desktop & Documents sync), so the same
        # physical folder reached via auto-create vs. Open Folder/CLI
        # startup could stringify differently and duplicate in the
        # Phase 3 history registry instead of deduping to one entry.
        # Reproduced here with a real symlink standing in for that.
        real_target = Path(self.tmp.name) / "real-target"
        real_target.mkdir()
        symlinked_base = Path(self.tmp.name) / "symlinked-base"
        try:
            symlinked_base.symlink_to(real_target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported on this platform/runner")

        with mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", symlinked_base):
            workspace = webui.create_workspace_for_first_message("hello", [])

        self.assertEqual(workspace.parent, real_target.resolve())

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
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
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
        # disk with AppState.workspace pointing at only one of them. (Phase
        # 3's touch_registry() also writes registry.json alongside it, so
        # filter to directories rather than asserting the dir is empty.)
        created_dirs = [p for p in self.base_dir.iterdir() if p.is_dir()]
        self.assertEqual(len(created_dirs), 1)
        self.assertIsNotNone(self.state.workspace)


class RegistryTests(unittest.TestCase):
    """AUTO_WORKSPACE_BASE_DIR patched to a tempdir for every test here --
    must never touch the real ~/Documents/Agent Handoff Bridge/. Also
    regression coverage for registry_path() being a function, not a
    module-level constant bound once at import -- a constant wouldn't see
    this patch and every test here would try to write to the real path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_read_registry_missing_file_returns_empty_list(self):
        self.assertEqual(webui.read_registry(), [])

    def test_read_registry_unreadable_path_returns_empty_list_not_raise(self):
        # registry.json existing as a *directory* (permissions issues are
        # the more realistic real-world case, but a directory-where-a-file-
        # is-expected is the portable way to force the same OSError family
        # without relying on chmod, which root/CI can bypass).
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").mkdir()
        self.assertEqual(webui.read_registry(), [])

    def test_read_registry_skips_malformed_entries_instead_of_crashing(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text(
            json.dumps([{"path": "/w/good", "name": "good"}, "not a dict", {"name": "no path field"}, 42]),
            encoding="utf-8",
        )
        entries = webui.read_registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "/w/good")

    def test_touch_registry_does_not_raise_when_the_write_fails(self):
        # Regression: touch_registry() is called from POST /api/open-folder
        # and main() *after* real state already changed (AppState.workspace
        # assigned, or the server about to start) -- a write failure here
        # (permissions, full disk, base dir exists as a file) must be
        # best-effort, not propagate and desync the HTTP response / crash
        # startup from what the server actually just did.
        self.base_dir.parent.mkdir(parents=True, exist_ok=True)
        self.base_dir.write_text("a file, not a directory", encoding="utf-8")
        try:
            webui.touch_registry(Path("/some/workspace"), webui.utc_now())
        except OSError:
            self.fail("touch_registry() must not raise on a write failure")

    def test_read_registry_malformed_json_returns_empty_list(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text("not json", encoding="utf-8")
        self.assertEqual(webui.read_registry(), [])

    def test_read_registry_non_list_json_returns_empty_list(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text('{"not": "a list"}', encoding="utf-8")
        self.assertEqual(webui.read_registry(), [])

    def test_touch_registry_writes_it_to_the_patched_base_dir_not_the_real_one(self):
        webui.touch_registry(Path("/some/workspace"), datetime(2026, 8, 4, tzinfo=timezone.utc))
        self.assertTrue((self.base_dir / "registry.json").exists())

    def test_touch_registry_adds_an_entry(self):
        webui.touch_registry(Path("/w/project-a"), datetime(2026, 8, 4, tzinfo=timezone.utc))
        entries = webui.read_registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], str(Path("/w/project-a")))
        self.assertEqual(entries[0]["name"], "project-a")

    def test_touching_the_same_workspace_again_moves_it_to_front_not_duplicates(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        webui.touch_registry(Path("/w/a"), now)
        webui.touch_registry(Path("/w/b"), now)
        webui.touch_registry(Path("/w/a"), now)  # re-touch a
        entries = webui.read_registry()
        self.assertEqual([e["path"] for e in entries], [str(Path("/w/a")), str(Path("/w/b"))])

    def test_caps_at_max_entries_evicting_the_oldest(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        for i in range(webui.REGISTRY_MAX_ENTRIES + 3):
            webui.touch_registry(Path(f"/w/project-{i}"), now)
        entries = webui.read_registry()
        self.assertEqual(len(entries), webui.REGISTRY_MAX_ENTRIES)
        # most recent (highest i) survive, oldest (0, 1, 2) evicted
        paths = [e["path"] for e in entries]
        self.assertIn(str(Path(f"/w/project-{webui.REGISTRY_MAX_ENTRIES + 2}")), paths)
        self.assertNotIn(str(Path("/w/project-0")), paths)


class PairMessagesIntoTurnsTests(unittest.TestCase):
    def test_user_message_alone_produces_a_turn_with_no_provider_yet(self):
        turns = webui.pair_messages_into_turns([{"role": "user", "text": "hi", "ts": "t1"}])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "hi")
        self.assertIsNone(turns[0]["provider"])

    def test_user_plus_agent_reply_forms_one_turn(self):
        messages = [
            {"role": "user", "text": "fix the bug", "ts": "t1"},
            {"role": "agent", "text": "done", "provider": "codex", "status": "success", "ts": "t2"},
        ]
        turns = webui.pair_messages_into_turns(messages)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "fix the bug")
        self.assertEqual(turns[0]["provider"], "codex")
        self.assertEqual(turns[0]["status"], "success")

    def test_multiple_agent_replies_in_one_turn_use_the_last_one(self):
        # DEC-12: auto-fallback (codex fails -> claude succeeds) -- the
        # drawer should show how the turn actually ended up, not the first
        # attempt.
        messages = [
            {"role": "user", "text": "fix the bug", "ts": "t1"},
            {"role": "agent", "text": "rate limited", "provider": "codex", "status": "handoff", "ts": "t2"},
            {"role": "agent", "text": "done", "provider": "claude", "status": "success", "ts": "t3"},
        ]
        turns = webui.pair_messages_into_turns(messages)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["provider"], "claude")
        self.assertEqual(turns[0]["status"], "success")

    def test_system_messages_do_not_start_a_turn(self):
        messages = [
            {"role": "system", "text": "workspace switched", "ts": "t1"},
            {"role": "user", "text": "hi", "ts": "t2"},
        ]
        turns = webui.pair_messages_into_turns(messages)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "hi")

    def test_two_separate_user_messages_produce_two_turns(self):
        messages = [
            {"role": "user", "text": "first", "ts": "t1"},
            {"role": "agent", "text": "ok", "provider": "codex", "status": "success", "ts": "t2"},
            {"role": "user", "text": "second", "ts": "t3"},
            {"role": "agent", "text": "ok2", "provider": "claude", "status": "success", "ts": "t4"},
        ]
        turns = webui.pair_messages_into_turns(messages)
        self.assertEqual([t["text"] for t in turns], ["first", "second"])


class CollectRecentTurnsTests(unittest.TestCase):
    def test_caps_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, tzinfo=timezone.utc)
            for i in range(8):
                webui.append_chat_message(root, "user", f"turn {i}", [], now)
                webui.append_chat_message(root, "agent", "ok", [], now, provider="codex", status="success")
            turns = webui.collect_recent_turns(root, limit=5)
            self.assertEqual(len(turns), 5)
            # newest first
            self.assertEqual(turns[0]["text"], "turn 7")

    def test_scans_backward_across_months_when_current_month_is_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = datetime(2026, 6, 1, tzinfo=timezone.utc)
            newer = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "old turn", [], older)
            webui.append_chat_message(root, "user", "new turn", [], newer)
            turns = webui.collect_recent_turns(root, limit=5)
            self.assertEqual({t["text"] for t in turns}, {"old turn", "new turn"})

    def test_empty_workspace_returns_no_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui.collect_recent_turns(Path(tmp)), [])

    def test_turn_split_across_a_month_boundary_still_pairs_correctly(self):
        # Regression: pairing each month's file in isolation would drop
        # the agent reply entirely (no preceding user message in its own
        # month's list) and leave the user's turn with no provider/status
        # -- a message sent right before a UTC month boundary, with the
        # reply landing just after it, must still pair as one turn.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            end_of_july = datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
            start_of_august = datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "cross-boundary turn", [], end_of_july)
            webui.append_chat_message(
                root, "agent", "done", [], start_of_august, provider="codex", status="success"
            )
            turns = webui.collect_recent_turns(root, limit=5)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["text"], "cross-boundary turn")
            self.assertEqual(turns[0]["provider"], "codex")
            self.assertEqual(turns[0]["status"], "success")


class BuildHistoryDrawerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.projects_dir = Path(self.tmp.name) / "projects"
        self.projects_dir.mkdir()

    def _make_project(self, name, text="hello"):
        root = self.projects_dir / name
        root.mkdir()
        webui.append_chat_message(root, "user", text, [], webui.utc_now())
        return root

    def test_current_workspace_is_pinned_first_and_marked(self):
        current = self._make_project("current-proj")
        other = self._make_project("other-proj")
        webui.touch_registry(other, webui.utc_now())
        webui.touch_registry(current, webui.utc_now())
        groups = webui.build_history_drawer(current)
        self.assertEqual(groups[0]["path"], str(current))
        self.assertTrue(groups[0]["current"])
        self.assertFalse(groups[1]["current"])

    def test_registry_entry_for_a_deleted_folder_is_silently_skipped(self):
        gone = self.projects_dir / "deleted-proj"
        gone.mkdir()
        webui.touch_registry(gone, webui.utc_now())
        shutil.rmtree(gone)
        groups = webui.build_history_drawer(None)
        self.assertEqual(groups, [])

    def test_no_current_workspace_still_returns_registry_groups(self):
        project = self._make_project("solo-proj")
        webui.touch_registry(project, webui.utc_now())
        groups = webui.build_history_drawer(None)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["current"])

    def test_each_group_carries_its_own_turns(self):
        project = self._make_project("with-turns", text="do the thing")
        webui.touch_registry(project, webui.utc_now())
        groups = webui.build_history_drawer(None)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["turns"][0]["text"], "do the thing")


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

    def test_model_starting_with_a_dash_is_passed_as_one_argv_token(self):
        # ["--model", value] would let argparse misparse a value that
        # looks like a flag (e.g. "--foo") as the next option instead of
        # --model's value ("argument --model: expected one argument") --
        # "--model=value" is unambiguous regardless of what value is.
        # Not reachable through the shipped UI today (it never sends
        # `model`), but the same class of gap was already closed for the
        # prompt (--prompt-file) and init's task (--), so closing it here
        # too for whenever `model` gets wired up.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            with mock.patch("handoff_webui.subprocess.run", wraps=subprocess.run) as spy:
                webui.run_provider_via_bridge(root, "codex", "hello", "--weird-model-name", "continue")
            command = spy.call_args.args[0]
            self.assertIn("--model=--weird-model-name", command)
            # never as a separate token (that's the old, broken ["--model", value] form)
            self.assertNotIn("--weird-model-name", command)

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
        #
        # The "claude hangs" premise only makes sense if claude's CLI was
        # actually launchable in the first place -- if it weren't installed,
        # the real recursive call would fail near-instantly (FileNotFoundError
        # -> exit 127), not hang until the outer timeout. handoff_webui's
        # timed-out-provider guess now uses next_available_provider() (review
        # fix: the naive next-in-PROVIDERS-order guess could name an
        # uninstalled provider), so shutil.which() is pinned here to make
        # that premise concrete and this test deterministic regardless of
        # what's actually installed on whatever machine runs the suite.
        with mock.patch("handoff_bridge.shutil.which", side_effect=lambda name: name in ("codex", "claude") and f"/usr/bin/{name}"), tempfile.TemporaryDirectory() as tmp:
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

    def test_open_folder_is_rejected_while_a_run_is_in_flight(self):
        # A run writes into whatever workspace was active when it started
        # and persists into that workspace's chat log on completion --
        # switching state.workspace out from under it mid-run would
        # misdirect where that write (and the client's eventual render of
        # it) ends up.
        webui._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/open-folder", {"path": str(self.root)})
        finally:
            webui._RUN_LOCK.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_new_user_message_is_rejected_while_a_run_is_in_flight(self):
        # Phase 3 regression guard: pair_messages_into_turns() attaches
        # each agent reply to whichever user message it saw most recently
        # in the chat log's append order -- a second client posting a new
        # user message while another run is still in flight could get
        # that in-flight run's eventual reply misattributed to the newer
        # message in the history drawer once it lands. Rejecting the new
        # message outright means two user turns can never be
        # simultaneously unanswered in the same workspace.
        webui._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "user", "text": "a second message", "attachments": []})
        finally:
            webui._RUN_LOCK.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

        # and it really wasn't persisted
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat")
        with urllib.request.urlopen(req, timeout=5) as resp:
            chat = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(chat["messages"], [])

    def test_system_message_is_still_accepted_while_a_run_is_in_flight(self):
        # The 409 guard is specifically for "user" (new turns) -- a system
        # message doesn't start a turn and shouldn't be blocked by it.
        webui._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "system", "text": "note", "attachments": []})
        finally:
            webui._RUN_LOCK.release()
        self.assertEqual(status, 200)

    def test_run_with_invalid_provider_is_rejected(self):
        # "gemini" used to be the invalid example here -- Phase 5 made it
        # a real provider (handoff_bridge.PROVIDERS grew to include it),
        # so a genuinely unknown name is needed instead.
        status, data = self._post("/api/run", {"provider": "not-a-real-provider", "text": "hi"})
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
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
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

    def test_api_history_is_empty_before_any_workspace_exists(self):
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(data["groups"], [])

    def test_auto_created_workspace_shows_up_in_the_history_drawer(self):
        # DEC-10: the registry (and so the drawer) must reflect
        # auto-created workspaces too, not just explicit Open Folder.
        self._post("/api/chat", {"role": "user", "text": "fix the deploy script", "attachments": []})
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["groups"]), 1)
        self.assertTrue(data["groups"][0]["current"])
        self.assertEqual(data["groups"][0]["turns"][0]["text"], "fix the deploy script")


class HistoryDrawerLiveServerTests(unittest.TestCase):
    """AUTO_WORKSPACE_BASE_DIR patched to a tempdir -- covers the
    registry-touch integration points a plain AppState(workspace) live
    server exercises (Open Folder), which NoWorkspaceLiveServerTests
    above (AppState(None)) can't reach directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # .resolve(): create_workspace_for_first_message() resolves
        # AUTO_WORKSPACE_BASE_DIR internally (path-normalization fix, see
        # its own comment) -- matching that here avoids a spurious macOS
        # /var vs /private/var mismatch in assertions below.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

        # .resolve() here matches every real production path into
        # AppState: resolve_startup_workspace()/validate_workspace_candidate()/
        # create_workspace_for_first_message() all resolve before AppState
        # ever sees the value -- AppState() itself does no resolution, so
        # an unresolved path here would be an unrealistic setup (and, on
        # macOS, a spurious /var vs /private/var mismatch against the
        # registry's resolved entries).
        self.root = (Path(self.tmp.name) / "root").resolve()
        self.root.mkdir()
        self.other = (Path(self.tmp.name) / "other").resolve()
        self.other.mkdir()

        # Real main() calls touch_registry() for its startup workspace
        # (DEC-10); these tests build AppState directly, bypassing main(),
        # so simulate that one call here -- otherwise self.root would
        # never appear in the registry despite being a real, currently-open
        # workspace, which doesn't reflect actual startup behavior.
        webui.touch_registry(self.root, webui.utc_now())

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

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_open_folder_registers_both_workspaces_in_the_drawer(self):
        self._post("/api/open-folder", {"path": str(self.other)})
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        paths = {g["path"] for g in data["groups"]}
        self.assertEqual(paths, {str(self.root), str(self.other)})
        current = next(g for g in data["groups"] if g["current"])
        self.assertEqual(current["path"], str(self.other))

    def test_open_folder_still_succeeds_even_if_the_registry_write_fails(self):
        # The registry is a Phase 3 convenience index, not durable state --
        # a write failure (base dir exists as a file, permissions, full
        # disk) must never turn a real, successful workspace switch into a
        # client-visible failure. Simulated here the same way
        # RegistryTests.test_touch_registry_does_not_raise_when_the_write_fails
        # does: the base dir itself exists as a file, not a directory.
        # setUp()'s own touch_registry() call already created it as a real
        # directory, so clear that first.
        shutil.rmtree(self.base_dir, ignore_errors=True)
        self.base_dir.write_text("a file, not a directory", encoding="utf-8")

        status, data = self._post("/api/open-folder", {"path": str(self.other)})

        self.assertEqual(status, 200)
        self.assertEqual(Path(data["workspace"]), self.other)

    def test_startup_workspace_is_registered_without_any_api_call(self):
        # DEC-10: main() touches the registry for the workspace it starts
        # with too, not just explicit UI actions -- setUp() simulates that
        # one main()-only call, so this just confirms it actually shows up
        # without any further API interaction.
        status, data = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(data["groups"][0]["path"], str(self.root))
        self.assertTrue(data["groups"][0]["current"])


class CredentialsTests(unittest.TestCase):
    """AUTO_WORKSPACE_BASE_DIR patched to a tempdir -- same reasoning as
    RegistryTests: credentials_path() is a function, not a module-level
    constant, precisely so this patch is actually honored (never touch the
    real ~/Documents/Agent Handoff Bridge/credentials.json)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_read_credentials_missing_file_returns_empty_dict(self):
        self.assertEqual(webui.read_credentials(), {})

    def test_read_credentials_malformed_json_returns_empty_dict(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text("not json", encoding="utf-8")
        self.assertEqual(webui.read_credentials(), {})

    def test_read_credentials_non_dict_json_returns_empty_dict(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(webui.read_credentials(), {})

    def test_read_credentials_filters_unknown_provider(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps({"claude": {"key": "sk-1"}, "gemini": {"key": "sk-2"}}), encoding="utf-8"
        )
        self.assertEqual(list(webui.read_credentials()), ["claude"])

    def test_read_credentials_filters_entry_with_no_key(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps({"claude": {"model": "claude-sonnet-5"}}), encoding="utf-8"
        )
        self.assertEqual(webui.read_credentials(), {})

    def test_save_then_read_round_trips_key_and_model(self):
        webui.save_credential("claude", "sk-ant-test", "claude-sonnet-5")
        creds = webui.read_credentials()
        self.assertEqual(creds["claude"], {"key": "sk-ant-test", "model": "claude-sonnet-5"})

    def test_save_with_no_model_stores_none(self):
        webui.save_credential("codex", "sk-test", None)
        self.assertIsNone(webui.read_credentials()["codex"]["model"])

    def test_save_with_empty_key_removes_the_entry(self):
        webui.save_credential("claude", "sk-ant-test", None)
        webui.save_credential("claude", "", None)
        self.assertNotIn("claude", webui.read_credentials())

    def test_saved_file_has_owner_only_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX file permissions not applicable")
        webui.save_credential("claude", "sk-ant-test", None)
        mode = webui.credentials_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_saving_one_provider_does_not_clobber_another(self):
        webui.save_credential("claude", "sk-claude", None)
        webui.save_credential("codex", "sk-codex", None)
        creds = webui.read_credentials()
        self.assertEqual(creds["claude"]["key"], "sk-claude")
        self.assertEqual(creds["codex"]["key"], "sk-codex")


class BuildApiMessageHistoryTests(unittest.TestCase):
    def test_empty_log_produces_just_the_current_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            messages = webui.build_api_message_history(root, "hello", now)
            self.assertEqual(messages, [{"role": "user", "content": "hello"}])

    def test_prior_turns_are_replayed_as_alternating_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            webui.append_chat_message(root, "user", "first question", [], now)
            webui.append_chat_message(root, "agent", "first answer", [], now, provider="claude", status="success", reason="none")
            webui.append_chat_message(root, "user", "second question (bare, no attachments)", [], now)
            messages = webui.build_api_message_history(root, "second question WITH attachment text", now)
            # The bare current-turn "user" log entry is dropped in favor of
            # the caller-supplied `prompt` (which carries attachment
            # content the bare log entry never does) -- so it must appear
            # exactly once, as the final message, not duplicated.
            self.assertEqual(
                messages,
                [
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "second question WITH attachment text"},
                ],
            )

    def test_system_role_messages_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            webui.append_chat_message(root, "system", "auto-created workspace", [], now)
            webui.append_chat_message(root, "user", "hi", [], now)
            messages = webui.build_api_message_history(root, "hi again", now)
            # The system message is filtered out entirely, and the trailing
            # bare "user" log entry is dropped in favor of `prompt` (same
            # rule as the prior-turns test) -- leaving just one message.
            self.assertEqual(messages, [{"role": "user", "content": "hi again"}])

    def test_history_is_capped_to_the_max_message_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            for i in range(webui.API_KEY_MODE_MAX_HISTORY_MESSAGES + 10):
                webui.append_chat_message(root, "user" if i % 2 == 0 else "agent", f"turn {i}", [], now)
            messages = webui.build_api_message_history(root, "final prompt", now)
            self.assertEqual(len(messages), webui.API_KEY_MODE_MAX_HISTORY_MESSAGES + 1)  # +1 for the final prompt
            self.assertEqual(messages[-1], {"role": "user", "content": "final prompt"})

    def test_two_consecutive_agent_messages_from_auto_fallback_are_merged_not_left_alternating_broken(self):
        # A single CLI turn can leave two consecutive "agent" chat-log
        # entries when --auto-fallback chains providers (codex fails ->
        # claude succeeds) -- POST /api/run appends one per resulting
        # record. Anthropic's Messages API requires strict user/assistant
        # alternation, so replaying these as two separate "assistant"
        # messages would make every subsequent API-key-mode call in this
        # workspace fail with a 400. They must be merged into one.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            webui.append_chat_message(root, "user", "do the thing", [], now)
            webui.append_chat_message(root, "agent", "codex gave up", [], now, provider="codex", status="handoff", reason="rate_limit: x")
            webui.append_chat_message(root, "agent", "claude finished it", [], now, provider="claude", status="success", reason="none")
            webui.append_chat_message(root, "user", "thanks, now do the next thing", [], now)
            messages = webui.build_api_message_history(root, "thanks, now do the next thing (with attachment)", now)
            self.assertEqual(
                messages,
                [
                    {"role": "user", "content": "do the thing"},
                    {"role": "assistant", "content": "codex gave up\n\nclaude finished it"},
                    {"role": "user", "content": "thanks, now do the next thing (with attachment)"},
                ],
            )
            roles = [m["role"] for m in messages]
            self.assertEqual(roles, ["user", "assistant", "user"])  # never two of the same role in a row

    def test_consecutive_trailing_user_messages_merge_with_the_final_prompt(self):
        # Edge case: two "user" chat-log entries back to back with no
        # intervening "agent" reply (e.g. a second POST /api/chat landed
        # without a POST /api/run in between). Only the single true
        # current-turn entry is dropped in favor of `prompt` -- the
        # remaining bare "user" entry must still end up merged with the
        # final prompt, not left as its own consecutive "user" message.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui.utc_now()
            webui.append_chat_message(root, "user", "first, unanswered", [], now)
            webui.append_chat_message(root, "user", "second, also unanswered", [], now)
            messages = webui.build_api_message_history(root, "second, also unanswered (full prompt)", now)
            self.assertEqual(
                messages, [{"role": "user", "content": "first, unanswered\n\nsecond, also unanswered (full prompt)"}]
            )

    def test_history_spans_a_month_boundary(self):
        # Phase 3 already had to fix collect_recent_turns() for exactly
        # this class of bug (silently dropping/misattributing context
        # split across a UTC month boundary) -- build_api_message_history()
        # must not regress the same way for the *first* message(s) of a
        # new month, when the current month's own log has little or no
        # prior context of its own.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
            august = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
            webui.append_chat_message(root, "user", "july question", [], july)
            webui.append_chat_message(root, "agent", "july answer", [], july, provider="claude", status="success", reason="none")
            webui.append_chat_message(root, "user", "august question (bare)", [], august)
            messages = webui.build_api_message_history(root, "august question (full prompt)", august)
            self.assertEqual(
                messages,
                [
                    {"role": "user", "content": "july question"},
                    {"role": "assistant", "content": "july answer"},
                    {"role": "user", "content": "august question (full prompt)"},
                ],
            )


def _fake_response(status: int, body_dict: dict) -> mock.MagicMock:
    """A fake urllib.request.urlopen() return value: usable as a context
    manager (`with urlopen(...) as response:`), with .status/.read()."""
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = json.dumps(body_dict).encode("utf-8")
    return response


def _fake_http_error(code: int, body_dict: dict, retry_after: str | None = None) -> urllib.error.HTTPError:
    """A real urllib.error.HTTPError instance (not a MagicMock) -- exercises
    _http_post_json()'s actual exc.read()/exc.headers.get() calls instead
    of a mock standing in for them."""
    fp = io.BytesIO(json.dumps(body_dict).encode("utf-8"))
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://example.invalid", code, "err", headers, fp)


class HttpPostJsonTests(unittest.TestCase):
    def test_a_header_validation_valueerror_is_converted_to_an_error_tuple_not_raised(self):
        # http.client raises a bare ValueError (not HTTPError/URLError) for
        # a header value containing forbidden characters -- e.g. a saved
        # API key with an embedded CR/LF reaching the x-api-key/
        # Authorization header unescaped. Before this fix, that ValueError
        # propagated straight out of _http_post_json(), uncaught anywhere
        # up the stack (POST /api/run only catches RunAlreadyInProgressError/
        # WorkspaceError), crashing that request's thread with no chat-log
        # record ever written -- unlike every other failure path this
        # feature classifies cleanly.
        raw_exception_text = "Invalid header value b'sk-super-secret\\r\\nX-Evil: 1'"
        with mock.patch("handoff_webui.urllib.request.urlopen", side_effect=ValueError(raw_exception_text)):
            status, data = webui._http_post_json(
                "https://example.invalid/v1/messages", {"x-api-key": "sk-super-secret\r\nX-Evil: 1"}, {}, 5
            )
        self.assertNotEqual(status, 200)
        message = json.dumps(data)
        # httplib's own ValueError text embeds the offending header VALUE
        # (the key itself) verbatim -- it must never be forwarded as-is,
        # unlike every other exception type this function handles.
        self.assertNotIn("sk-super-secret", message)
        self.assertNotIn(raw_exception_text, message)

    def test_success_path_still_works_unchanged(self):
        with mock.patch("handoff_webui.urllib.request.urlopen", return_value=_fake_response(200, {"ok": True})):
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual((status, data), (200, {"ok": True}))

    def test_a_malformed_200_body_is_a_clean_error_not_a_raise_and_not_mislabeled_as_a_header_problem(self):
        # Regression: an earlier version of the header-rejection ValueError
        # catch (added in a prior round) was broad enough to also swallow
        # json.JSONDecodeError raised from parsing a malformed *success*
        # body -- JSONDecodeError is itself a ValueError subclass -- which
        # would have mislabeled this as "headers were rejected" instead of
        # what actually happened.
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.status = 200
        fake.read.return_value = b"not json"
        with mock.patch("handoff_webui.urllib.request.urlopen", return_value=fake):
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertNotEqual(status, 200)
        message = json.dumps(data)
        self.assertNotIn("header", message.lower())

    def test_a_429_is_retried_and_a_later_success_is_returned(self):
        rate_limited = _fake_http_error(429, {"error": {"type": "rate_limit_error", "message": "slow down"}})
        succeeded = _fake_response(200, {"ok": True})
        with mock.patch("handoff_webui.urllib.request.urlopen", side_effect=[rate_limited, succeeded]), mock.patch(
            "handoff_webui._sleep"
        ) as sleep_spy:
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual((status, data), (200, {"ok": True}))
        sleep_spy.assert_called_once()

    def test_retries_are_bounded_then_the_final_error_is_returned(self):
        always_500 = [_fake_http_error(500, {"error": {"type": "api_error", "message": "down"}}) for _ in range(10)]
        with mock.patch("handoff_webui.urllib.request.urlopen", side_effect=always_500), mock.patch(
            "handoff_webui._sleep"
        ) as sleep_spy:
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual(status, 500)
        self.assertEqual(sleep_spy.call_count, webui.API_KEY_MODE_MAX_RETRIES)

    def test_a_non_retryable_error_is_returned_immediately_without_sleeping(self):
        auth_error = _fake_http_error(401, {"error": {"type": "authentication_error", "message": "bad key"}})
        with mock.patch("handoff_webui.urllib.request.urlopen", side_effect=[auth_error, auth_error]), mock.patch(
            "handoff_webui._sleep"
        ) as sleep_spy:
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual(status, 401)
        sleep_spy.assert_not_called()

    def test_retry_delay_honors_a_numeric_retry_after_header(self):
        rate_limited = _fake_http_error(
            429, {"error": {"type": "rate_limit_error", "message": "slow down"}}, retry_after="7"
        )
        succeeded = _fake_response(200, {"ok": True})
        with mock.patch("handoff_webui.urllib.request.urlopen", side_effect=[rate_limited, succeeded]), mock.patch(
            "handoff_webui._sleep"
        ) as sleep_spy:
            webui._http_post_json("https://example.invalid", {}, {}, 5)
        sleep_spy.assert_called_once_with(7.0)

    def test_a_network_error_is_retried_the_same_as_a_5xx(self):
        with mock.patch(
            "handoff_webui.urllib.request.urlopen", side_effect=[urllib.error.URLError("connection reset"), _fake_response(200, {"ok": True})]
        ), mock.patch("handoff_webui._sleep") as sleep_spy:
            status, data = webui._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual((status, data), (200, {"ok": True}))
        sleep_spy.assert_called_once()


class CallProviderApiTests(unittest.TestCase):
    def test_anthropic_success_extracts_text(self):
        with mock.patch(
            "handoff_webui._http_post_json",
            return_value=(200, {"content": [{"type": "text", "text": "hi there"}]}),
        ):
            result = webui.call_anthropic_messages_api("sk-secret", "claude-sonnet-5", [{"role": "user", "content": "hi"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hi there")

    def test_anthropic_error_response_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "handoff_webui._http_post_json",
            return_value=(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
        ):
            result = webui.call_anthropic_messages_api("sk-super-secret-value", "claude-sonnet-5", [])
        self.assertFalse(result["ok"])
        self.assertIn("authentication_error", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_anthropic_network_error_does_not_raise(self):
        with mock.patch("handoff_webui._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui.call_anthropic_messages_api("sk-secret", "claude-sonnet-5", [])
        self.assertFalse(result["ok"])
        self.assertIn("network error", result["message"])
        self.assertNotIn("sk-secret", result["message"])

    def test_anthropic_handles_the_invalid_header_error_tuple_without_raising(self):
        # _http_post_json() converts a header-validation ValueError (see
        # HttpPostJsonTests below) into a plain (0, {"error": ...}) tuple
        # rather than letting it propagate -- this confirms the caller
        # treats that tuple as an ordinary non-200 error, not a crash.
        with mock.patch(
            "handoff_webui._http_post_json",
            return_value=(0, {"error": {"type": "invalid_request", "message": "request headers were rejected"}}),
        ):
            result = webui.call_anthropic_messages_api("sk-secret-with-crlf", "claude-sonnet-5", [])
        self.assertFalse(result["ok"])
        self.assertIn("invalid_request", result["message"])
        self.assertNotIn("sk-secret-with-crlf", result["message"])

    def test_openai_success_extracts_text_from_output_items(self):
        with mock.patch(
            "handoff_webui._http_post_json",
            return_value=(
                200,
                {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi from openai"}]}]},
            ),
        ):
            result = webui.call_openai_responses_api("sk-secret", "gpt-5.1-codex", [{"role": "user", "content": "hi"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hi from openai")

    def test_openai_error_response_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "handoff_webui._http_post_json",
            return_value=(429, {"error": {"type": "rate_limit_error", "message": "too many requests"}}),
        ):
            result = webui.call_openai_responses_api("sk-super-secret-value", "gpt-5.1-codex", [])
        self.assertFalse(result["ok"])
        self.assertIn("rate_limit_error", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])


class RunProviderViaApiKeyTests(unittest.TestCase):
    def test_success_produces_a_record_with_no_session_or_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("handoff_webui.call_anthropic_messages_api", return_value={"ok": True, "text": "hello back"}):
                records = webui.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue"
                )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["final_text"], "hello back")
        self.assertFalse(record["handoff_needed"])
        self.assertEqual(record["reason"], "none")
        self.assertIsNone(record["session_id"])
        self.assertIsNone(record["run_dir"])
        self.assertEqual(record["model"], "claude-sonnet-5")

    def test_failure_produces_a_tool_failure_reason_classified_as_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "handoff_webui.call_anthropic_messages_api", return_value={"ok": False, "message": "401 authentication_error"}
            ) as spy:
                records = webui.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue"
                )
        spy.assert_called_once()  # confirms this exercised the real API-failure path, not the no-model-configured one
        record = records[0]
        self.assertTrue(record["reason"].startswith("tool_failure"))
        self.assertEqual(
            webui.classify_run_status(record["handoff_needed"], record["reason"]), "fail"
        )

    def test_claude_with_no_model_configured_and_no_default_errors_without_calling_the_api(self):
        # Neither provider has a built-in default model (see
        # API_KEY_MODE_DEFAULT_MODELS' own comment on why Claude's was
        # deliberately removed) -- this mirrors the codex test below for
        # symmetry.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("handoff_webui.call_anthropic_messages_api") as spy:
                records = webui.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": None}, "continue")
        spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("model", records[0]["final_text"])

    def test_codex_with_no_model_configured_and_no_default_errors_without_calling_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("handoff_webui.call_openai_responses_api") as spy:
                records = webui.run_provider_via_api_key(root, "codex", "hello", {"key": "sk-x", "model": None}, "continue")
        spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("model", records[0]["final_text"])

    def test_codex_with_a_saved_model_calls_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("handoff_webui.call_openai_responses_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui.run_provider_via_api_key(
                    root, "codex", "hello", {"key": "sk-x", "model": "gpt-5.1-codex"}, "continue"
                )
        spy.assert_called_once()
        self.assertEqual(records[0]["model"], "gpt-5.1-codex")

    def test_model_override_takes_priority_over_the_saved_credential_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("handoff_webui.call_anthropic_messages_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-old"}, "continue", model_override="claude-new"
                )
        self.assertEqual(records[0]["model"], "claude-new")
        self.assertEqual(spy.call_args.args[1], "claude-new")


class ProviderDispatchTests(FakeProviderPathMixin, unittest.TestCase):
    """Covers _run_provider_via_bridge_locked()'s Phase 4 branch -- the
    part of the dispatch logic RunProviderViaBridgeTests (which always has
    a fake CLI on PATH) can't exercise: a provider whose CLI is genuinely
    absent."""

    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("handoff_webui.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    def test_cli_available_provider_uses_subprocess_even_if_a_key_is_also_saved(self):
        # A saved key must never override an available CLI -- DEC-16.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        webui.save_credential("codex", "sk-should-be-unused", None)
        with mock.patch("handoff_webui.call_openai_responses_api") as spy:
            records = webui.run_provider_via_bridge(self.workspace, "codex", "hello", None, "continue")
        spy.assert_not_called()
        self.assertEqual(records[0]["session_id"], "fake-codex-session")

    def test_cli_missing_provider_with_a_saved_key_uses_the_api_path(self):
        webui.save_credential("claude", "sk-x", "claude-sonnet-5")
        with mock.patch("handoff_webui.shutil.which", return_value=None), mock.patch(
            "handoff_webui.call_anthropic_messages_api", return_value={"ok": True, "text": "api reply"}
        ) as spy:
            records = webui.run_provider_via_bridge(self.workspace, "claude", "hello", None, "continue")
        spy.assert_called_once()
        self.assertEqual(records[0]["final_text"], "api reply")
        self.assertIsNone(records[0]["run_dir"])

    def test_cli_missing_provider_with_no_saved_key_behaves_exactly_as_before_this_phase(self):
        # No fake binary for "claude" and no credential saved -- this falls
        # through to the pre-existing subprocess path unchanged, which
        # spawns a real handoff_bridge.py child process. That child inherits
        # this process's PATH, so mock.patch("handoff_webui.shutil.which")
        # alone would NOT be enough here (it only affects this parent
        # process's own dispatch check) -- if a real `claude` CLI happens to
        # be installed on this machine's PATH, the child could actually
        # invoke it. PATH is replaced outright (not just prepended, like
        # setUpFakeProviders() does) so the child genuinely cannot find one
        # either, and handoff_bridge.py's own FileNotFoundError -> exit_code
        # 127 handling is what's actually being exercised here.
        with mock.patch.dict(os.environ, {"PATH": str(self.fake_bin)}):
            records = webui.run_provider_via_bridge(self.workspace, "claude", "hello", None, "continue")
        self.assertEqual(records[0]["exit_code"], 127)

    def test_auto_with_no_cli_at_all_falls_back_to_a_provider_with_a_saved_key(self):
        webui.save_credential("claude", "sk-x", "claude-sonnet-5")
        with mock.patch("handoff_webui.shutil.which", return_value=None), mock.patch(
            "handoff_webui.call_anthropic_messages_api", return_value={"ok": True, "text": "api reply"}
        ):
            records = webui.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        self.assertEqual(records[0]["provider"], "claude")
        self.assertEqual(records[0]["final_text"], "api reply")

    def test_auto_with_no_cli_and_no_saved_key_returns_a_clear_error_not_a_crash(self):
        with mock.patch("handoff_webui.shutil.which", return_value=None):
            records = webui.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        # Same invariant RunProviderViaBridgeTests::
        # test_synthetic_record_resolves_auto_never_persists_auto_literal
        # already enforces for the CLI-side synthetic-record path: the
        # /api/run handler persists record["provider"] verbatim into the
        # chat log (append_chat_message(provider=record["provider"])), so
        # the literal string "auto" must never reach it here either.
        self.assertNotEqual(records[0]["provider"], "auto")
        self.assertIn(records[0]["provider"], webui.PROVIDERS)

    def test_auto_with_at_least_one_cli_ignores_saved_keys_and_uses_the_existing_subprocess_path(self):
        # DEC-16: `auto` only considers API-key mode when NO CLI exists at
        # all -- one CLI being available must keep the existing
        # choose_auto_provider()/--auto-fallback behavior completely
        # unchanged, even if a key happens to be saved for the other one.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        webui.save_credential("claude", "sk-should-be-unused", "claude-sonnet-5")
        with mock.patch("handoff_webui.call_anthropic_messages_api") as spy:
            records = webui.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        spy.assert_not_called()
        self.assertEqual(records[0]["provider"], "codex")


class ProviderApiLiveServerTests(unittest.TestCase):
    """GET /api/providers and POST /api/provider-key over a real HTTP
    server -- same pattern as HistoryDrawerLiveServerTests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
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

    def test_providers_list_reflects_cli_detection_and_key_state(self):
        status, data = self._get("/api/providers")
        self.assertEqual(status, 200)
        by_name = {p["provider"]: p for p in data["providers"]}
        # Phase 5: PROVIDERS grew to include gemini for CLI dispatch, but
        # API_KEY_MODE_PROVIDERS (DEC-15's scope) deliberately did not --
        # gemini shows up here (real CLI-detection badge) without gaining
        # a key field.
        self.assertEqual(set(by_name), {"codex", "claude", "gemini"})
        for info in by_name.values():
            self.assertIn("cli_detected", info)
            self.assertFalse(info["api_key_configured"])
        self.assertTrue(by_name["codex"]["api_key_mode_supported"])
        self.assertTrue(by_name["claude"]["api_key_mode_supported"])
        self.assertFalse(by_name["gemini"]["api_key_mode_supported"])

    def test_saving_a_key_never_echoes_it_back(self):
        status, data = self._post("/api/provider-key", {"provider": "claude", "key": "sk-secret-value", "model": "claude-sonnet-5"})
        self.assertEqual(status, 200)
        self.assertNotIn("key", data)
        self.assertEqual(data, {"provider": "claude", "api_key_configured": True, "model": "claude-sonnet-5"})

    def test_saved_key_is_reflected_in_the_providers_list(self):
        self._post("/api/provider-key", {"provider": "claude", "key": "sk-secret-value", "model": "claude-sonnet-5"})
        _, data = self._get("/api/providers")
        claude = next(p for p in data["providers"] if p["provider"] == "claude")
        self.assertTrue(claude["api_key_configured"])
        self.assertEqual(claude["model"], "claude-sonnet-5")

    def test_empty_key_removes_a_previously_saved_one(self):
        self._post("/api/provider-key", {"provider": "claude", "key": "sk-secret-value"})
        status, data = self._post("/api/provider-key", {"provider": "claude", "key": ""})
        self.assertEqual(status, 200)
        self.assertFalse(data["api_key_configured"])
        _, providers = self._get("/api/providers")
        claude = next(p for p in providers["providers"] if p["provider"] == "claude")
        self.assertFalse(claude["api_key_configured"])

    def test_invalid_provider_is_rejected_with_400(self):
        status, data = self._post("/api/provider-key", {"provider": "totally-unknown", "key": "sk-x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_gemini_is_rejected_here_even_though_its_a_real_cli_provider_elsewhere(self):
        # DEC-15's API-key-mode scope (API_KEY_MODE_PROVIDERS) deliberately
        # was not extended to gemini when PROVIDERS grew in Phase 5 -- this
        # endpoint specifically must keep rejecting it, even though
        # GET /api/providers and POST /api/run both now recognize it fine.
        status, data = self._post("/api/provider-key", {"provider": "gemini", "key": "sk-x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_a_write_failure_is_a_clean_400_not_a_crashed_request(self):
        # save_credential() doesn't catch OSError itself -- unlike
        # touch_registry() (best-effort, logged not raised), the write
        # here IS the point of the request, so a failure (simulated the
        # same way RegistryTests does: the base dir exists as a file, not
        # a directory) must surface as an ordinary error response instead
        # of an uncaught exception killing this request's thread with no
        # JSON reply at all.
        self.base_dir.parent.mkdir(parents=True, exist_ok=True)
        self.base_dir.write_text("a file, not a directory", encoding="utf-8")
        status, data = self._post("/api/provider-key", {"provider": "claude", "key": "sk-x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class CheckForUpdateInBackgroundTests(unittest.TestCase):
    def test_sets_state_update_info_from_check_for_update(self):
        state = webui.AppState(None)
        with mock.patch(
            "handoff_webui.check_for_update",
            return_value={"latest_version": "0.2.0", "current_version": "0.1.0", "url": "https://example.invalid"},
        ):
            webui._check_for_update_in_background(state)
        self.assertEqual(state.update_info["latest_version"], "0.2.0")

    def test_none_result_leaves_update_info_none(self):
        state = webui.AppState(None)
        with mock.patch("handoff_webui.check_for_update", return_value=None):
            webui._check_for_update_in_background(state)
        self.assertIsNone(state.update_info)


class UpdateCheckLiveServerTests(unittest.TestCase):
    """GET /api/update-check over a real HTTP server -- same pattern as
    ProviderApiLiveServerTests."""

    def setUp(self):
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
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_no_update_info_yet_reports_unavailable(self):
        # Covers both "background check hasn't finished" and "checked,
        # found nothing newer" -- both produce the same response shape on
        # purpose (see the route's own comment): the frontend has no need
        # to distinguish "still checking" from "you're up to date".
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"update_available": False})

    def test_populated_update_info_is_reflected(self):
        self.state.update_info = {"latest_version": "0.2.0", "current_version": "0.1.0", "url": "https://example.invalid"}
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertTrue(data["update_available"])
        self.assertEqual(data["latest_version"], "0.2.0")
        self.assertEqual(data["url"], "https://example.invalid")


if __name__ == "__main__":
    unittest.main()
