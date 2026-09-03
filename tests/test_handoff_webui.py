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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402
import handoff_webui as webui  # noqa: E402
import webui_api_key_mode  # noqa: E402
import webui_bridge_run  # noqa: E402
import webui_chat_storage  # noqa: E402
import webui_common  # noqa: E402
import webui_credentials  # noqa: E402
import webui_workspace  # noqa: E402


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


class AugmentPathForGuiLaunchTests(unittest.TestCase):
    """A GUI launch (Finder/Dock, not a terminal) inherits launchd's
    minimal PATH on macOS -- codex/claude/gemini CLIs installed via
    Homebrew/nvm/etc. become invisible to shutil.which() as a result.
    augment_path_for_gui_launch() asks the user's login shell for its
    real PATH and merges in whatever's missing."""

    def setUp(self):
        self._orig_path = os.environ.get("PATH", "")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._orig_path))

    def test_no_op_on_non_macos(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        with mock.patch.object(webui.sys, "platform", "linux"), mock.patch.object(webui.subprocess, "run") as run:
            webui.augment_path_for_gui_launch()
        run.assert_not_called()
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_merges_missing_entries_from_login_shell_path(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="__AHB_PATH__/opt/homebrew/bin:/usr/bin:/bin__AHB_PATH__\n"
        )
        with mock.patch.object(webui.sys, "platform", "darwin"), mock.patch.object(
            webui.subprocess, "run", return_value=fake_result
        ) as run:
            webui.augment_path_for_gui_launch()
        run.assert_called_once()
        # New entry appended; already-present entries not duplicated.
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin:/opt/homebrew/bin")

    def test_shell_spawn_failure_leaves_path_untouched(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        with mock.patch.object(webui.sys, "platform", "darwin"), mock.patch.object(
            webui.subprocess, "run", side_effect=OSError("no such shell")
        ):
            webui.augment_path_for_gui_launch()
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_shell_timeout_leaves_path_untouched(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        with mock.patch.object(webui.sys, "platform", "darwin"), mock.patch.object(
            webui.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="zsh", timeout=5)
        ):
            webui.augment_path_for_gui_launch()
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")

    def test_unparseable_shell_output_leaves_path_untouched(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="some unrelated shell startup noise\n")
        with mock.patch.object(webui.sys, "platform", "darwin"), mock.patch.object(
            webui.subprocess, "run", return_value=fake_result
        ):
            webui.augment_path_for_gui_launch()
        self.assertEqual(os.environ["PATH"], "/usr/bin:/bin")


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


class MainNoWorkspaceStartupBannerTests(unittest.TestCase):
    """Regression coverage for a structure audit finding (2026-08-20):
    main()'s no-workspace startup banner printed AUTO_WORKSPACE_BASE_DIR
    via a stale `from webui_common import AUTO_WORKSPACE_BASE_DIR` binding
    instead of the module-qualified `webui_common.AUTO_WORKSPACE_BASE_DIR`
    every other consumer uses -- immune to mock.patch("webui_common.
    AUTO_WORKSPACE_BASE_DIR", ...), so it would have silently kept
    printing the real ~/Documents/Agent Handoff Bridge path under test.
    Never actually binds a socket or blocks on a real server thread
    (ThreadingHTTPServer and the background update-check call are both
    mocked) -- same "must return fast" constraint as
    MainRefusesNonLoopbackHostTests above."""

    def test_prints_the_patched_auto_workspace_base_dir_not_the_real_one(self):
        orig_cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory()
        fake_base_dir = Path(tmp.name) / "fake-auto-workspace-base"
        try:
            os.chdir(tmp.name)  # no .handoff marker here -> "no workspace yet"
            with mock.patch.object(webui_common, "AUTO_WORKSPACE_BASE_DIR", fake_base_dir), mock.patch.object(
                webui, "_bridge_check_for_update", return_value={"status": "unavailable"}
            ), mock.patch.object(webui, "ThreadingHTTPServer", return_value=mock.Mock()), mock.patch(
                "builtins.print"
            ) as print_spy:
                exit_code = webui.main(["--no-browser"])
        finally:
            os.chdir(orig_cwd)
            tmp.cleanup()

        self.assertEqual(exit_code, 0)
        printed = "\n".join(str(call.args[0]) if call.args else "" for call in print_spy.call_args_list)
        self.assertIn(str(fake_base_dir), printed)
        self.assertNotIn(str(webui_common.AUTO_WORKSPACE_BASE_DIR), printed)


class BridgeCommandPrefixTests(unittest.TestCase):
    """Phase 7a (DEC-22): when this module runs frozen (PyInstaller, as
    the Tauri sidecar), sys.executable is the frozen server binary
    itself, not a real Python interpreter -- bridge_command_prefix()
    must switch to invoking a sibling CLI sidecar binary directly
    instead of `[sys.executable, BRIDGE_SCRIPT]`."""

    def test_unfrozen_uses_sys_executable_and_bridge_script(self):
        with mock.patch.object(webui.sys, "frozen", False, create=True):
            self.assertEqual(webui_common.bridge_command_prefix(), [webui.sys.executable, str(webui_common.BRIDGE_SCRIPT)])

    def test_frozen_uses_a_sibling_cli_sidecar_next_to_sys_executable(self):
        with mock.patch.object(webui.sys, "frozen", True, create=True), mock.patch.object(
            webui.sys, "executable", "/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-server"
        ), mock.patch.object(webui.sys, "platform", "darwin"):
            prefix = webui_common.bridge_command_prefix()
        self.assertEqual(
            prefix, ["/Applications/Agent Handoff Bridge.app/Contents/MacOS/agent-handoff-bridge-cli"]
        )

    def test_frozen_on_windows_uses_the_exe_suffix(self):
        # bridge_command_prefix() builds this via PureWindowsPath (not the
        # host-native Path) when sys.platform is "win32", so the result is
        # genuinely backslash-style regardless of which OS runs this test --
        # expected value constructed the same way rather than hand-typed, so
        # it can't drift from what PureWindowsPath actually produces.
        with mock.patch.object(webui.sys, "frozen", True, create=True), mock.patch.object(
            webui.sys, "executable", "/apps/agent-handoff-bridge/agent-handoff-bridge-server.exe"
        ), mock.patch.object(webui.sys, "platform", "win32"):
            prefix = webui_common.bridge_command_prefix()
        expected = PureWindowsPath("/apps/agent-handoff-bridge") / "agent-handoff-bridge-cli.exe"
        self.assertEqual(prefix, [str(expected)])


class SafeJoinTests(unittest.TestCase):
    def test_relative_path_within_root_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "file.txt").write_text("hi", encoding="utf-8")
            resolved = webui_workspace.safe_join(root, "sub/file.txt")
            self.assertEqual(resolved, (root / "sub" / "file.txt").resolve())

    def test_empty_path_resolves_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(webui_workspace.safe_join(root, ""), root.resolve())

    def test_dotdot_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.safe_join(root, "../../etc/passwd")

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.safe_join(root, "/etc/passwd")

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
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.safe_join(root, "escape/secret.txt")


class ListTreeEntriesTests(unittest.TestCase):
    def test_dirs_before_files_alphabetical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zeta.txt").write_text("z", encoding="utf-8")
            (root / "alpha.txt").write_text("a", encoding="utf-8")
            (root / "beta_dir").mkdir()
            entries = webui_workspace.list_tree_entries(root, "")
            names = [e["name"] for e in entries]
            self.assertEqual(names, ["beta_dir", "alpha.txt", "zeta.txt"])
            self.assertEqual(entries[0]["type"], "dir")
            self.assertEqual(entries[1]["type"], "file")

    def test_excluded_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "src").mkdir()
            names = [e["name"] for e in webui_workspace.list_tree_entries(root, "")]
            self.assertNotIn(".git", names)
            self.assertIn("src", names)

    def test_nonexistent_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.list_tree_entries(Path(tmp), "nope")

    def test_file_path_raises_not_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.list_tree_entries(root, "f.txt")


class ReadFilePreviewTests(unittest.TestCase):
    def test_text_file_returns_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("hello world", encoding="utf-8")
            preview = webui_workspace.read_file_preview(root, "notes.md")
            self.assertEqual(preview["content"], "hello world")
            self.assertFalse(preview["truncated"])

    def test_binary_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin.dat").write_bytes(b"\x00\x01\x02binary")
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.read_file_preview(root, "bin.dat")

    def test_oversized_file_is_truncated_not_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_max = webui_workspace.MAX_FILE_BYTES
            webui_workspace.MAX_FILE_BYTES = 10
            try:
                (root / "big.txt").write_text("0123456789" * 5, encoding="utf-8")
                preview = webui_workspace.read_file_preview(root, "big.txt")
                self.assertTrue(preview["truncated"])
                self.assertEqual(len(preview["content"]), 10)
            finally:
                webui_workspace.MAX_FILE_BYTES = original_max

    def test_directory_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.read_file_preview(root, "sub")


class LiveServerTests(unittest.TestCase):
    """Exercises the actual HTTP layer, not just the pure functions above."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        # newline="": read_file_preview() reads in binary mode (preserving
        # the file's real on-disk bytes, by design -- see its own
        # docstring), so the fixture must pin exact bytes too; the default
        # write_text() newline translation would otherwise write "\r\n" on
        # Windows and desync from what this test asserts.
        (root / "README.md").write_text("# hello\n", encoding="utf-8", newline="")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8", newline="")

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
        self.assertEqual(data["version"], hb.BRIDGE_VERSION)

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
            resolved = webui_workspace.validate_workspace_candidate(tmp)
            self.assertEqual(resolved, Path(tmp).resolve())

    def test_empty_path_rejected(self):
        with self.assertRaises(webui_common.WorkspaceError):
            webui_workspace.validate_workspace_candidate("")

    def test_relative_path_rejected(self):
        with self.assertRaises(webui_common.WorkspaceError):
            webui_workspace.validate_workspace_candidate("relative/dir")

    def test_nonexistent_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.validate_workspace_candidate(str(Path(tmp) / "does-not-exist"))

    def test_file_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "f.txt"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.validate_workspace_candidate(str(file_path))


class HasHandoffMarkerTests(unittest.TestCase):
    def test_true_when_dot_handoff_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            self.assertTrue(webui_workspace.has_handoff_marker(root))

    def test_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(webui_workspace.has_handoff_marker(Path(tmp)))

    def test_false_when_dot_handoff_is_a_file_not_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").write_text("not a dir", encoding="utf-8")
            self.assertFalse(webui_workspace.has_handoff_marker(root))


class ResolveStartupWorkspaceTests(unittest.TestCase):
    def test_explicit_valid_path_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, error = webui_workspace.resolve_startup_workspace(tmp, Path("/irrelevant"))
            self.assertIsNone(error)
            self.assertEqual(workspace, Path(tmp).resolve())

    def test_explicit_invalid_path_is_an_error_not_auto_create(self):
        # DEC-04: an explicit --workspace typo must fail loudly, never
        # silently fall into the "no workspace" auto-create flow -- an
        # explicit path means the user is confident about where they meant
        # to point.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            workspace, error = webui_workspace.resolve_startup_workspace(str(missing), Path("/irrelevant"))
            self.assertIsNone(workspace)
            self.assertIsNotNone(error)
            self.assertIn("does not exist", error)

    def test_no_arg_with_initialized_cwd_resolves_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".handoff").mkdir()
            workspace, error = webui_workspace.resolve_startup_workspace(None, cwd)
            self.assertIsNone(error)
            self.assertEqual(workspace, cwd.resolve())

    def test_no_arg_with_uninitialized_cwd_returns_no_workspace_not_an_error(self):
        # 2026-08-04 DEC-04 revision: "cwd invalid" barely ever happens (a
        # running process's cwd essentially always exists), which would
        # make the whole Phase 2 flow unreachable in practice -- the real
        # condition is "not yet an initialized handoff workspace".
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)  # no .handoff/ -- e.g. launcher double-clicked from Downloads
            workspace, error = webui_workspace.resolve_startup_workspace(None, cwd)
            self.assertIsNone(workspace)
            self.assertIsNone(error)


class SlugifyForFolderNameTests(unittest.TestCase):
    def test_ascii_text_becomes_hyphenated_slug(self):
        self.assertEqual(webui_workspace.slugify_for_folder_name("Fix the deploy script"), "Fix-the-deploy-script")

    def test_korean_text_is_preserved(self):
        # DEC-05's whole point: unlike a typical ASCII-only slugify library
        # that would strip/transliterate this to nothing, \w is
        # Unicode-aware and keeps Hangul -- matching the wireframe's own
        # example folder name.
        slug = webui_workspace.slugify_for_folder_name("배포 스크립트에 있는 버그를 점검해줘.")
        self.assertEqual(slug, "배포-스크립트에-있는-버그를-점검해줘")

    def test_empty_text_becomes_untitled(self):
        self.assertEqual(webui_workspace.slugify_for_folder_name(""), "untitled")

    def test_whitespace_only_becomes_untitled(self):
        self.assertEqual(webui_workspace.slugify_for_folder_name("   \n\t  "), "untitled")

    def test_punctuation_only_becomes_untitled(self):
        self.assertEqual(webui_workspace.slugify_for_folder_name("... !!! ???"), "untitled")

    def test_long_text_is_truncated_to_max_length(self):
        slug = webui_workspace.slugify_for_folder_name("word " * 30)
        self.assertLessEqual(len(slug), webui_workspace.MAX_SLUG_LENGTH)
        self.assertFalse(slug.endswith("-"))


class BuildAutoWorkspaceNameTests(unittest.TestCase):
    def test_uses_text_when_present(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui_workspace.build_auto_workspace_name("Fix bug", [], now)
        self.assertEqual(name, "2026-08-04-Fix-bug")

    def test_falls_back_to_attachment_name_when_no_text(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui_workspace.build_auto_workspace_name("", [{"name": "report.pdf"}], now)
        self.assertEqual(name, "2026-08-04-report-pdf")

    def test_falls_back_to_untitled_when_neither_text_nor_attachments(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        name = webui_workspace.build_auto_workspace_name("", [], now)
        self.assertEqual(name, "2026-08-04-untitled")


class ResolveTaskForFirstMessageTests(unittest.TestCase):
    def test_uses_text_when_present(self):
        self.assertEqual(webui_workspace.resolve_task_for_first_message("Fix bug", []), "Fix bug")

    def test_attachments_only_produces_a_meaningful_task_not_a_generic_placeholder(self):
        # Regression: the folder name already fell back to the attachment's
        # name (build_auto_workspace_name), but state.json's task -- which
        # feeds every future prompt's "## Task" section -- used to ignore
        # that and always record the generic placeholder for an
        # attachments-only first message.
        task = webui_workspace.resolve_task_for_first_message("", [{"name": "report.pdf"}])
        self.assertIn("report.pdf", task)
        self.assertNotEqual(task, "Continue the current handoff task.")

    def test_falls_back_to_placeholder_when_neither_text_nor_attachments(self):
        self.assertEqual(webui_workspace.resolve_task_for_first_message("", []), "Continue the current handoff task.")


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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_creates_directory_under_base_dir_with_date_and_slug(self):
        workspace = webui_workspace.create_workspace_for_first_message("Fix the deploy script", [])
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

        with mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", symlinked_base):
            workspace = webui_workspace.create_workspace_for_first_message("hello", [])

        self.assertEqual(workspace.parent, real_target.resolve())

    def test_runs_init_and_produces_state_json_with_task_set_to_the_message(self):
        workspace = webui_workspace.create_workspace_for_first_message("investigate the flaky test", [])
        state_path = workspace / ".handoff" / "state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["task"], "investigate the flaky test")

    def test_attachments_only_message_records_a_meaningful_task(self):
        workspace = webui_workspace.create_workspace_for_first_message("", [{"name": "report.pdf"}])
        state = json.loads((workspace / ".handoff" / "state.json").read_text(encoding="utf-8"))
        self.assertIn("report.pdf", state["task"])

    def test_produces_current_md_too_not_just_state_json(self):
        # init_handoff() writes both unconditionally on success -- the
        # explicit post-condition check in create_workspace_for_first_message()
        # relies on that being true.
        workspace = webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertTrue((workspace / ".handoff" / "current.md").exists())

    def test_collision_appends_numeric_suffix_and_never_reuses_the_folder(self):
        first = webui_workspace.create_workspace_for_first_message("same summary", [])
        second = webui_workspace.create_workspace_for_first_message("same summary", [])
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue(second.name.endswith("-2"))

    def test_ensures_chat_gitignore_is_written(self):
        workspace = webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertTrue((workspace / ".handoff" / "webui" / ".gitignore").exists())

    def test_message_matching_an_init_flag_name_is_still_treated_as_the_task(self):
        # Without "--" before the task in the subprocess argv, argparse
        # would consume a first message that's literally "--no-install"
        # (or any other real flag of `init`) as that option instead of the
        # positional task, and fail with "the following arguments are
        # required: task" -- a real user message shouldn't be able to
        # break scaffolding just by looking like a CLI flag.
        workspace = webui_workspace.create_workspace_for_first_message("--no-install", [])
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
        with self.assertRaises(webui_common.WorkspaceError):
            webui_workspace.create_workspace_for_first_message("hello", [])

    def test_init_subprocess_failure_raises_and_cleans_up_the_directory(self):
        # Regression: the subprocess result used to be discarded entirely --
        # a failing `init` (bad permissions, disk full, a bug in
        # handoff_bridge.py) would silently leave a half-scaffolded
        # directory that append_chat_message() then wrote into as if it
        # were a real workspace.
        failed = mock.Mock(returncode=1, stdout="", stderr="boom: disk full")
        with mock.patch("handoff_bridge.subprocess.run", return_value=failed):
            with self.assertRaises(webui_common.WorkspaceError) as ctx:
                webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertIn("boom: disk full", str(ctx.exception))
        # and it didn't leave an orphaned empty folder behind
        self.assertEqual(list(self.base_dir.iterdir()), [])

    def test_init_subprocess_timeout_raises_and_cleans_up_the_directory(self):
        with mock.patch(
            "handoff_bridge.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="handoff_bridge.py", timeout=30),
        ):
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertEqual(list(self.base_dir.iterdir()), [])

    def test_exit_zero_without_the_expected_handoff_files_is_still_a_failure(self):
        # Defense in depth: don't trust the exit code alone. A "successful"
        # init that -- for whatever reason -- didn't actually produce
        # .handoff/state.json or .handoff/current.md must not be confirmed
        # as a real workspace (docs/architecture.md: those are the durable
        # handoff surface).
        fake_success_but_did_nothing = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("handoff_bridge.subprocess.run", return_value=fake_success_but_did_nothing):
            with self.assertRaises(webui_common.WorkspaceError) as ctx:
                webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertIn(".handoff/", str(ctx.exception))
        self.assertEqual(list(self.base_dir.iterdir()), [])

    def test_init_subprocess_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine -- to decode this subprocess's stdout/stderr,
        # and `task` here is the user's own first message.
        #
        # The mocked "success" here doesn't actually create real
        # .handoff/ files, so create_workspace_for_first_message() still
        # raises afterward (test_exit_zero_without_the_expected_handoff_files_is_still_a_failure
        # above covers that path directly) -- irrelevant to this test,
        # which only cares that subprocess.run() was called with the
        # right kwargs, captured before that later exception.
        success = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("handoff_bridge.subprocess.run", return_value=success) as run_spy:
            with self.assertRaises(webui_common.WorkspaceError):
                webui_workspace.create_workspace_for_first_message("hello", [])
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")


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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_read_registry_missing_file_returns_empty_list(self):
        self.assertEqual(webui_chat_storage.read_registry(), [])

    def test_read_registry_unreadable_path_returns_empty_list_not_raise(self):
        # registry.json existing as a *directory* (permissions issues are
        # the more realistic real-world case, but a directory-where-a-file-
        # is-expected is the portable way to force the same OSError family
        # without relying on chmod, which root/CI can bypass).
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").mkdir()
        self.assertEqual(webui_chat_storage.read_registry(), [])

    def test_read_registry_skips_malformed_entries_instead_of_crashing(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text(
            json.dumps([{"path": "/w/good", "name": "good"}, "not a dict", {"name": "no path field"}, 42]),
            encoding="utf-8",
        )
        entries = webui_chat_storage.read_registry()
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
            webui_chat_storage.touch_registry(Path("/some/workspace"), webui_common.utc_now())
        except OSError:
            self.fail("touch_registry() must not raise on a write failure")

    def test_read_registry_malformed_json_returns_empty_list(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text("not json", encoding="utf-8")
        self.assertEqual(webui_chat_storage.read_registry(), [])

    def test_read_registry_non_list_json_returns_empty_list(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "registry.json").write_text('{"not": "a list"}', encoding="utf-8")
        self.assertEqual(webui_chat_storage.read_registry(), [])

    def test_touch_registry_writes_it_to_the_patched_base_dir_not_the_real_one(self):
        webui_chat_storage.touch_registry(Path("/some/workspace"), datetime(2026, 8, 4, tzinfo=timezone.utc))
        self.assertTrue((self.base_dir / "registry.json").exists())

    def test_touch_registry_adds_an_entry(self):
        webui_chat_storage.touch_registry(Path("/w/project-a"), datetime(2026, 8, 4, tzinfo=timezone.utc))
        entries = webui_chat_storage.read_registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], str(Path("/w/project-a")))
        self.assertEqual(entries[0]["name"], "project-a")

    def test_touching_the_same_workspace_again_moves_it_to_front_not_duplicates(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        webui_chat_storage.touch_registry(Path("/w/a"), now)
        webui_chat_storage.touch_registry(Path("/w/b"), now)
        webui_chat_storage.touch_registry(Path("/w/a"), now)  # re-touch a
        entries = webui_chat_storage.read_registry()
        self.assertEqual([e["path"] for e in entries], [str(Path("/w/a")), str(Path("/w/b"))])

    def test_caps_at_max_entries_evicting_the_oldest(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        for i in range(webui_chat_storage.REGISTRY_MAX_ENTRIES + 3):
            webui_chat_storage.touch_registry(Path(f"/w/project-{i}"), now)
        entries = webui_chat_storage.read_registry()
        self.assertEqual(len(entries), webui_chat_storage.REGISTRY_MAX_ENTRIES)
        # most recent (highest i) survive, oldest (0, 1, 2) evicted
        paths = [e["path"] for e in entries]
        self.assertIn(str(Path(f"/w/project-{webui_chat_storage.REGISTRY_MAX_ENTRIES + 2}")), paths)
        self.assertNotIn(str(Path("/w/project-0")), paths)


class PairMessagesIntoTurnsTests(unittest.TestCase):
    def test_user_message_alone_produces_a_turn_with_no_provider_yet(self):
        turns = webui_chat_storage.pair_messages_into_turns([{"role": "user", "text": "hi", "ts": "t1"}])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "hi")
        self.assertIsNone(turns[0]["provider"])

    def test_user_plus_agent_reply_forms_one_turn(self):
        messages = [
            {"role": "user", "text": "fix the bug", "ts": "t1"},
            {"role": "agent", "text": "done", "provider": "codex", "status": "success", "ts": "t2"},
        ]
        turns = webui_chat_storage.pair_messages_into_turns(messages)
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
        turns = webui_chat_storage.pair_messages_into_turns(messages)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["provider"], "claude")
        self.assertEqual(turns[0]["status"], "success")

    def test_system_messages_do_not_start_a_turn(self):
        messages = [
            {"role": "system", "text": "workspace switched", "ts": "t1"},
            {"role": "user", "text": "hi", "ts": "t2"},
        ]
        turns = webui_chat_storage.pair_messages_into_turns(messages)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "hi")

    def test_two_separate_user_messages_produce_two_turns(self):
        messages = [
            {"role": "user", "text": "first", "ts": "t1"},
            {"role": "agent", "text": "ok", "provider": "codex", "status": "success", "ts": "t2"},
            {"role": "user", "text": "second", "ts": "t3"},
            {"role": "agent", "text": "ok2", "provider": "claude", "status": "success", "ts": "t4"},
        ]
        turns = webui_chat_storage.pair_messages_into_turns(messages)
        self.assertEqual([t["text"] for t in turns], ["first", "second"])


class CollectRecentTurnsTests(unittest.TestCase):
    def test_caps_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, tzinfo=timezone.utc)
            for i in range(8):
                webui_chat_storage.append_chat_message(root, "user", f"turn {i}", [], now)
                webui_chat_storage.append_chat_message(root, "agent", "ok", [], now, provider="codex", status="success")
            turns = webui_chat_storage.collect_recent_turns(root, limit=5)
            self.assertEqual(len(turns), 5)
            # newest first
            self.assertEqual(turns[0]["text"], "turn 7")

    def test_scans_backward_across_months_when_current_month_is_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = datetime(2026, 6, 1, tzinfo=timezone.utc)
            newer = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "old turn", [], older)
            webui_chat_storage.append_chat_message(root, "user", "new turn", [], newer)
            turns = webui_chat_storage.collect_recent_turns(root, limit=5)
            self.assertEqual({t["text"] for t in turns}, {"old turn", "new turn"})

    def test_empty_workspace_returns_no_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui_chat_storage.collect_recent_turns(Path(tmp)), [])

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
            webui_chat_storage.append_chat_message(root, "user", "cross-boundary turn", [], end_of_july)
            webui_chat_storage.append_chat_message(
                root, "agent", "done", [], start_of_august, provider="codex", status="success"
            )
            turns = webui_chat_storage.collect_recent_turns(root, limit=5)
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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.projects_dir = Path(self.tmp.name) / "projects"
        self.projects_dir.mkdir()

    def _make_project(self, name, text="hello"):
        root = self.projects_dir / name
        root.mkdir()
        webui_chat_storage.append_chat_message(root, "user", text, [], webui_common.utc_now())
        return root

    def test_current_workspace_is_pinned_first_and_marked(self):
        current = self._make_project("current-proj")
        other = self._make_project("other-proj")
        webui_chat_storage.touch_registry(other, webui_common.utc_now())
        webui_chat_storage.touch_registry(current, webui_common.utc_now())
        groups = webui_chat_storage.build_history_drawer(current)
        self.assertEqual(groups[0]["path"], str(current))
        self.assertTrue(groups[0]["current"])
        self.assertFalse(groups[1]["current"])

    def test_registry_entry_for_a_deleted_folder_is_silently_skipped(self):
        gone = self.projects_dir / "deleted-proj"
        gone.mkdir()
        webui_chat_storage.touch_registry(gone, webui_common.utc_now())
        shutil.rmtree(gone)
        groups = webui_chat_storage.build_history_drawer(None)
        self.assertEqual(groups, [])

    def test_no_current_workspace_still_returns_registry_groups(self):
        project = self._make_project("solo-proj")
        webui_chat_storage.touch_registry(project, webui_common.utc_now())
        groups = webui_chat_storage.build_history_drawer(None)
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["current"])

    def test_each_group_carries_its_own_turns(self):
        project = self._make_project("with-turns", text="do the thing")
        webui_chat_storage.touch_registry(project, webui_common.utc_now())
        groups = webui_chat_storage.build_history_drawer(None)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["turns"][0]["text"], "do the thing")


class ChatStorageTests(unittest.TestCase):
    def test_append_then_read_current_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
            saved = webui_chat_storage.append_chat_message(root, "user", "hello", [], now)
            self.assertEqual(saved["role"], "user")
            self.assertEqual(saved["text"], "hello")
            self.assertIn("id", saved)

            messages = webui_chat_storage.read_month_messages(root, "2026-08")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["text"], "hello")

    def test_multiple_appends_preserve_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "first", [], now)
            webui_chat_storage.append_chat_message(root, "user", "second", [], now)
            messages = webui_chat_storage.read_month_messages(root, "2026-08")
            self.assertEqual([m["text"] for m in messages], ["first", "second"])

    def test_invalid_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(webui_common.WorkspaceError):
                webui_chat_storage.append_chat_message(Path(tmp), "assistant", "hi", [], utc_now_for_test())

    def test_read_missing_month_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui_chat_storage.read_month_messages(Path(tmp), "2020-01"), [])

    def test_read_tolerates_plain_file_vanishing_between_exists_check_and_read(self):
        # Regression test: read_month_messages() used to call plain.exists()
        # then plain.read_text() with no try/except in between. On the real
        # ThreadingHTTPServer, archive_old_months() can unlink() that same
        # file (under WriteLock) in that exact window, from another
        # request's thread -- an uncaught FileNotFoundError used to escape.
        # Force exists() to report True for a file that isn't actually
        # there to simulate landing exactly in that TOCTOU gap; the fix
        # should treat it the same as "never existed" (empty list), not
        # raise.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("pathlib.Path.exists", return_value=True):
                self.assertEqual(webui_chat_storage.read_month_messages(root, "2020-01"), [])

    def test_list_available_months_sees_both_plain_and_compressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat_dir = webui_chat_storage.chat_dir(root)
            chat_dir.mkdir(parents=True)
            (chat_dir / "2026-07.jsonl.gz").write_bytes(b"")
            (chat_dir / "2026-08.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(webui_chat_storage.list_available_months(root), ["2026-07", "2026-08"])

    def test_archive_compresses_past_months_but_not_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "old message", [], july)
            webui_chat_storage.append_chat_message(root, "user", "new message", [], august)

            archived = webui_chat_storage.archive_old_months(root, august)

            self.assertEqual(archived, ["2026-07"])
            chat_dir = webui_chat_storage.chat_dir(root)
            self.assertTrue((chat_dir / "2026-07.jsonl.gz").exists())
            self.assertFalse((chat_dir / "2026-07.jsonl").exists())
            self.assertTrue((chat_dir / "2026-08.jsonl").exists())

    def test_archived_month_still_readable_after_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "archive me", [], july)
            webui_chat_storage.archive_old_months(root, august)

            messages = webui_chat_storage.read_month_messages(root, "2026-07")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["text"], "archive me")

    def test_archive_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            july = datetime(2026, 7, 15, tzinfo=timezone.utc)
            august = datetime(2026, 8, 4, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "msg", [], july)
            first = webui_chat_storage.archive_old_months(root, august)
            second = webui_chat_storage.archive_old_months(root, august)
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
            webui_chat_storage.append_chat_message(root, "user", "may", [], may_)
            webui_chat_storage.append_chat_message(root, "user", "june", [], june)
            webui_chat_storage.append_chat_message(root, "user", "july", [], july)
            webui_chat_storage.append_chat_message(root, "user", "august", [], august)

            archived = webui_chat_storage.archive_old_months(root, august)

            self.assertEqual(sorted(archived), ["2026-05", "2026-06", "2026-07"])
            chat_dir = webui_chat_storage.chat_dir(root)
            for month in ("2026-05", "2026-06", "2026-07"):
                self.assertTrue((chat_dir / f"{month}.jsonl.gz").exists(), f"{month} not compressed")
                self.assertFalse((chat_dir / f"{month}.jsonl").exists(), f"{month} plain file still present")
            self.assertTrue((chat_dir / "2026-08.jsonl").exists())
            for month in ("2026-05", "2026-06", "2026-07"):
                messages = webui_chat_storage.read_month_messages(root, month)
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
        self.assertEqual(webui_bridge_run.classify_run_status(False, "none: no handoff signal detected"), "success")

    def test_tool_failure_is_fail(self):
        self.assertEqual(webui_bridge_run.classify_run_status(True, "tool_failure: provider command not found"), "fail")

    def test_unknown_is_fail(self):
        self.assertEqual(webui_bridge_run.classify_run_status(True, "unknown: provider emitted an unrecognized error"), "fail")

    def test_rate_limit_is_handoff(self):
        self.assertEqual(webui_bridge_run.classify_run_status(True, "rate_limit: matched rate_limit signal"), "handoff")

    def test_quota_is_handoff(self):
        self.assertEqual(webui_bridge_run.classify_run_status(True, "quota: matched quota signal"), "handoff")


class BuildRunPromptTests(unittest.TestCase):
    def test_text_only_passes_through_unchanged(self):
        self.assertEqual(webui_bridge_run.build_run_prompt("hello", []), "hello")

    def test_attachment_content_is_included(self):
        prompt = webui_bridge_run.build_run_prompt(
            "look at this", [{"name": "a.py", "path": "a.py", "content": "print(1)", "truncated": False}]
        )
        self.assertIn("look at this", prompt)
        self.assertIn("a.py", prompt)
        self.assertIn("print(1)", prompt)

    def test_truncated_attachment_is_noted(self):
        prompt = webui_bridge_run.build_run_prompt(
            "", [{"name": "big.txt", "path": "big.txt", "content": "...", "truncated": True}]
        )
        self.assertIn("(truncated)", prompt)

    def test_binary_attachment_with_no_content_is_noted_not_dropped(self):
        prompt = webui_bridge_run.build_run_prompt("", [{"name": "image.png", "path": "image.png", "content": None}])
        self.assertIn("image.png", prompt)
        self.assertIn("no preview available", prompt)

    def test_attachment_only_with_no_text_still_produces_a_prompt(self):
        prompt = webui_bridge_run.build_run_prompt("", [{"name": "a.py", "path": "a.py", "content": "x = 1", "truncated": False}])
        self.assertTrue(prompt.strip())
        self.assertIn("x = 1", prompt)


class ReadStateHistoryTests(unittest.TestCase):
    def test_missing_state_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui_bridge_run.read_state_history(Path(tmp)), [])

    def test_malformed_state_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            (root / ".handoff" / "state.json").write_text("not json", encoding="utf-8")
            self.assertEqual(webui_bridge_run.read_state_history(root), [])

    def test_reads_history_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            (root / ".handoff" / "state.json").write_text(
                json.dumps({"history": [{"provider": "codex"}]}), encoding="utf-8"
            )
            self.assertEqual(webui_bridge_run.read_state_history(root), [{"provider": "codex"}])


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
            webui_bridge_run._RUN_LOCK.acquire()
            try:
                with self.assertRaises(webui_bridge_run.RunAlreadyInProgressError):
                    webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            finally:
                webui_bridge_run._RUN_LOCK.release()

    def test_lock_is_released_after_a_normal_call_so_the_next_one_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            webui_bridge_run.run_provider_via_bridge(root, "codex", "first", None, "continue")
            # Would raise RunAlreadyInProgressError if the lock leaked.
            records = webui_bridge_run.run_provider_via_bridge(root, "codex", "second", None, "continue")
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
            with mock.patch("webui_bridge_run.subprocess.run", wraps=subprocess.run) as spy:
                webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", None, "continue")
            command = spy.call_args.args[0]
            self.assertIn("--timeout-seconds", command)
            idx = command.index("--timeout-seconds")
            self.assertEqual(command[idx + 1], str(webui_bridge_run.PROVIDER_RUN_TIMEOUT_SECONDS))
            self.assertEqual(spy.call_args.kwargs["timeout"], webui_bridge_run.OUTER_SUBPROCESS_TIMEOUT_SECONDS)

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
            with mock.patch("webui_bridge_run.subprocess.run", wraps=subprocess.run) as spy:
                webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", "--weird-model-name", "continue")
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
        real_run = subprocess.run  # patching webui_bridge_run.subprocess.run patches this module's too (same object)

        def _capture_prompt_file_then_run(command, **kwargs):
            idx = command.index("--prompt-file")
            captured["prompt_text"] = Path(command[idx + 1]).read_text(encoding="utf-8")
            return real_run(command, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            prompt = webui_bridge_run.build_run_prompt(
                "do the thing", [{"name": "a.py", "path": "a.py", "content": "print('hi')", "truncated": False}]
            )
            with mock.patch("webui_bridge_run.subprocess.run", side_effect=_capture_prompt_file_then_run):
                webui_bridge_run.run_provider_via_bridge(root, "codex", prompt, None, "continue")

        self.assertIn("do the thing", captured["prompt_text"])
        self.assertIn("a.py", captured["prompt_text"])
        self.assertIn("print('hi')", captured["prompt_text"])

    def test_successful_run_produces_one_history_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            records = webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", None, "continue")
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
            records = webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", None, "continue")
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
        records = webui_bridge_run.run_provider_via_bridge(
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
        records = webui_bridge_run.run_provider_via_bridge(
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
        # The timed-out-provider guess now goes through
        # _bridge_next_provider(), a subprocess call to handoff_bridge.py's
        # own next-provider subcommand (structure audit: this used to be an
        # in-process next_available_provider() import) -- that subprocess
        # would do its own real shutil.which() against whatever's actually
        # installed on the machine running this test, which isn't
        # deterministic, so _bridge_next_provider() itself is mocked
        # directly rather than trying to mock shutil.which() inside a
        # process this test never controls.
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

            with mock.patch("webui_bridge_run.subprocess.run", side_effect=_seed_partial_history_then_hang), mock.patch(
                "webui_bridge_run._bridge_next_provider", return_value="claude"
            ):
                records = webui_bridge_run.run_provider_via_bridge(root, "codex", "hello", None, "continue")

            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["provider"], "codex")
            self.assertEqual(records[1]["provider"], "claude")
            self.assertTrue(records[1]["handoff_needed"])
            self.assertTrue(records[1]["final_text"].startswith("Timed out"))


class RunProviderViaBridgeSubprocessEncodingTests(unittest.TestCase):
    """Not FakeProviderPathMixin-based (that skips outside a POSIX shell)
    -- this only needs to inspect the subprocess.run() call itself, not a
    real fake-provider round trip, so it runs on every platform including
    Windows, where this exact bug class was found."""

    def test_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess.run() falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine -- to decode this subprocess's stdout/stderr,
        # which can reflect arbitrary provider/prompt content.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch("webui_bridge_run.cli_available", return_value=True), mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(workspace, "codex", "hello", None, "continue")
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")


class RunProviderViaBridgeAutoFallbackFlagTests(unittest.TestCase):
    """Covers the Settings panel's auto-fallback toggle (webui/app.js):
    run_provider_via_bridge()'s new `auto_fallback` parameter must control
    whether the CLI-mode subprocess argv includes --auto-fallback at all --
    handoff_bridge.py run's own flag defaults to off, so omitting it (not
    a --no-auto-fallback counterpart) is the correct way to disable it."""

    def _run(self, auto_fallback):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch("webui_bridge_run.cli_available", return_value=True), mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(workspace, "codex", "hello", None, "continue", auto_fallback)
        return run_spy.call_args.args[0]

    def test_default_includes_auto_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch("webui_bridge_run.cli_available", return_value=True), mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(workspace, "codex", "hello", None, "continue")
        self.assertIn("--auto-fallback", run_spy.call_args.args[0])

    def test_explicit_true_includes_the_flag(self):
        self.assertIn("--auto-fallback", self._run(True))

    def test_false_omits_the_flag_entirely(self):
        command = self._run(False)
        self.assertNotIn("--auto-fallback", command)
        # And nothing invalid like a "--no-auto-fallback" was substituted --
        # handoff_bridge.py run has no such flag to begin with.
        self.assertNotIn("--no-auto-fallback", command)


class ApiRunLiveServerTests(FakeProviderPathMixin, unittest.TestCase):
    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Regression (found via a full-suite pollution hunt, 2026-08-16):
        # POST /api/open-folder (used below) unconditionally calls
        # touch_registry() as a side effect -- registry_path() depends on
        # AUTO_WORKSPACE_BASE_DIR, not on the workspace path being opened,
        # so omitting this patch wrote a real registry.json to the
        # developer's actual ~/Documents/Agent Handoff Bridge/ even though
        # self.root here is a tempdir.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
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

    def test_run_threads_auto_fallback_from_the_request_body(self):
        # webui/app.js's Settings-panel toggle sends this field; confirms
        # it actually reaches run_provider_via_bridge() rather than being
        # silently dropped somewhere in the /api/run handler.
        with mock.patch(
            "handoff_webui.run_provider_via_bridge",
            return_value=[
                {
                    "provider": "codex",
                    "model": None,
                    "exit_code": 0,
                    "session_id": None,
                    "final_text": "ok",
                    "handoff_needed": False,
                    "reason": "none",
                }
            ],
        ) as run_spy:
            status, _data = self._post("/api/run", {"provider": "codex", "text": "hi", "auto_fallback": False})
        self.assertEqual(status, 200)
        self.assertEqual(run_spy.call_args.args[-1], False)

    def test_run_defaults_auto_fallback_to_true_when_omitted(self):
        with mock.patch(
            "handoff_webui.run_provider_via_bridge",
            return_value=[
                {
                    "provider": "codex",
                    "model": None,
                    "exit_code": 0,
                    "session_id": None,
                    "final_text": "ok",
                    "handoff_needed": False,
                    "reason": "none",
                }
            ],
        ) as run_spy:
            status, _data = self._post("/api/run", {"provider": "codex", "text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(run_spy.call_args.args[-1], True)

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

    def test_run_with_a_custom_provider_actually_works_not_just_400s(self):
        # Regression: found while auditing provider selection (2026-09-02).
        # The provider-select dropdown (webui/app.js's
        # refreshProviderSelect()) lists custom providers (DEC-26) using
        # exactly this "custom:<name>" value (GET /api/providers'
        # custom_providers[].provider, from custom_provider_id()), but
        # POST /api/run's own validation used to reject anything that
        # wasn't literally "auto"/"codex"/"claude"/"gemini" with 400
        # "invalid provider" -- before ever reaching
        # webui_bridge_run.py's already-correct is_custom_provider()
        # dispatch. Selecting a custom provider from the real dropdown and
        # sending a message could never have worked.
        webui_credentials.save_custom_provider(
            "openrouter", "sk-or-test", "meta-llama/llama-3", "https://openrouter.ai/api/v1", "openai"
        )
        with mock.patch(
            "webui_api_key_mode.call_openai_compatible_chat_api", return_value={"ok": True, "text": "custom provider reply"}
        ) as spy:
            status, data = self._post("/api/run", {"provider": "custom:openrouter", "text": "hi"})
        self.assertEqual(status, 200)
        spy.assert_called_once()
        self.assertEqual(data["messages"][0]["text"], "custom provider reply")
        self.assertEqual(data["messages"][0]["provider"], "custom:openrouter")

    def test_concurrent_run_gets_409_not_a_hang_or_duplicate_message(self):
        webui_bridge_run._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/run", {"provider": "codex", "text": "hi"})
        finally:
            webui_bridge_run._RUN_LOCK.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_open_folder_is_rejected_while_a_run_is_in_flight(self):
        # A run writes into whatever workspace was active when it started
        # and persists into that workspace's chat log on completion --
        # switching state.workspace out from under it mid-run would
        # misdirect where that write (and the client's eventual render of
        # it) ends up.
        webui_bridge_run._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/open-folder", {"path": str(self.root)})
        finally:
            webui_bridge_run._RUN_LOCK.release()
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
        webui_bridge_run._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "user", "text": "a second message", "attachments": []})
        finally:
            webui_bridge_run._RUN_LOCK.release()
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
        webui_bridge_run._RUN_LOCK.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "system", "text": "note", "attachments": []})
        finally:
            webui_bridge_run._RUN_LOCK.release()
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
            webui_chat_storage.ensure_chat_gitignore(root)
            gitignore_path = root / ".handoff" / "webui" / ".gitignore"
            self.assertTrue(gitignore_path.exists())
            self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "*\n")

    def test_idempotent_does_not_clobber_a_customized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gitignore_path = root / ".handoff" / "webui" / ".gitignore"
            gitignore_path.parent.mkdir(parents=True)
            gitignore_path.write_text("# custom\n*\n", encoding="utf-8")
            webui_chat_storage.ensure_chat_gitignore(root)
            self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "# custom\n*\n")

    def test_append_chat_message_creates_it_even_in_a_workspace_with_no_dot_handoff_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse((root / ".handoff").exists())
            webui_chat_storage.append_chat_message(root, "user", "hi", [], utc_now_for_test())
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

            webui_chat_storage.append_chat_message(root, "user", "should stay untracked", [], utc_now_for_test())

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

        # Regression (found via a full-suite pollution hunt, 2026-08-16):
        # POST /api/open-folder unconditionally calls touch_registry() as a
        # side effect of switching workspace -- registry_path() depends on
        # AUTO_WORKSPACE_BASE_DIR, not on the workspace path being switched
        # to, so even though every workspace here is a tempdir, omitting
        # this patch made every /api/open-folder call in this class write
        # a real registry.json to the developer's actual
        # ~/Documents/Agent Handoff Bridge/.
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
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
        self.assertEqual(data["version"], hb.BRIDGE_VERSION)

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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
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
        webui_chat_storage.touch_registry(self.root, webui_common.utc_now())

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
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_read_credentials_missing_file_returns_empty_dict(self):
        self.assertEqual(webui_bridge_run.read_credentials(), {})

    def test_read_credentials_malformed_json_returns_empty_dict(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text("not json", encoding="utf-8")
        self.assertEqual(webui_bridge_run.read_credentials(), {})

    def test_read_credentials_non_dict_json_returns_empty_dict(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(webui_bridge_run.read_credentials(), {})

    def test_read_credentials_filters_unknown_provider(self):
        # "gemini" used to be the not-API-key-mode-supported example here
        # (DEC-15) -- DEC-25 extended API_KEY_MODE_PROVIDERS to include
        # it, so this now needs a genuinely made-up provider name that
        # will never be in that tuple.
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps({"claude": {"key": "sk-1"}, "totally-unknown-provider": {"key": "sk-2"}}), encoding="utf-8"
        )
        self.assertEqual(list(webui_bridge_run.read_credentials()), ["claude"])

    def test_read_credentials_filters_entry_with_no_key(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps({"claude": {"model": "claude-sonnet-5"}}), encoding="utf-8"
        )
        self.assertEqual(webui_bridge_run.read_credentials(), {})

    def test_save_then_read_round_trips_key_and_model(self):
        webui_credentials.save_credential("claude", "sk-ant-test", "claude-sonnet-5")
        creds = webui_bridge_run.read_credentials()
        self.assertEqual(creds["claude"], {"key": "sk-ant-test", "model": "claude-sonnet-5"})

    def test_save_with_no_model_stores_none(self):
        webui_credentials.save_credential("codex", "sk-test", None)
        self.assertIsNone(webui_bridge_run.read_credentials()["codex"]["model"])

    def test_save_with_empty_key_removes_the_entry(self):
        webui_credentials.save_credential("claude", "sk-ant-test", None)
        webui_credentials.save_credential("claude", "", None)
        self.assertNotIn("claude", webui_bridge_run.read_credentials())

    def test_saved_file_has_owner_only_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX file permissions not applicable")
        webui_credentials.save_credential("claude", "sk-ant-test", None)
        mode = webui_credentials.credentials_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_saving_one_provider_does_not_clobber_another(self):
        webui_credentials.save_credential("claude", "sk-claude", None)
        webui_credentials.save_credential("codex", "sk-codex", None)
        creds = webui_bridge_run.read_credentials()
        self.assertEqual(creds["claude"]["key"], "sk-claude")
        self.assertEqual(creds["codex"]["key"], "sk-codex")


class CliProviderModelTests(unittest.TestCase):
    """read_cli_provider_models()/save_cli_provider_model(): a provider's
    default --model override for CLI mode, distinct from the API-key-mode
    credentials above -- no key is ever involved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(webui_credentials.read_cli_provider_models(), {})

    def test_save_then_read_round_trips(self):
        webui_credentials.save_cli_provider_model("codex", "gpt-5-codex")
        self.assertEqual(webui_credentials.read_cli_provider_models(), {"codex": "gpt-5-codex"})

    def test_empty_model_removes_the_entry(self):
        webui_credentials.save_cli_provider_model("claude", "claude-opus")
        webui_credentials.save_cli_provider_model("claude", "")
        self.assertNotIn("claude", webui_credentials.read_cli_provider_models())

    def test_none_model_removes_the_entry(self):
        webui_credentials.save_cli_provider_model("claude", "claude-opus")
        webui_credentials.save_cli_provider_model("claude", None)
        self.assertNotIn("claude", webui_credentials.read_cli_provider_models())

    def test_whitespace_only_model_is_treated_as_empty(self):
        webui_credentials.save_cli_provider_model("gemini", "   ")
        self.assertEqual(webui_credentials.read_cli_provider_models(), {})

    def test_saving_one_provider_does_not_clobber_another(self):
        webui_credentials.save_cli_provider_model("codex", "gpt-5-codex")
        webui_credentials.save_cli_provider_model("claude", "claude-opus")
        models = webui_credentials.read_cli_provider_models()
        self.assertEqual(models["codex"], "gpt-5-codex")
        self.assertEqual(models["claude"], "claude-opus")

    def test_unknown_provider_in_the_raw_file_is_filtered_on_read(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps({"cli_models": {"claude": "claude-opus", "totally-unknown-provider": "x"}}),
            encoding="utf-8",
        )
        self.assertEqual(list(webui_credentials.read_cli_provider_models()), ["claude"])

    def test_coexists_with_api_key_credentials_in_the_same_file(self):
        webui_credentials.save_credential("claude", "sk-ant-test", "claude-sonnet-5")
        webui_credentials.save_cli_provider_model("codex", "gpt-5-codex")
        self.assertEqual(webui_bridge_run.read_credentials()["claude"]["key"], "sk-ant-test")
        self.assertEqual(webui_credentials.read_cli_provider_models()["codex"], "gpt-5-codex")


class CustomProviderCredentialsTests(unittest.TestCase):
    """Custom providers (DEC-26): a user-named, user-supplied HTTP
    endpoint stored alongside the fixed codex/claude/gemini entries in
    the same credentials.json, under its own "custom_providers" key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_save_then_read_round_trips_all_fields(self):
        webui_credentials.save_custom_provider(
            "openrouter", "sk-or-test", "meta-llama/llama-3", "https://openrouter.ai/api/v1", "openai"
        )
        entry = webui_credentials.read_custom_providers()["openrouter"]
        self.assertEqual(
            entry,
            {"key": "sk-or-test", "model": "meta-llama/llama-3", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"},
        )

    def test_base_url_trailing_slash_is_stripped(self):
        webui_credentials.save_custom_provider("local", "k", "m", "http://localhost:8080/v1/", "openai")
        self.assertEqual(webui_credentials.read_custom_providers()["local"]["base_url"], "http://localhost:8080/v1")

    def test_empty_key_deletes_the_entry(self):
        webui_credentials.save_custom_provider("temp", "k", "m", "https://example.invalid", "openai")
        webui_credentials.delete_custom_provider("temp")
        self.assertNotIn("temp", webui_credentials.read_custom_providers())

    def test_custom_providers_coexist_with_fixed_provider_credentials(self):
        webui_credentials.save_credential("claude", "sk-claude", None)
        webui_credentials.save_custom_provider("groq", "k", "m", "https://api.groq.com/openai/v1", "openai")
        self.assertEqual(webui_bridge_run.read_credentials()["claude"]["key"], "sk-claude")
        self.assertEqual(webui_credentials.read_custom_providers()["groq"]["key"], "k")

    def test_name_colliding_with_a_builtin_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("claude", "k", "m", "https://example.invalid", "openai")

    def test_blank_name_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("   ", "k", "m", "https://example.invalid", "openai")

    def test_name_with_invalid_characters_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("has spaces", "k", "m", "https://example.invalid", "openai")

    def test_unknown_api_format_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("x", "k", "m", "https://example.invalid", "not-a-real-format")

    def test_base_url_without_scheme_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("x", "k", "m", "example.invalid/v1", "openai")

    def test_missing_model_is_rejected(self):
        with self.assertRaises(ValueError):
            webui_credentials.save_custom_provider("x", "k", "", "https://example.invalid", "openai")

    def test_malformed_stored_entry_is_skipped_not_fatal(self):
        self.base_dir.mkdir(parents=True)
        (self.base_dir / "credentials.json").write_text(
            json.dumps(
                {
                    "custom_providers": {
                        "good": {"key": "k", "model": "m", "base_url": "https://x.invalid", "api_format": "openai"},
                        "bad_no_key": {"model": "m", "base_url": "https://x.invalid", "api_format": "openai"},
                        "bad_format": {"key": "k", "model": "m", "base_url": "https://x.invalid", "api_format": "carrier-pigeon"},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(list(webui_credentials.read_custom_providers()), ["good"])

    def test_is_custom_provider_and_id_helpers_round_trip(self):
        provider_id = webui_credentials.custom_provider_id("openrouter")
        self.assertEqual(provider_id, "custom:openrouter")
        self.assertTrue(webui_credentials.is_custom_provider(provider_id))
        self.assertFalse(webui_credentials.is_custom_provider("claude"))
        self.assertEqual(webui_credentials.custom_provider_name(provider_id), "openrouter")


class SharedContextTests(unittest.TestCase):
    """webui_common.read_shared_context()/write_shared_context() (DEC-27):
    the API-key-mode read path for `.handoff/shared-context.md` -- the
    same file handoff_bridge.py's build_prompt() folds into every CLI-
    mode prompt (see BuildPromptSharedContextTests in
    tests/test_handoff_bridge.py for that side)."""

    def test_missing_file_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(webui_common.read_shared_context(Path(tmp)), "")

    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            webui_common.write_shared_context(root, "Never touch legacy/.")
            self.assertEqual(webui_common.read_shared_context(root), "Never touch legacy/.")

    def test_whitespace_only_content_reads_back_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            webui_common.write_shared_context(root, "   \n\n  ")
            self.assertEqual(webui_common.read_shared_context(root), "")

    def test_write_creates_the_handoff_directory_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            webui_common.write_shared_context(root, "hello")
            self.assertTrue((root / ".handoff" / "shared-context.md").exists())


class BuildApiMessageHistoryTests(unittest.TestCase):
    def test_empty_log_produces_just_the_current_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui_common.utc_now()
            messages = webui_api_key_mode.build_api_message_history(root, "hello", now)
            self.assertEqual(messages, [{"role": "user", "content": "hello"}])

    def test_prior_turns_are_replayed_as_alternating_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui_common.utc_now()
            webui_chat_storage.append_chat_message(root, "user", "first question", [], now)
            webui_chat_storage.append_chat_message(root, "agent", "first answer", [], now, provider="claude", status="success", reason="none")
            webui_chat_storage.append_chat_message(root, "user", "second question (bare, no attachments)", [], now)
            messages = webui_api_key_mode.build_api_message_history(root, "second question WITH attachment text", now)
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
            now = webui_common.utc_now()
            webui_chat_storage.append_chat_message(root, "system", "auto-created workspace", [], now)
            webui_chat_storage.append_chat_message(root, "user", "hi", [], now)
            messages = webui_api_key_mode.build_api_message_history(root, "hi again", now)
            # The system message is filtered out entirely, and the trailing
            # bare "user" log entry is dropped in favor of `prompt` (same
            # rule as the prior-turns test) -- leaving just one message.
            self.assertEqual(messages, [{"role": "user", "content": "hi again"}])

    def test_history_is_capped_to_the_max_message_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = webui_common.utc_now()
            for i in range(webui_api_key_mode.API_KEY_MODE_MAX_HISTORY_MESSAGES + 10):
                webui_chat_storage.append_chat_message(root, "user" if i % 2 == 0 else "agent", f"turn {i}", [], now)
            messages = webui_api_key_mode.build_api_message_history(root, "final prompt", now)
            self.assertEqual(len(messages), webui_api_key_mode.API_KEY_MODE_MAX_HISTORY_MESSAGES + 1)  # +1 for the final prompt
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
            now = webui_common.utc_now()
            webui_chat_storage.append_chat_message(root, "user", "do the thing", [], now)
            webui_chat_storage.append_chat_message(root, "agent", "codex gave up", [], now, provider="codex", status="handoff", reason="rate_limit: x")
            webui_chat_storage.append_chat_message(root, "agent", "claude finished it", [], now, provider="claude", status="success", reason="none")
            webui_chat_storage.append_chat_message(root, "user", "thanks, now do the next thing", [], now)
            messages = webui_api_key_mode.build_api_message_history(root, "thanks, now do the next thing (with attachment)", now)
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
            now = webui_common.utc_now()
            webui_chat_storage.append_chat_message(root, "user", "first, unanswered", [], now)
            webui_chat_storage.append_chat_message(root, "user", "second, also unanswered", [], now)
            messages = webui_api_key_mode.build_api_message_history(root, "second, also unanswered (full prompt)", now)
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
            webui_chat_storage.append_chat_message(root, "user", "july question", [], july)
            webui_chat_storage.append_chat_message(root, "agent", "july answer", [], july, provider="claude", status="success", reason="none")
            webui_chat_storage.append_chat_message(root, "user", "august question (bare)", [], august)
            messages = webui_api_key_mode.build_api_message_history(root, "august question (full prompt)", august)
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
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", side_effect=ValueError(raw_exception_text)):
            status, data = webui_api_key_mode._http_post_json(
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
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", return_value=_fake_response(200, {"ok": True})):
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
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
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", return_value=fake):
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertNotEqual(status, 200)
        message = json.dumps(data)
        self.assertNotIn("header", message.lower())

    def test_a_429_is_retried_and_a_later_success_is_returned(self):
        rate_limited = _fake_http_error(429, {"error": {"type": "rate_limit_error", "message": "slow down"}})
        succeeded = _fake_response(200, {"ok": True})
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", side_effect=[rate_limited, succeeded]), mock.patch(
            "webui_api_key_mode._sleep"
        ) as sleep_spy:
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual((status, data), (200, {"ok": True}))
        sleep_spy.assert_called_once()

    def test_retries_are_bounded_then_the_final_error_is_returned(self):
        always_500 = [_fake_http_error(500, {"error": {"type": "api_error", "message": "down"}}) for _ in range(10)]
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", side_effect=always_500), mock.patch(
            "webui_api_key_mode._sleep"
        ) as sleep_spy:
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual(status, 500)
        self.assertEqual(sleep_spy.call_count, webui_api_key_mode.API_KEY_MODE_MAX_RETRIES)

    def test_a_non_retryable_error_is_returned_immediately_without_sleeping(self):
        auth_error = _fake_http_error(401, {"error": {"type": "authentication_error", "message": "bad key"}})
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", side_effect=[auth_error, auth_error]), mock.patch(
            "webui_api_key_mode._sleep"
        ) as sleep_spy:
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual(status, 401)
        sleep_spy.assert_not_called()

    def test_retry_delay_honors_a_numeric_retry_after_header(self):
        rate_limited = _fake_http_error(
            429, {"error": {"type": "rate_limit_error", "message": "slow down"}}, retry_after="7"
        )
        succeeded = _fake_response(200, {"ok": True})
        with mock.patch("webui_api_key_mode.urllib.request.urlopen", side_effect=[rate_limited, succeeded]), mock.patch(
            "webui_api_key_mode._sleep"
        ) as sleep_spy:
            webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        sleep_spy.assert_called_once_with(7.0)

    def test_a_network_error_is_retried_the_same_as_a_5xx(self):
        with mock.patch(
            "webui_api_key_mode.urllib.request.urlopen", side_effect=[urllib.error.URLError("connection reset"), _fake_response(200, {"ok": True})]
        ), mock.patch("webui_api_key_mode._sleep") as sleep_spy:
            status, data = webui_api_key_mode._http_post_json("https://example.invalid", {}, {}, 5)
        self.assertEqual((status, data), (200, {"ok": True}))
        sleep_spy.assert_called_once()


class CallProviderApiTests(unittest.TestCase):
    """A fixture with no tool_use/function_call block (every fixture
    here) exercises the tool loop's zero-iteration case -- one HTTP call,
    same behavior as before CFL-17 added the loop around these
    functions. AgenticLoopTests below covers the actual tool-call path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_anthropic_success_extracts_text(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"content": [{"type": "text", "text": "hi there"}]}),
        ):
            result = webui_api_key_mode.call_anthropic_messages_api(
                "sk-secret", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hi there")

    def test_anthropic_error_response_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
        ):
            result = webui_api_key_mode.call_anthropic_messages_api("sk-super-secret-value", "claude-sonnet-5", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("authentication_error", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_anthropic_network_error_does_not_raise(self):
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui_api_key_mode.call_anthropic_messages_api("sk-secret", "claude-sonnet-5", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("network error", result["message"])
        self.assertNotIn("sk-secret", result["message"])

    def test_anthropic_handles_the_invalid_header_error_tuple_without_raising(self):
        # _http_post_json() converts a header-validation ValueError (see
        # HttpPostJsonTests below) into a plain (0, {"error": ...}) tuple
        # rather than letting it propagate -- this confirms the caller
        # treats that tuple as an ordinary non-200 error, not a crash.
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(0, {"error": {"type": "invalid_request", "message": "request headers were rejected"}}),
        ):
            result = webui_api_key_mode.call_anthropic_messages_api("sk-secret-with-crlf", "claude-sonnet-5", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_request", result["message"])
        self.assertNotIn("sk-secret-with-crlf", result["message"])

    def test_openai_success_extracts_text_from_output_items(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(
                200,
                {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi from openai"}]}]},
            ),
        ):
            result = webui_api_key_mode.call_openai_responses_api(
                "sk-secret", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hi from openai")

    def test_openai_error_response_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(429, {"error": {"type": "rate_limit_error", "message": "too many requests"}}),
        ):
            result = webui_api_key_mode.call_openai_responses_api("sk-super-secret-value", "gpt-5.1-codex", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("rate_limit_error", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_gemini_success_extracts_text_from_candidates(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}]}),
        ):
            result = webui_api_key_mode.call_gemini_api(
                "sk-secret", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hi from gemini")

    def test_gemini_error_response_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid"}}),
        ):
            result = webui_api_key_mode.call_gemini_api("sk-super-secret-value", "gemini-2.5-flash", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_ARGUMENT", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_gemini_network_error_does_not_raise(self):
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui_api_key_mode.call_gemini_api("sk-secret", "gemini-2.5-flash", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("network error", result["message"])
        self.assertNotIn("sk-secret", result["message"])

    def test_gemini_blocked_prompt_is_reported_as_an_error(self):
        # No candidates at all, with a promptFeedback.blockReason -- must
        # not be silently treated as an empty-but-successful reply.
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}),
        ):
            result = webui_api_key_mode.call_gemini_api("sk-secret", "gemini-2.5-flash", [], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("SAFETY", result["message"])


class ValidateProviderApiKeyTests(unittest.TestCase):
    """validate_provider_api_key() -- the real, minimal call POST
    /api/provider-key now makes before ever saving a non-empty key. Unlike
    call_anthropic_messages_api()/call_openai_responses_api(), this never
    runs the tool-use loop -- it has no workspace and no reason to grant
    tool access just to check a key."""

    def test_claude_success_returns_the_reply_text(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"content": [{"type": "text", "text": "ok"}]}),
        ) as spy:
            result = webui_api_key_mode.validate_provider_api_key("claude", "sk-secret", "claude-sonnet-5")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "ok")
        # No tools/tool_choice at all -- a validation ping must never grant
        # tool access, unlike a real chat turn.
        sent_body = spy.call_args.args[2]
        self.assertNotIn("tools", sent_body)
        self.assertNotIn("tool_choice", sent_body)
        self.assertEqual(sent_body["max_tokens"], webui_api_key_mode.API_KEY_VALIDATION_MAX_TOKENS)

    def test_codex_success_returns_the_reply_text(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}),
        ) as spy:
            result = webui_api_key_mode.validate_provider_api_key("codex", "sk-secret", "gpt-5.1-codex")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "ok")
        sent_body = spy.call_args.args[2]
        self.assertNotIn("tools", sent_body)

    def test_invalid_key_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
        ):
            result = webui_api_key_mode.validate_provider_api_key("claude", "sk-super-secret-value", "claude-sonnet-5")
        self.assertFalse(result["ok"])
        self.assertIn("authentication_error", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_network_error_does_not_raise(self):
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui_api_key_mode.validate_provider_api_key("claude", "sk-secret", "claude-sonnet-5")
        self.assertFalse(result["ok"])
        self.assertIn("network error", result["message"])

    def test_empty_reply_still_counts_as_ok(self):
        with mock.patch("webui_api_key_mode._http_post_json", return_value=(200, {"content": []})):
            result = webui_api_key_mode.validate_provider_api_key("claude", "sk-secret", "claude-sonnet-5")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "(empty response)")

    def test_gemini_success_returns_the_reply_text(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
        ) as spy:
            result = webui_api_key_mode.validate_provider_api_key("gemini", "sk-secret", "gemini-2.5-flash")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "ok")
        sent_body = spy.call_args.args[2]
        self.assertNotIn("tools", sent_body)
        self.assertEqual(sent_body["generationConfig"]["maxOutputTokens"], webui_api_key_mode.API_KEY_VALIDATION_MAX_TOKENS)
        # Auth via header, not the `?key=` query-string form -- never put
        # the secret in a URL.
        sent_headers = spy.call_args.args[1]
        self.assertEqual(sent_headers["x-goog-api-key"], "sk-secret")
        self.assertNotIn("key=sk-secret", spy.call_args.args[0])

    def test_gemini_invalid_key_is_reported_and_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid"}}),
        ):
            result = webui_api_key_mode.validate_provider_api_key("gemini", "sk-super-secret-value", "gemini-2.5-flash")
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_ARGUMENT", result["message"])
        self.assertNotIn("sk-super-secret-value", result["message"])

    def test_custom_openai_format_posts_to_chat_completions_under_base_url(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
        ) as spy:
            result = webui_api_key_mode.validate_provider_api_key(
                "openrouter", "sk-or", "meta-llama/llama-3", api_format="openai", base_url="https://openrouter.ai/api/v1"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "ok")
        self.assertEqual(spy.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertNotIn("tools", spy.call_args.args[2])

    def test_custom_anthropic_format_posts_to_the_given_full_url(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"content": [{"type": "text", "text": "ok"}]}),
        ) as spy:
            result = webui_api_key_mode.validate_provider_api_key(
                "proxy", "sk-x", "m", api_format="anthropic", base_url="https://proxy.example/v1/messages"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(spy.call_args.args[0], "https://proxy.example/v1/messages")

    def test_custom_openai_format_invalid_key_never_echoes_the_key(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(401, {"error": {"type": "invalid_api_key", "message": "bad key"}}),
        ):
            result = webui_api_key_mode.validate_provider_api_key(
                "openrouter", "sk-super-secret", "m", api_format="openai", base_url="https://openrouter.ai/api/v1"
            )
        self.assertFalse(result["ok"])
        self.assertNotIn("sk-super-secret", result["message"])


class ToolCallTranscriptTests(unittest.TestCase):
    """_escape_fence()/_tool_call_transcript_block() -- the fenced-
    code-block audit trail DEC-21 relies on for post-hoc visibility in
    place of a per-tool-call confirmation. A review round found that
    tool output/arguments containing their own ``` run could break the
    frontend's fence-matching regex (webui/app.js only recognizes a
    literal ``` delimiter, no longer-fence escape hatch exists), cutting
    the transcript off mid-way."""

    def test_escape_fence_breaks_up_a_triple_backtick_run(self):
        self.assertNotIn("```", webui_api_key_mode._escape_fence("before ``` after"))

    def test_escape_fence_leaves_ordinary_text_untouched(self):
        self.assertEqual(webui_api_key_mode._escape_fence("no backticks here"), "no backticks here")

    def test_escape_fence_handles_longer_backtick_runs_too(self):
        self.assertNotIn("```", webui_api_key_mode._escape_fence("`````"))

    def test_transcript_block_with_embedded_fence_in_result_stays_a_single_block(self):
        block = webui_api_key_mode._tool_call_transcript_block("run_shell", '{"command": "cat f.md"}', "# title\n```python\nprint(1)\n```\n")
        # The whole block must still open with exactly one ``` and close
        # with exactly one ``` -- an embedded fence in the middle must not
        # produce extra fence boundaries the frontend regex would split on.
        self.assertTrue(block.startswith("```\n"))
        self.assertTrue(block.endswith("\n```"))
        inner = block[len("```\n") : -len("\n```")]
        self.assertNotIn("```", inner)

    def test_transcript_block_truncates_large_arguments_not_just_large_results(self):
        # A review round found write_file's `content`/edit_file's
        # `new_string` had no cap on the *arguments* side, even though
        # TOOL_OUTPUT_MAX_CHARS was already applied to tool *results* --
        # a large-but-normal file write would otherwise inflate every
        # subsequent API call's context indefinitely.
        huge_args = '{"path": "big.txt", "content": "' + ("a" * (webui_api_key_mode.TOOL_OUTPUT_MAX_CHARS + 500)) + '"}'
        block = webui_api_key_mode._tool_call_transcript_block("write_file", huge_args, "wrote a lot of characters to big.txt")
        self.assertIn("truncated for transcript", block)
        self.assertLess(len(block), len(huge_args))

    def test_truncate_for_transcript_leaves_short_text_untouched(self):
        self.assertEqual(webui_api_key_mode._truncate_for_transcript("short"), "short")


class ToolDefinitionTests(unittest.TestCase):
    """anthropic_tool_definitions()/openai_tool_definitions()/
    gemini_tool_definitions() are all derived from the single
    _TOOL_SPECS list -- this guards against the three vendor schemas
    drifting out of sync with each other."""

    def test_all_three_vendor_schemas_list_the_same_four_tool_names(self):
        anthropic_names = {t["name"] for t in webui_api_key_mode.anthropic_tool_definitions()}
        openai_names = {t["name"] for t in webui_api_key_mode.openai_tool_definitions()}
        gemini_names = {d["name"] for d in webui_api_key_mode.gemini_tool_definitions()[0]["functionDeclarations"]}
        self.assertEqual(anthropic_names, {"read_file", "write_file", "edit_file", "run_shell"})
        self.assertEqual(anthropic_names, openai_names)
        self.assertEqual(anthropic_names, gemini_names)

    def test_openai_definitions_are_strict_function_tools(self):
        for tool in webui_api_key_mode.openai_tool_definitions():
            self.assertEqual(tool["type"], "function")
            self.assertTrue(tool["strict"])
            self.assertFalse(tool["parameters"]["additionalProperties"])

    def test_anthropic_definitions_use_input_schema_not_parameters(self):
        for tool in webui_api_key_mode.anthropic_tool_definitions():
            self.assertIn("input_schema", tool)
            self.assertNotIn("parameters", tool)
            self.assertEqual(tool["input_schema"]["type"], "object")

    def test_gemini_definitions_are_a_single_tool_with_all_declarations(self):
        # One Tool object holding every functionDeclaration, not one Tool
        # per function -- see gemini_tool_definitions()'s own comment for
        # why (matches the shape confirmed against Google's docs).
        tools = webui_api_key_mode.gemini_tool_definitions()
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(tools[0]["functionDeclarations"]), 4)
        for declaration in tools[0]["functionDeclarations"]:
            self.assertIn("parameters", declaration)
            self.assertNotIn("additionalProperties", declaration["parameters"])
            self.assertNotIn("strict", declaration)


class ToolExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_read_file_returns_contents(self):
        (self.workspace / "a.txt").write_text("hello world", encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "read_file", {"path": "a.txt"})
        self.assertEqual(result, "hello world")

    def test_read_file_missing_path_argument_is_an_error_not_a_crash(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "read_file", {})
        self.assertTrue(result.startswith("error:"))

    def test_read_file_output_over_the_cap_is_truncated_not_silently_cut(self):
        # A review round found this tool bypassed TOOL_OUTPUT_MAX_CHARS
        # entirely -- read_file_preview()'s own MAX_FILE_BYTES cap (~256KB)
        # bounds what's read off disk, not what's fed into the next API
        # call, so a single large file could still blow past the same
        # context/cost budget run_shell's output already respects.
        (self.workspace / "big.txt").write_text("a" * (webui_api_key_mode.TOOL_OUTPUT_MAX_CHARS + 500), encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "read_file", {"path": "big.txt"})
        self.assertIn("(truncated)", result)
        self.assertLessEqual(len(result), webui_api_key_mode.TOOL_OUTPUT_MAX_CHARS + 50)

    def test_read_file_path_escape_is_rejected(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "read_file", {"path": "../outside.txt"})
        self.assertTrue(result.startswith("error:"))

    def test_write_file_creates_a_new_file_including_parent_dirs(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "write_file", {"path": "nested/dir/new.txt", "content": "hi"})
        self.assertIn("wrote", result)
        self.assertEqual((self.workspace / "nested" / "dir" / "new.txt").read_text(encoding="utf-8"), "hi")

    def test_write_file_overwrites_an_existing_file(self):
        (self.workspace / "b.txt").write_text("old", encoding="utf-8")
        webui_api_key_mode.execute_tool_call(self.workspace, "write_file", {"path": "b.txt", "content": "new"})
        self.assertEqual((self.workspace / "b.txt").read_text(encoding="utf-8"), "new")

    def test_write_file_path_escape_is_rejected(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "write_file", {"path": "/etc/passwd", "content": "x"})
        self.assertTrue(result.startswith("error:"))
        self.assertFalse(Path("/etc/passwd_should_never_exist").exists())

    def test_edit_file_replaces_a_unique_match(self):
        (self.workspace / "c.txt").write_text("foo bar baz", encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "edit_file", {"path": "c.txt", "old_string": "bar", "new_string": "qux"})
        self.assertIn("edited", result)
        self.assertEqual((self.workspace / "c.txt").read_text(encoding="utf-8"), "foo qux baz")

    def test_edit_file_zero_matches_is_an_error_and_leaves_the_file_untouched(self):
        (self.workspace / "d.txt").write_text("unchanged", encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "edit_file", {"path": "d.txt", "old_string": "missing", "new_string": "x"})
        self.assertTrue(result.startswith("error:"))
        self.assertEqual((self.workspace / "d.txt").read_text(encoding="utf-8"), "unchanged")

    def test_edit_file_ambiguous_match_is_rejected_not_guessed(self):
        (self.workspace / "e.txt").write_text("dup dup", encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "edit_file", {"path": "e.txt", "old_string": "dup", "new_string": "x"})
        self.assertTrue(result.startswith("error:"))
        self.assertEqual((self.workspace / "e.txt").read_text(encoding="utf-8"), "dup dup")

    def test_edit_file_nonexistent_file_is_an_error(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "edit_file", {"path": "nope.txt", "old_string": "a", "new_string": "b"})
        self.assertTrue(result.startswith("error:"))

    def test_run_shell_returns_exit_code_and_output(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "echo hello-from-shell-tool"})
        self.assertIn("exit code: 0", result)
        self.assertIn("hello-from-shell-tool", result)

    def test_run_shell_runs_in_the_workspace_directory(self):
        (self.workspace / "marker.txt").write_text("x", encoding="utf-8")
        result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "ls"})
        self.assertIn("marker.txt", result)

    def test_run_shell_nonzero_exit_is_reported_not_treated_as_a_tool_error(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "exit 3"})
        self.assertIn("exit code: 3", result)

    def test_run_shell_timeout_is_reported_as_an_error_string(self):
        mock_process = mock.MagicMock()
        # pid value itself is unused now that _kill_process_tree is fully
        # mocked below (it never reads .pid); left as a real-looking
        # placeholder for consistency with the other mock_process fixtures
        # in this class.
        mock_process.pid = 999999999
        mock_process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="sleep 999", timeout=webui_api_key_mode.TOOL_EXEC_TIMEOUT_SECONDS),
            ("", ""),  # the reap call after _kill_process_tree()
        ]
        # _kill_process_tree itself is mocked out here -- this test only
        # cares about the reported error string, and the function's own
        # kill mechanics are already covered by two sibling tests in this
        # class: test_run_shell_timeout_kills_the_whole_process_group_not_
        # just_the_shell (POSIX) and test_kill_process_tree_uses_taskkill_
        # on_windows (Windows). Without this, Windows's taskkill branch
        # calls the real subprocess.run(), which internally calls Popen()
        # too -- since mock.patch below patches the shared subprocess
        # module's Popen attribute (not a copy scoped to this test), that
        # inner call is silently redirected to mock_process as well,
        # exhausting its 2-item side_effect list and corrupting the
        # unrelated taskkill call with a stray empty-tuple unpack.
        with mock.patch("webui_api_key_mode.subprocess.Popen", return_value=mock_process), mock.patch(
            "webui_api_key_mode._kill_process_tree"
        ):
            result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "sleep 999"})
        self.assertTrue(result.startswith("error:"))
        self.assertIn("timed out", result)

    @unittest.skipIf(sys.platform == "win32", "asserts os.killpg was called; on Windows _kill_process_tree takes the taskkill branch instead")
    def test_run_shell_timeout_kills_the_whole_process_group_not_just_the_shell(self):
        mock_process = mock.MagicMock()
        mock_process.pid = 999999999
        mock_process.communicate.side_effect = subprocess.TimeoutExpired(cmd="sleep 999", timeout=1)
        with mock.patch("webui_api_key_mode.subprocess.Popen", return_value=mock_process), mock.patch(
            "webui_api_key_mode.os.killpg"
        ) as killpg_spy:
            webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "sleep 999"})
        # The audit finding this covers: the old implementation only ever
        # called process.kill() on the immediate shell -- os.killpg() (the
        # whole process group a backgrounded/forked descendant also
        # belongs to, since the process was started with
        # start_new_session=True) must actually be reached. Uses
        # process.pid directly as the pgid (not a separate os.getpgid()
        # lookup -- verified flaky once the shell itself has already
        # exited, see _kill_process_tree's own comment).
        killpg_spy.assert_called_once_with(999999999, signal.SIGKILL)
        mock_process.kill.assert_called_once()

    @unittest.skipIf(
        sys.platform == "win32",
        "process-group kill via os.killpg is POSIX-specific; the Windows taskkill branch is covered separately (mocked), not live here",
    )
    def test_run_shell_timeout_actually_stops_a_backgrounded_descendant(self):
        # A real, unmocked integration test: the outer `sh -c` returns
        # almost immediately (it backgrounds a loop and exits), but that
        # backgrounded loop -- which inherits the same stdout/stderr pipe
        # this tool reads from -- would otherwise keep running and writing
        # to `marker` for ~5s regardless of the tool's own timeout, unless
        # the whole process group is actually killed, not just the shell.
        marker = self.workspace / "still-alive.txt"
        command = f"(for i in $(seq 1 50); do echo alive >> {marker}; sleep 0.1; done &) ; exit 0"
        with mock.patch.object(webui_api_key_mode, "TOOL_EXEC_TIMEOUT_SECONDS", 0.3):
            result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": command})
        self.assertIn("timed out", result)
        count_at_timeout = len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0
        time.sleep(1.0)  # the unfixed bug would accumulate many more lines here
        count_after_wait = len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0
        self.assertEqual(count_after_wait, count_at_timeout)

    def test_kill_process_tree_uses_taskkill_on_windows(self):
        mock_process = mock.MagicMock()
        mock_process.pid = 4242
        with mock.patch.object(webui_api_key_mode.sys, "platform", "win32"), mock.patch(
            "webui_api_key_mode.subprocess.run"
        ) as run_spy:
            webui_api_key_mode._kill_process_tree(mock_process)
        run_spy.assert_called_once_with(["taskkill", "/F", "/T", "/PID", "4242"], capture_output=True)

    def test_run_shell_output_over_the_cap_is_truncated_not_silently_cut(self):
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = ("a" * (webui_api_key_mode.TOOL_OUTPUT_MAX_CHARS + 500), "")
        mock_process.returncode = 0
        with mock.patch("webui_api_key_mode.subprocess.Popen", return_value=mock_process):
            result = webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "x"})
        self.assertIn("(output truncated)", result)
        self.assertLessEqual(len(result), webui_api_key_mode.TOOL_OUTPUT_MAX_CHARS + 200)

    def test_run_shell_pins_utf8_encoding(self):
        # Regression coverage for a real crash class (2026-08-14, see
        # handoff_bridge.py's run_provider() fix): without an explicit
        # encoding, subprocess falls back to
        # locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
        # Windows machine -- to decode this command's stdout/stderr, and a
        # model-issued shell command (or the files it reads) can easily
        # produce non-ASCII output.
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = ("ok", "")
        mock_process.returncode = 0
        with mock.patch("webui_api_key_mode.subprocess.Popen", return_value=mock_process) as popen_spy:
            webui_api_key_mode.execute_tool_call(self.workspace, "run_shell", {"command": "echo ok"})
        self.assertEqual(popen_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen_spy.call_args.kwargs["errors"], "replace")

    def test_unknown_tool_name_is_an_error_not_a_crash(self):
        result = webui_api_key_mode.execute_tool_call(self.workspace, "delete_everything", {})
        self.assertTrue(result.startswith("error:"))

    def test_an_exception_inside_an_executor_is_caught_not_propagated(self):
        # _TOOL_EXECUTORS binds the function object at import time, so
        # patching handoff_webui._tool_read_file by name wouldn't affect
        # the dispatcher's already-stored reference -- the dict entry
        # itself has to be swapped.
        with mock.patch.dict(
            "webui_api_key_mode._TOOL_EXECUTORS", {"read_file": mock.Mock(side_effect=RuntimeError("boom"))}
        ):
            result = webui_api_key_mode.execute_tool_call(self.workspace, "read_file", {"path": "a.txt"})
        self.assertTrue(result.startswith("error:"))
        self.assertIn("boom", result)


class AgenticLoopTests(unittest.TestCase):
    """call_anthropic_messages_api()/call_openai_responses_api()'s tool
    loop (CFL-17/DEC-21) -- execute_tool_call() itself is mocked here so
    these tests isolate the loop's control flow (when to call again, how
    results feed back, the iteration bound) from the tool executors,
    which ToolExecutorTests already covers directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_anthropic_executes_a_tool_call_then_returns_the_final_text(self):
        tool_use_response = (
            200,
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
                ]
            },
        )
        final_response = (200, {"content": [{"type": "text", "text": "the file says hi"}]})
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=[tool_use_response, final_response]), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="hi"
        ) as exec_spy:
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "read a.txt"}], self.workspace)
        self.assertTrue(result["ok"])
        exec_spy.assert_called_once_with(self.workspace, "read_file", {"path": "a.txt"})
        self.assertIn("the file says hi", result["text"])
        self.assertIn("read_file", result["text"])

    def test_anthropic_stops_after_max_tool_iterations(self):
        always_calls_tool = (
            200,
            {"content": [{"type": "tool_use", "id": "toolu_x", "name": "run_shell", "input": {"command": "echo hi"}}]},
        )
        with mock.patch("webui_api_key_mode._http_post_json", return_value=always_calls_tool), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="ok"
        ) as exec_spy:
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "loop forever"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, webui_api_key_mode.MAX_TOOL_ITERATIONS)
        self.assertIn(f"stopped after {webui_api_key_mode.MAX_TOOL_ITERATIONS}", result["text"])

    def test_anthropic_feeds_tool_result_back_with_the_matching_tool_use_id(self):
        tool_use_response = (
            200,
            {"content": [{"type": "tool_use", "id": "toolu_abc", "name": "read_file", "input": {"path": "a.txt"}}]},
        )
        final_response = (200, {"content": [{"type": "text", "text": "done"}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[tool_use_response, final_response]
        ) as http_spy, mock.patch("webui_api_key_mode.execute_tool_call", return_value="file contents"):
            webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        second_call_body = http_spy.call_args_list[1].args[2]
        tool_result_message = second_call_body["messages"][-1]
        self.assertEqual(tool_result_message["role"], "user")
        self.assertEqual(tool_result_message["content"][0]["type"], "tool_result")
        self.assertEqual(tool_result_message["content"][0]["tool_use_id"], "toolu_abc")
        self.assertEqual(tool_result_message["content"][0]["content"], "file contents")

    def test_anthropic_defensively_executes_every_tool_use_block_even_if_the_api_ever_ignores_disable_parallel(self):
        # disable_parallel_tool_use is a hint, not an API guarantee this
        # code controls -- if a response ever carries more than one
        # tool_use block anyway, every one of them still needs an
        # executed result and a matching tool_result, or the next call
        # would 400 on a mismatched-tool-result-id error. A self-review
        # round found the original implementation only executed
        # tool_use_blocks[0] and silently dropped the rest.
        two_tool_use_response = (
            200,
            {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
                    {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b.txt"}},
                ]
            },
        )
        final_response = (200, {"content": [{"type": "text", "text": "done"}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[two_tool_use_response, final_response]
        ) as http_spy, mock.patch("webui_api_key_mode.execute_tool_call", return_value="ok") as exec_spy:
            webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertEqual(exec_spy.call_count, 2)
        second_call_body = http_spy.call_args_list[1].args[2]
        tool_result_message = second_call_body["messages"][-1]
        self.assertEqual(len(tool_result_message["content"]), 2)
        self.assertEqual({b["tool_use_id"] for b in tool_result_message["content"]}, {"toolu_1", "toolu_2"})

    def test_anthropic_max_iterations_bounds_executions_even_across_a_multi_block_response(self):
        batch_response = (
            200,
            {
                "content": [
                    {"type": "tool_use", "id": f"toolu_{i}", "name": "run_shell", "input": {"command": "echo hi"}}
                    for i in range(webui_api_key_mode.MAX_TOOL_ITERATIONS + 10)
                ]
            },
        )
        with mock.patch("webui_api_key_mode._http_post_json", return_value=batch_response), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="ok"
        ) as exec_spy:
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, webui_api_key_mode.MAX_TOOL_ITERATIONS)
        self.assertIn(f"stopped after {webui_api_key_mode.MAX_TOOL_ITERATIONS}", result["text"])

    def test_anthropic_no_tool_use_returns_on_the_first_call(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(200, {"content": [{"type": "text", "text": "just chatting"}]})
        ) as http_spy:
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertEqual(http_spy.call_count, 1)
        self.assertEqual(result["text"], "just chatting")

    def test_anthropic_a_tool_that_already_ran_is_not_lost_when_the_next_call_fails(self):
        # A review round found that a tool with real side effects
        # (write_file/edit_file/run_shell) executing on one iteration,
        # followed by the *next* API call failing, used to discard the
        # record of what already ran along with the failed call --
        # DEC-21's whole rationale for skipping a per-tool-call
        # confirmation depends on that record staying visible.
        tool_use_response = (
            200,
            {"content": [{"type": "tool_use", "id": "toolu_1", "name": "run_shell", "input": {"command": "rm important.txt"}}]},
        )
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[tool_use_response, urllib.error.URLError("boom")]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="deleted"):
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("run_shell", result["message"])
        self.assertIn("deleted", result["message"])
        self.assertIn("network error", result["message"])

    def test_anthropic_error_before_any_tool_runs_has_no_transcript_prefix(self):
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui_api_key_mode.call_anthropic_messages_api("sk-x", "claude-sonnet-5", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertFalse(result["ok"])
        self.assertTrue(result["message"].startswith("network error calling Anthropic Messages API:"))

    def test_openai_a_tool_that_already_ran_is_not_lost_when_the_next_call_fails(self):
        function_call_response = (
            200,
            {"output": [{"type": "function_call", "call_id": "call_1", "name": "run_shell", "arguments": '{"command": "rm important.txt"}'}]},
        )
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[function_call_response, urllib.error.URLError("boom")]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="deleted"):
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("run_shell", result["message"])
        self.assertIn("deleted", result["message"])
        self.assertIn("network error", result["message"])

    def test_openai_executes_a_function_call_then_returns_the_final_text(self):
        function_call_response = (
            200,
            {"output": [{"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path": "a.txt"}'}]},
        )
        final_response = (
            200,
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": "the file says hi"}]}]},
        )
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[function_call_response, final_response]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="hi") as exec_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "read a.txt"}], self.workspace)
        self.assertTrue(result["ok"])
        exec_spy.assert_called_once_with(self.workspace, "read_file", {"path": "a.txt"})
        self.assertIn("the file says hi", result["text"])

    def test_openai_executes_multiple_function_calls_in_one_output(self):
        two_calls_response = (
            200,
            {
                "output": [
                    {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path": "a.txt"}'},
                    {"type": "function_call", "call_id": "call_2", "name": "read_file", "arguments": '{"path": "b.txt"}'},
                ]
            },
        )
        final_response = (200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[two_calls_response, final_response]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="ok") as exec_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, 2)

    def test_openai_stops_after_max_tool_iterations(self):
        always_calls_function = (
            200,
            {"output": [{"type": "function_call", "call_id": "call_x", "name": "run_shell", "arguments": '{"command": "echo hi"}'}]},
        )
        with mock.patch("webui_api_key_mode._http_post_json", return_value=always_calls_function), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="ok"
        ) as exec_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "loop forever"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, webui_api_key_mode.MAX_TOOL_ITERATIONS)
        self.assertIn(f"stopped after {webui_api_key_mode.MAX_TOOL_ITERATIONS}", result["text"])

    def test_openai_max_iterations_bounds_executions_not_just_http_round_trips(self):
        # A self-review round found that bounding the outer HTTP-call loop
        # alone doesn't bound cost: OpenAI's output array can legitimately
        # carry more than one function_call per response, so a single
        # response batching many more than MAX_TOOL_ITERATIONS calls must
        # still stop exactly at the bound, not execute the whole batch.
        batch_response = (
            200,
            {
                "output": [
                    {"type": "function_call", "call_id": f"call_{i}", "name": "run_shell", "arguments": '{"command": "echo hi"}'}
                    for i in range(webui_api_key_mode.MAX_TOOL_ITERATIONS + 10)
                ]
            },
        )
        with mock.patch("webui_api_key_mode._http_post_json", return_value=batch_response), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="ok"
        ) as exec_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, webui_api_key_mode.MAX_TOOL_ITERATIONS)
        self.assertIn(f"stopped after {webui_api_key_mode.MAX_TOOL_ITERATIONS}", result["text"])

    def test_openai_malformed_arguments_json_does_not_crash_the_loop(self):
        bad_args_response = (
            200,
            {"output": [{"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{not valid json"}]},
        )
        final_response = (200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[bad_args_response, final_response]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="ok") as exec_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        exec_spy.assert_called_once_with(self.workspace, "read_file", {})

    def test_openai_no_function_call_returns_on_the_first_call(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "just chatting"}]}]}),
        ) as http_spy:
            result = webui_api_key_mode.call_openai_responses_api("sk-x", "gpt-5.1-codex", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertEqual(http_spy.call_count, 1)
        self.assertEqual(result["text"], "just chatting")

    def test_gemini_executes_a_function_call_then_returns_the_final_text(self):
        function_call_response = (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.txt"}, "id": "call-1"}}]
                        }
                    }
                ]
            },
        )
        final_response = (200, {"candidates": [{"content": {"parts": [{"text": "the file says hi"}]}}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[function_call_response, final_response]
        ) as post_spy, mock.patch("webui_api_key_mode.execute_tool_call", return_value="hi") as exec_spy:
            result = webui_api_key_mode.call_gemini_api(
                "sk-x", "gemini-2.5-flash", [{"role": "user", "content": "read a.txt"}], self.workspace
            )
        self.assertTrue(result["ok"])
        exec_spy.assert_called_once_with(self.workspace, "read_file", {"path": "a.txt"})
        self.assertIn("the file says hi", result["text"])
        # The second call's request body must carry the functionResponse
        # back, wrapped as a JSON object (not the bare tool-result
        # string, execute_tool_call()'s own return shape) and echoing
        # the same call id.
        second_call_body = post_spy.call_args_list[1].args[2]
        function_response_part = second_call_body["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(function_response_part["response"], {"result": "hi"})
        self.assertEqual(function_response_part["id"], "call-1")

    def test_gemini_defensively_executes_every_function_call_even_if_the_api_ever_returns_more_than_one(self):
        two_calls_response = (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "read_file", "args": {"path": "a.txt"}}},
                                {"functionCall": {"name": "read_file", "args": {"path": "b.txt"}}},
                            ]
                        }
                    }
                ]
            },
        )
        final_response = (200, {"candidates": [{"content": {"parts": [{"text": "done"}]}}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[two_calls_response, final_response]
        ), mock.patch("webui_api_key_mode.execute_tool_call", return_value="ok") as exec_spy:
            result = webui_api_key_mode.call_gemini_api("sk-x", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, 2)

    def test_gemini_max_iterations_bounds_executions_even_across_a_multi_part_response(self):
        batch_response = (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "run_shell", "args": {"command": "echo hi"}}}
                                for _ in range(webui_api_key_mode.MAX_TOOL_ITERATIONS + 10)
                            ]
                        }
                    }
                ]
            },
        )
        with mock.patch("webui_api_key_mode._http_post_json", return_value=batch_response), mock.patch(
            "webui_api_key_mode.execute_tool_call", return_value="ok"
        ) as exec_spy:
            result = webui_api_key_mode.call_gemini_api("sk-x", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertTrue(result["ok"])
        self.assertEqual(exec_spy.call_count, webui_api_key_mode.MAX_TOOL_ITERATIONS)
        self.assertIn(f"stopped after {webui_api_key_mode.MAX_TOOL_ITERATIONS}", result["text"])

    def test_gemini_no_function_call_returns_on_the_first_call(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"candidates": [{"content": {"parts": [{"text": "just chatting"}]}}]}),
        ) as http_spy:
            result = webui_api_key_mode.call_gemini_api("sk-x", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], self.workspace)
        self.assertEqual(http_spy.call_count, 1)
        self.assertEqual(result["text"], "just chatting")

    def test_gemini_function_call_without_an_id_does_not_echo_a_fabricated_one(self):
        # Some models/responses omit "id" on the functionCall (confirmed:
        # only "always returned... for Gemini 3 models") -- the
        # functionResponse sent back must not invent one.
        no_id_response = (
            200,
            {"candidates": [{"content": {"parts": [{"functionCall": {"name": "run_shell", "args": {"command": "echo hi"}}}]}}]},
        )
        final_response = (200, {"candidates": [{"content": {"parts": [{"text": "done"}]}}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[no_id_response, final_response]
        ) as post_spy, mock.patch("webui_api_key_mode.execute_tool_call", return_value="ok"):
            webui_api_key_mode.call_gemini_api("sk-x", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], self.workspace)
        second_call_body = post_spy.call_args_list[1].args[2]
        function_response_part = second_call_body["contents"][-1]["parts"][0]["functionResponse"]
        self.assertNotIn("id", function_response_part)


class CustomOpenAiCompatibleApiTests(unittest.TestCase):
    """call_openai_compatible_chat_api() (DEC-26): custom providers'
    OpenAI-compatible (Chat Completions, not Responses API) caller. Same
    control-flow coverage style as AgenticLoopTests above, minus the
    cases already proven identical there (max-iterations bound, every-
    tool-call-in-a-batch-executed defensiveness) -- this focuses on what's
    actually different about Chat Completions' shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_posts_to_chat_completions_under_the_given_base_url(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
        ) as http_spy:
            webui_api_key_mode.call_openai_compatible_chat_api(
                "https://openrouter.ai/api/v1", "sk-or", "meta-llama/llama-3", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertEqual(http_spy.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")

    def test_no_tool_calls_returns_the_plain_content_on_the_first_call(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "just chatting"}}]}),
        ) as http_spy:
            result = webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertEqual(http_spy.call_count, 1)
        self.assertEqual(result["text"], "just chatting")

    def test_system_prompt_is_prepended_as_a_system_role_message(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        ) as http_spy:
            webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace, system="be terse"
            )
        sent_messages = http_spy.call_args.args[2]["messages"]
        self.assertEqual(sent_messages[0], {"role": "system", "content": "be terse"})

    def test_no_system_prompt_does_not_add_a_system_message(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
        ) as http_spy:
            webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace
            )
        sent_messages = http_spy.call_args.args[2]["messages"]
        self.assertEqual([m["role"] for m in sent_messages], ["user"])

    def test_executes_a_tool_call_then_feeds_the_result_back_as_a_tool_role_message(self):
        tool_call_response = (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
                            ],
                        }
                    }
                ]
            },
        )
        final_response = (200, {"choices": [{"message": {"role": "assistant", "content": "the file says hi"}}]})
        with mock.patch(
            "webui_api_key_mode._http_post_json", side_effect=[tool_call_response, final_response]
        ) as http_spy, mock.patch("webui_api_key_mode.execute_tool_call", return_value="hi") as exec_spy:
            result = webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "read a.txt"}], self.workspace
            )
        exec_spy.assert_called_once_with(self.workspace, "read_file", {"path": "a.txt"})
        self.assertIn("the file says hi", result["text"])
        second_call_messages = http_spy.call_args_list[1].args[2]["messages"]
        tool_result_message = second_call_messages[-1]
        self.assertEqual(tool_result_message, {"role": "tool", "tool_call_id": "call_1", "content": "hi"})

    def test_server_that_ignores_tools_and_replies_in_plain_text_is_not_treated_as_an_error(self):
        # Not every custom endpoint supports tool calls -- one that just
        # ignores the `tools` field in the request and replies normally
        # must be handled the same as "no tool_calls in the response",
        # not surfaced as a failure.
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "sure, here you go"}}]}),
        ):
            result = webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "sure, here you go")

    def test_network_error_is_reported_not_raised(self):
        with mock.patch("webui_api_key_mode._http_post_json", side_effect=urllib.error.URLError("boom")):
            result = webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertFalse(result["ok"])
        self.assertNotIn("sk-x", result["message"])

    def test_api_error_status_is_reported_with_the_error_body(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(401, {"error": {"type": "invalid_api_key", "message": "bad key"}})
        ):
            result = webui_api_key_mode.call_openai_compatible_chat_api(
                "https://x.invalid", "sk-x", "m", [{"role": "user", "content": "hi"}], self.workspace
            )
        self.assertFalse(result["ok"])
        self.assertIn("bad key", result["message"])


class RunProviderViaApiKeyTests(unittest.TestCase):
    def test_success_produces_a_record_with_no_session_or_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "hello back"}):
                records = webui_api_key_mode.run_provider_via_api_key(
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
                "webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": False, "message": "401 authentication_error"}
            ) as spy:
                records = webui_api_key_mode.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue"
                )
        spy.assert_called_once()  # confirms this exercised the real API-failure path, not the no-model-configured one
        record = records[0]
        self.assertTrue(record["reason"].startswith("tool_failure"))
        self.assertEqual(
            webui_bridge_run.classify_run_status(record["handoff_needed"], record["reason"]), "fail"
        )

    def test_claude_with_no_model_configured_and_no_default_errors_without_calling_the_api(self):
        # Neither provider has a built-in default model (see
        # API_KEY_MODE_DEFAULT_MODELS' own comment on why Claude's was
        # deliberately removed) -- this mirrors the codex test below for
        # symmetry.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api") as spy:
                records = webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": None}, "continue")
        spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("model", records[0]["final_text"])

    def test_codex_with_no_model_configured_and_no_default_errors_without_calling_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_openai_responses_api") as spy:
                records = webui_api_key_mode.run_provider_via_api_key(root, "codex", "hello", {"key": "sk-x", "model": None}, "continue")
        spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("model", records[0]["final_text"])

    def test_codex_with_a_saved_model_calls_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_openai_responses_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui_api_key_mode.run_provider_via_api_key(
                    root, "codex", "hello", {"key": "sk-x", "model": "gpt-5.1-codex"}, "continue"
                )
        spy.assert_called_once()
        self.assertEqual(records[0]["model"], "gpt-5.1-codex")

    def test_gemini_with_a_saved_model_calls_the_api(self):
        # DEC-25: gemini is dispatched via call_gemini_api(), the same as
        # claude/codex go through their own caller.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_gemini_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui_api_key_mode.run_provider_via_api_key(
                    root, "gemini", "hello", {"key": "sk-x", "model": "gemini-2.5-flash"}, "continue"
                )
        spy.assert_called_once()
        self.assertEqual(records[0]["model"], "gemini-2.5-flash")

    def test_gemini_with_no_model_configured_and_no_default_errors_without_calling_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_gemini_api") as spy:
                records = webui_api_key_mode.run_provider_via_api_key(root, "gemini", "hello", {"key": "sk-x", "model": None}, "continue")
        spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("model", records[0]["final_text"])

    def test_model_override_takes_priority_over_the_saved_credential_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui_api_key_mode.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-old"}, "continue", model_override="claude-new"
                )
        self.assertEqual(records[0]["model"], "claude-new")
        self.assertEqual(spy.call_args.args[1], "claude-new")

    def test_shared_context_is_threaded_into_the_caller_as_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".handoff").mkdir()
            (root / ".handoff" / "shared-context.md").write_text("Never touch legacy/.", encoding="utf-8")
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "ok"}) as spy:
                webui_api_key_mode.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue"
                )
        self.assertEqual(spy.call_args.kwargs.get("system"), "Never touch legacy/.")

    def test_missing_shared_context_file_passes_empty_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "ok"}) as spy:
                webui_api_key_mode.run_provider_via_api_key(
                    root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue"
                )
        self.assertEqual(spy.call_args.kwargs.get("system"), "")

    def test_custom_openai_format_provider_dispatches_to_the_chat_completions_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = {"key": "sk-or", "model": "meta-llama/llama-3", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"}
            with mock.patch("webui_api_key_mode.call_openai_compatible_chat_api", return_value={"ok": True, "text": "ok"}) as spy:
                records = webui_api_key_mode.run_provider_via_api_key(root, "custom:openrouter", "hello", credential, "continue")
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0], "https://openrouter.ai/api/v1")
        self.assertEqual(records[0]["provider"], "custom:openrouter")
        self.assertEqual(records[0]["model"], "meta-llama/llama-3")

    def test_custom_anthropic_format_provider_dispatches_to_the_messages_caller_with_its_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = {"key": "sk-x", "model": "some-model", "base_url": "https://proxy.example/v1/messages", "api_format": "anthropic"}
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "ok"}) as spy:
                webui_api_key_mode.run_provider_via_api_key(root, "custom:proxy", "hello", credential, "continue")
        spy.assert_called_once()
        self.assertEqual(spy.call_args.kwargs.get("base_url"), "https://proxy.example/v1/messages")

    def test_custom_provider_with_unknown_api_format_errors_without_calling_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = {"key": "sk-x", "model": "m", "base_url": "https://x.invalid", "api_format": "carrier-pigeon"}
            with mock.patch("webui_api_key_mode.call_openai_compatible_chat_api") as openai_spy, mock.patch(
                "webui_api_key_mode.call_anthropic_messages_api"
            ) as anthropic_spy:
                records = webui_api_key_mode.run_provider_via_api_key(root, "custom:bad", "hello", credential, "continue")
        openai_spy.assert_not_called()
        anthropic_spy.assert_not_called()
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))


class ApiKeyModeCurrentMdContinuityTests(unittest.TestCase):
    """Covers the audit finding (F2, 2026-09-02) that run_provider_via_api_key()
    never touched .handoff/current.md -- this project's actual cross-provider
    continuity document -- even though CLI mode appends to it after every
    run. A later CLI/mobile handoff used to see whatever files an
    API-key-mode turn's tool loop (CFL-17/DEC-21) had changed, with no
    durable record of what happened or why.
    """

    def _current_md_text(self, root: Path) -> str:
        path = root / ".handoff" / "current.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_successful_turn_appends_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "did the thing"}):
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue")
            text = self._current_md_text(root)
        self.assertIn("## API-Key-Mode Turn", text)
        self.assertIn("- Provider: claude", text)
        self.assertIn("- Model: claude-sonnet-5", text)
        self.assertIn("- Exit code: 0", text)
        self.assertIn("- Handoff needed: False", text)
        self.assertIn("did the thing", text)

    def test_failed_turn_also_appends_a_record(self):
        # A mid-turn API failure can still follow tool calls that already
        # had real side effects (_error_with_transcript()'s own docstring)
        # -- the record of that must reach current.md too, not just
        # successful turns.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": False, "message": "401 authentication_error"}
            ):
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue")
            text = self._current_md_text(root)
        self.assertIn("## API-Key-Mode Turn", text)
        self.assertIn("- Handoff needed: True", text)
        self.assertIn("authentication_error", text)

    def test_no_model_configured_preflight_error_does_not_touch_current_md(self):
        # No real attempt against the provider ever happened here -- unlike
        # the two cases above, there is nothing to durably record.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api") as spy:
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": None}, "continue")
            spy.assert_not_called()
            self.assertFalse((root / ".handoff" / "current.md").exists())

    def test_two_turns_append_two_separate_sections_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "first"}):
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue")
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "second"}):
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hi again", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue")
            text = self._current_md_text(root)
        self.assertEqual(text.count("## API-Key-Mode Turn"), 2)
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_a_concurrent_cli_mode_append_current_call_is_preserved_not_clobbered(self):
        # Both this function and handoff_bridge.append_current() must
        # contend for the *same* lock (handoff_dir / ".write.lock") --
        # otherwise one could read-then-clobber the other's write in the
        # classic lost-update pattern this project's WriteLock exists to
        # prevent (see scripts/handoff_hook.py's own docstring for the
        # identical concern between the hook and a bridge subprocess).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "api-key-mode turn"}):
                webui_api_key_mode.run_provider_via_api_key(root, "claude", "hello", {"key": "sk-x", "model": "claude-sonnet-5"}, "continue")
            original_cwd = Path.cwd()
            os.chdir(root)
            try:
                hb.append_current(
                    {
                        "started_at": "2026-01-01T00:00:00",
                        "provider": "codex",
                        "exit_code": 0,
                        "handoff_needed": False,
                        "reason": "none",
                    }
                )
            finally:
                os.chdir(original_cwd)
            text = self._current_md_text(root)
        self.assertIn("## API-Key-Mode Turn", text)
        self.assertIn("## Run 2026-01-01T00:00:00", text)


class ProviderDispatchTests(FakeProviderPathMixin, unittest.TestCase):
    """Covers _run_provider_via_bridge_locked()'s Phase 4 branch -- the
    part of the dispatch logic RunProviderViaBridgeTests (which always has
    a fake CLI on PATH) can't exercise: a provider whose CLI is genuinely
    absent."""

    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    def test_cli_available_provider_uses_subprocess_even_if_a_key_is_also_saved(self):
        # A saved key must never override an available CLI -- DEC-16.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        webui_credentials.save_credential("codex", "sk-should-be-unused", None)
        with mock.patch("webui_api_key_mode.call_openai_responses_api") as spy:
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "codex", "hello", None, "continue")
        spy.assert_not_called()
        self.assertEqual(records[0]["session_id"], "fake-codex-session")

    def test_cli_available_provider_never_reads_credentials_file(self):
        # Regression test: _run_provider_via_bridge_locked() used to call
        # read_credentials() (a disk read + JSON parse) unconditionally at
        # the top of the function, even though the result is only ever
        # consulted inside the CLI-unavailable branches below. In the
        # common case -- CLI installed, as here -- that read happened on
        # every single /api/run request for a value that was discarded
        # unused. It must now be skipped entirely.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        webui_credentials.save_credential("codex", "sk-should-be-unused", None)
        with mock.patch("webui_bridge_run.read_credentials", wraps=webui_bridge_run.read_credentials) as spy:
            webui_bridge_run.run_provider_via_bridge(self.workspace, "codex", "hello", None, "continue")
        spy.assert_not_called()

    def test_auto_with_a_cli_available_never_reads_credentials_file(self):
        # Same efficiency invariant as above, for the "auto" branch: with at
        # least one CLI available, credentials.json must not be read at all.
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
        webui_credentials.save_credential("claude", "sk-should-be-unused", "claude-sonnet-5")
        with mock.patch("webui_bridge_run.read_credentials", wraps=webui_bridge_run.read_credentials) as spy:
            webui_bridge_run.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        spy.assert_not_called()

    def test_cli_missing_provider_with_a_saved_key_uses_the_api_path(self):
        webui_credentials.save_credential("claude", "sk-x", "claude-sonnet-5")
        with mock.patch("webui_credentials.shutil.which", return_value=None), mock.patch(
            "webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "api reply"}
        ) as spy:
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "claude", "hello", None, "continue")
        spy.assert_called_once()
        self.assertEqual(records[0]["final_text"], "api reply")
        self.assertIsNone(records[0]["run_dir"])

    def test_cli_missing_provider_with_no_saved_key_behaves_exactly_as_before_this_phase(self):
        # No fake binary for "claude" and no credential saved -- this falls
        # through to the pre-existing subprocess path unchanged, which
        # spawns a real handoff_bridge.py child process. That child inherits
        # this process's PATH, so mock.patch("webui_credentials.shutil.which")
        # alone would NOT be enough here (it only affects this parent
        # process's own dispatch check) -- if a real `claude` CLI happens to
        # be installed on this machine's PATH, the child could actually
        # invoke it. PATH is replaced outright (not just prepended, like
        # setUpFakeProviders() does) so the child genuinely cannot find one
        # either, and handoff_bridge.py's own FileNotFoundError -> exit_code
        # 127 handling is what's actually being exercised here.
        with mock.patch.dict(os.environ, {"PATH": str(self.fake_bin)}):
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "claude", "hello", None, "continue")
        self.assertEqual(records[0]["exit_code"], 127)

    def test_auto_with_no_cli_at_all_falls_back_to_a_provider_with_a_saved_key(self):
        webui_credentials.save_credential("claude", "sk-x", "claude-sonnet-5")
        with mock.patch("webui_credentials.shutil.which", return_value=None), mock.patch(
            "webui_api_key_mode.call_anthropic_messages_api", return_value={"ok": True, "text": "api reply"}
        ):
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        self.assertEqual(records[0]["provider"], "claude")
        self.assertEqual(records[0]["final_text"], "api reply")

    def test_auto_with_no_cli_and_no_saved_key_returns_a_clear_error_not_a_crash(self):
        with mock.patch("webui_credentials.shutil.which", return_value=None):
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
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
        webui_credentials.save_credential("claude", "sk-should-be-unused", "claude-sonnet-5")
        with mock.patch("webui_api_key_mode.call_anthropic_messages_api") as spy:
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "auto", "hello", None, "continue")
        spy.assert_not_called()
        self.assertEqual(records[0]["provider"], "codex")

    def test_custom_provider_never_checks_cli_availability_at_all(self):
        # A custom provider (DEC-26) has no binary/CLI concept whatsoever
        # -- confirms cli_available() is never even consulted for one,
        # unlike every fixed-provider branch above.
        webui_credentials.save_custom_provider("openrouter", "sk-or", "meta-llama/llama-3", "https://openrouter.ai/api/v1", "openai")
        with mock.patch("webui_bridge_run.cli_available") as cli_spy, mock.patch(
            "webui_api_key_mode.call_openai_compatible_chat_api", return_value={"ok": True, "text": "custom reply"}
        ):
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "custom:openrouter", "hello", None, "continue")
        cli_spy.assert_not_called()
        self.assertEqual(records[0]["provider"], "custom:openrouter")
        self.assertEqual(records[0]["final_text"], "custom reply")

    def test_deleted_custom_provider_returns_a_clear_error_not_a_crash(self):
        # Selecting a custom provider in the composer, then deleting it in
        # the connection panel before sending, must not KeyError/crash --
        # a clear tool_failure record instead.
        with mock.patch("webui_api_key_mode.call_openai_compatible_chat_api") as spy:
            records = webui_bridge_run.run_provider_via_bridge(self.workspace, "custom:never-configured", "hello", None, "continue")
        spy.assert_not_called()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))
        self.assertIn("never-configured", records[0]["final_text"])


class ProviderApiLiveServerTests(unittest.TestCase):
    """GET /api/providers and POST /api/provider-key over a real HTTP
    server -- same pattern as HistoryDrawerLiveServerTests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
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
        self.assertEqual(set(by_name), {"codex", "claude", "gemini"})
        for info in by_name.values():
            self.assertIn("cli_detected", info)
            self.assertFalse(info["api_key_configured"])
        # DEC-25: all three now support API-key mode.
        self.assertTrue(by_name["codex"]["api_key_mode_supported"])
        self.assertTrue(by_name["claude"]["api_key_mode_supported"])
        self.assertTrue(by_name["gemini"]["api_key_mode_supported"])

    def _mock_valid_key_response(self):
        # POST /api/provider-key now calls validate_provider_api_key()
        # (a real HTTP call) before saving any non-empty key -- mocked
        # here at the same _http_post_json seam CallProviderApiTests
        # already uses, so these HTTP-server tests never hit the network.
        return mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"content": [{"type": "text", "text": "ok"}]}),
        )

    def test_saving_a_key_never_echoes_it_back(self):
        with self._mock_valid_key_response():
            status, data = self._post(
                "/api/provider-key", {"provider": "claude", "key": "sk-secret-value", "model": "claude-sonnet-5"}
            )
        self.assertEqual(status, 200)
        self.assertNotIn("key", data)
        self.assertEqual(
            data,
            {"provider": "claude", "api_key_configured": True, "model": "claude-sonnet-5", "verified": True, "confirmation": "ok"},
        )

    def test_saved_key_is_reflected_in_the_providers_list(self):
        with self._mock_valid_key_response():
            self._post("/api/provider-key", {"provider": "claude", "key": "sk-secret-value", "model": "claude-sonnet-5"})
        _, data = self._get("/api/providers")
        claude = next(p for p in data["providers"] if p["provider"] == "claude")
        self.assertTrue(claude["api_key_configured"])
        self.assertEqual(claude["model"], "claude-sonnet-5")

    def test_empty_key_removes_a_previously_saved_one(self):
        with self._mock_valid_key_response():
            self._post("/api/provider-key", {"provider": "claude", "key": "sk-secret-value", "model": "claude-sonnet-5"})
        # Removal (empty key) skips validation entirely -- no mock needed
        # for this second call, and none is active here.
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

    def test_gemini_key_can_be_saved_and_verified_too(self):
        # DEC-25 extended API_KEY_MODE_PROVIDERS to include gemini
        # (previously rejected here even though GET /api/providers and
        # POST /api/run both already recognized it as a real CLI
        # provider, DEC-15's original, narrower scope). Gemini's response
        # shape differs from Claude's -- candidates[0].content.parts,
        # not content -- so this uses its own mocked response rather than
        # reusing _mock_valid_key_response().
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
        ):
            status, data = self._post(
                "/api/provider-key", {"provider": "gemini", "key": "sk-x", "model": "gemini-2.5-flash"}
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["api_key_configured"])
        self.assertEqual(data["confirmation"], "ok")
        _, providers = self._get("/api/providers")
        gemini = next(p for p in providers["providers"] if p["provider"] == "gemini")
        self.assertTrue(gemini["api_key_configured"])

    def test_a_gemini_key_that_fails_validation_is_rejected_with_400_and_not_saved(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid"}}),
        ):
            status, data = self._post(
                "/api/provider-key", {"provider": "gemini", "key": "sk-wrong", "model": "gemini-2.5-flash"}
            )
        self.assertEqual(status, 400)
        self.assertIn("INVALID_ARGUMENT", data["error"])
        self.assertNotIn("sk-wrong", data["error"])
        _, providers = self._get("/api/providers")
        gemini = next(p for p in providers["providers"] if p["provider"] == "gemini")
        self.assertFalse(gemini["api_key_configured"])

    def test_a_key_without_a_model_is_rejected_with_400_and_not_saved(self):
        # A non-empty key has nothing to validate against without a
        # model, and no built-in default exists for either provider
        # (API_KEY_MODE_DEFAULT_MODELS is deliberately empty) -- so this
        # must fail loudly instead of silently saving an unusable
        # credential, exactly the "no key stored on the strength of an
        # unchecked string alone" gap this whole feature closes.
        status, data = self._post("/api/provider-key", {"provider": "claude", "key": "sk-x"})
        self.assertEqual(status, 400)
        self.assertIn("model", data["error"])
        _, providers = self._get("/api/providers")
        claude = next(p for p in providers["providers"] if p["provider"] == "claude")
        self.assertFalse(claude["api_key_configured"])

    def test_a_key_that_fails_validation_is_rejected_with_400_and_not_saved(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
        ):
            status, data = self._post(
                "/api/provider-key", {"provider": "claude", "key": "sk-wrong-value", "model": "claude-sonnet-5"}
            )
        self.assertEqual(status, 400)
        self.assertIn("authentication_error", data["error"])
        self.assertNotIn("sk-wrong-value", data["error"])
        _, providers = self._get("/api/providers")
        claude = next(p for p in providers["providers"] if p["provider"] == "claude")
        self.assertFalse(claude["api_key_configured"])

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
        with self._mock_valid_key_response():
            status, data = self._post(
                "/api/provider-key", {"provider": "claude", "key": "sk-x", "model": "claude-sonnet-5"}
            )
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class CliProviderModelLiveServerTests(unittest.TestCase):
    """POST /api/cli-model and GET /api/providers' `cli_model` field over
    a real HTTP server -- the CLI-mode sibling of ProviderApiLiveServerTests
    (which covers API-key mode's `model` field instead). No key/validation
    involved here at all -- the CLI itself handles auth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)

        self.state = webui.AppState(None)
        handler = webui.build_handler(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_providers_list_has_no_cli_model_by_default(self):
        _, data = self._get("/api/providers")
        for info in data["providers"]:
            self.assertIsNone(info["cli_model"])

    def test_saving_a_cli_model_needs_no_key_and_is_not_verified(self):
        status, data = self._post("/api/cli-model", {"provider": "codex", "model": "gpt-5-codex"})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"provider": "codex", "model": "gpt-5-codex"})

    def test_saved_cli_model_is_reflected_in_the_providers_list(self):
        self._post("/api/cli-model", {"provider": "claude", "model": "claude-opus"})
        _, data = self._get("/api/providers")
        claude = next(p for p in data["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["cli_model"], "claude-opus")
        # Distinct from API-key mode's own `model` field -- no key was
        # ever saved here, so that field must stay untouched.
        self.assertIsNone(claude["model"])
        self.assertFalse(claude["api_key_configured"])

    def test_empty_model_removes_a_previously_saved_one(self):
        self._post("/api/cli-model", {"provider": "gemini", "model": "gemini-2.5-pro"})
        status, data = self._post("/api/cli-model", {"provider": "gemini", "model": ""})
        self.assertEqual(status, 200)
        self.assertIsNone(data["model"])
        _, providers = self._get("/api/providers")
        gemini = next(p for p in providers["providers"] if p["provider"] == "gemini")
        self.assertIsNone(gemini["cli_model"])

    def test_invalid_provider_is_rejected_with_400(self):
        status, data = self._post("/api/cli-model", {"provider": "totally-unknown", "model": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_custom_provider_is_rejected_here_too(self):
        # Custom providers (DEC-26) have their own model field, set via
        # /api/custom-provider -- this endpoint is fixed-provider-only.
        status, data = self._post("/api/cli-model", {"provider": "custom:openrouter", "model": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_saving_one_providers_model_does_not_affect_another(self):
        self._post("/api/cli-model", {"provider": "codex", "model": "gpt-5-codex"})
        self._post("/api/cli-model", {"provider": "claude", "model": "claude-opus"})
        _, data = self._get("/api/providers")
        by_name = {p["provider"]: p for p in data["providers"]}
        self.assertEqual(by_name["codex"]["cli_model"], "gpt-5-codex")
        self.assertEqual(by_name["claude"]["cli_model"], "claude-opus")


class CustomProviderLiveServerTests(unittest.TestCase):
    """POST /api/custom-provider and GET /api/providers' `custom_providers`
    field over a real HTTP server -- same pattern as
    ProviderApiLiveServerTests (its fixed-provider counterpart)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
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

    def _mock_valid_key_response(self):
        return mock.patch(
            "webui_api_key_mode._http_post_json",
            return_value=(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
        )

    def test_new_server_has_no_custom_providers(self):
        status, data = self._get("/api/providers")
        self.assertEqual(status, 200)
        self.assertEqual(data["custom_providers"], [])

    def test_saving_a_custom_provider_never_echoes_the_key(self):
        with self._mock_valid_key_response():
            status, data = self._post(
                "/api/custom-provider",
                {"name": "openrouter", "key": "sk-secret", "model": "meta-llama/llama-3", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"},
            )
        self.assertEqual(status, 200)
        self.assertNotIn("key", data)
        self.assertEqual(data["provider"], "custom:openrouter")
        self.assertTrue(data["api_key_configured"])
        self.assertEqual(data["confirmation"], "ok")

    def test_saved_custom_provider_is_reflected_in_the_providers_list(self):
        with self._mock_valid_key_response():
            self._post(
                "/api/custom-provider",
                {"name": "openrouter", "key": "sk-secret", "model": "meta-llama/llama-3", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"},
            )
        _, data = self._get("/api/providers")
        entry = next(p for p in data["custom_providers"] if p["provider"] == "custom:openrouter")
        self.assertEqual(entry["name"], "openrouter")
        self.assertEqual(entry["api_format"], "openai")
        self.assertEqual(entry["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(entry["model"], "meta-llama/llama-3")
        self.assertTrue(entry["api_key_configured"])
        # Fixed providers are unaffected by adding a custom one.
        self.assertEqual({p["provider"] for p in data["providers"]}, {"codex", "claude", "gemini"})

    def test_empty_key_deletes_a_previously_saved_custom_provider(self):
        with self._mock_valid_key_response():
            self._post(
                "/api/custom-provider",
                {"name": "temp", "key": "sk-x", "model": "m", "base_url": "https://x.invalid", "api_format": "openai"},
            )
        status, data = self._post("/api/custom-provider", {"name": "temp", "key": ""})
        self.assertEqual(status, 200)
        self.assertFalse(data["api_key_configured"])
        _, providers = self._get("/api/providers")
        self.assertEqual(providers["custom_providers"], [])

    def test_name_colliding_with_a_builtin_provider_is_rejected_with_400(self):
        status, data = self._post(
            "/api/custom-provider",
            {"name": "claude", "key": "sk-x", "model": "m", "base_url": "https://x.invalid", "api_format": "openai"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_unknown_api_format_is_rejected_with_400(self):
        status, data = self._post(
            "/api/custom-provider",
            {"name": "x", "key": "sk-x", "model": "m", "base_url": "https://x.invalid", "api_format": "carrier-pigeon"},
        )
        self.assertEqual(status, 400)

    def test_base_url_without_scheme_is_rejected_with_400(self):
        status, data = self._post(
            "/api/custom-provider", {"name": "x", "key": "sk-x", "model": "m", "base_url": "x.invalid", "api_format": "openai"}
        )
        self.assertEqual(status, 400)

    def test_a_key_that_fails_validation_is_rejected_with_400_and_not_saved(self):
        with mock.patch(
            "webui_api_key_mode._http_post_json", return_value=(401, {"error": {"type": "invalid_api_key", "message": "bad key"}})
        ):
            status, data = self._post(
                "/api/custom-provider",
                {"name": "openrouter", "key": "sk-wrong", "model": "m", "base_url": "https://openrouter.ai/api/v1", "api_format": "openai"},
            )
        self.assertEqual(status, 400)
        self.assertNotIn("sk-wrong", data["error"])
        _, providers = self._get("/api/providers")
        self.assertEqual(providers["custom_providers"], [])


class SharedContextLiveServerTests(unittest.TestCase):
    """GET/POST /api/shared-context (DEC-27) over a real HTTP server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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

    def test_fresh_workspace_has_empty_shared_context(self):
        status, data = self._get("/api/shared-context")
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "")

    def test_save_then_read_round_trips(self):
        self._post("/api/shared-context", {"text": "Never touch legacy/."})
        status, data = self._get("/api/shared-context")
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "Never touch legacy/.")

    def test_saved_content_is_actually_on_disk_under_the_workspace(self):
        self._post("/api/shared-context", {"text": "hello"})
        self.assertEqual((self.root / ".handoff" / "shared-context.md").read_text(encoding="utf-8"), "hello")


class NoWorkspaceSharedContextLiveServerTests(unittest.TestCase):
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

    def test_get_with_no_workspace_returns_empty_text_not_an_error(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/shared-context", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode("utf-8")), {"text": ""})

    def test_post_with_no_workspace_is_rejected_with_400(self):
        body = json.dumps({"text": "hello"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/shared-context", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected an HTTPError")
        except urllib.error.HTTPError as exc:
            with exc:
                self.assertEqual(exc.code, 400)


class CheckForUpdateInBackgroundTests(unittest.TestCase):
    def test_a_fresh_appstate_has_not_been_checked_yet(self):
        # Regression guard for the race a review found: before this
        # function ever runs, update_checked must read as False, not be
        # indistinguishable from "checked, nothing found".
        state = webui.AppState(None)
        self.assertFalse(state.update_checked)
        self.assertIsNone(state.update_info)

    def test_sets_state_update_info_from_check_for_update(self):
        state = webui.AppState(None)
        with mock.patch(
            "handoff_webui._bridge_check_for_update",
            return_value={
                "status": "available",
                "latest_version": "0.2.0",
                "current_version": "0.1.0",
                "url": "https://example.invalid",
            },
        ):
            webui._check_for_update_in_background(state)
        self.assertEqual(state.update_info["status"], "available")
        self.assertEqual(state.update_info["latest_version"], "0.2.0")
        self.assertTrue(state.update_checked)

    def test_unavailable_result_still_marks_checked_with_the_real_status(self):
        # CFL-18: check_for_update() never returns None anymore -- even a
        # "couldn't check" outcome is a real dict with status:
        # "unavailable", distinguishable from "not checked yet" (which is
        # what leaves state.update_info as None, before this function
        # ever runs -- see test_a_fresh_appstate_has_not_been_checked_yet
        # above).
        state = webui.AppState(None)
        with mock.patch(
            "handoff_webui._bridge_check_for_update", return_value={"status": "unavailable", "current_version": "0.1.0"}
        ):
            webui._check_for_update_in_background(state)
        self.assertEqual(state.update_info, {"status": "unavailable", "current_version": "0.1.0"})
        self.assertTrue(state.update_checked)


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

    def test_before_the_background_check_finishes_checked_is_false(self):
        # Regression coverage for the race a review found: this must be
        # distinguishable from "checked, no update" (below) -- a
        # single-shot frontend fetch that landed in this window used to
        # permanently (and silently) miss a real update found moments
        # later, since both cases used to collapse into the same
        # response.
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"checked": False})

    def test_checked_with_no_update_found_is_distinguishable_from_pending(self):
        self.state.update_checked = True
        self.state.update_info = {"status": "current", "current_version": "0.1.0"}
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"checked": True, "status": "current", "current_version": "0.1.0"})

    def test_populated_update_info_is_reflected(self):
        self.state.update_checked = True
        self.state.update_info = {
            "status": "available",
            "latest_version": "0.2.0",
            "current_version": "0.1.0",
            "url": "https://example.invalid",
        }
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertTrue(data["checked"])
        self.assertEqual(data["status"], "available")
        self.assertEqual(data["latest_version"], "0.2.0")
        self.assertEqual(data["url"], "https://example.invalid")

    def test_unavailable_status_is_reflected_distinctly_from_current(self):
        # CFL-18: "couldn't check at all" (gh missing/unauthenticated/
        # offline) must never look like "checked, nothing newer" to a
        # client reading this response.
        self.state.update_checked = True
        self.state.update_info = {"status": "unavailable", "current_version": "0.1.0"}
        status, data = self._get("/api/update-check")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"checked": True, "status": "unavailable", "current_version": "0.1.0"})

    def test_sequential_requests_observe_the_real_pending_to_checked_transition(self):
        # A review pointed out the other tests here only ever assert the
        # two static end-states by setting state.* directly -- this one
        # exercises the actual transition a real page load experiences,
        # over the real live server, with a genuine background thread
        # (gated by an Event instead of a real multi-second `gh` call).
        release = threading.Event()

        def _delayed_check():
            release.wait(timeout=5)
            return {"status": "available", "latest_version": "0.2.0", "current_version": "0.1.0", "url": "https://example.invalid"}

        with mock.patch("handoff_webui._bridge_check_for_update", side_effect=_delayed_check):
            bg_thread = threading.Thread(
                target=webui._check_for_update_in_background, args=(self.state,), daemon=True
            )
            bg_thread.start()
            try:
                # The background thread is parked on `release` -- a
                # request arriving now must observe the pending state,
                # never a stale/incorrect "checked" answer for it.
                status, data = self._get("/api/update-check")
                self.assertEqual(status, 200)
                self.assertEqual(data, {"checked": False})
            finally:
                release.set()
                bg_thread.join(timeout=5)
            # Once the background thread has actually finished, the same
            # endpoint must reflect the real result on the next request.
            status, data = self._get("/api/update-check")
            self.assertEqual(status, 200)
            self.assertTrue(data["checked"])
            self.assertEqual(data["status"], "available")
            self.assertEqual(data["latest_version"], "0.2.0")


if __name__ == "__main__":
    unittest.main()
