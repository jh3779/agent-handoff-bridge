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
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handoff_bridge as hb  # noqa: E402
import handoff_webui as webui  # noqa: E402
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
    minimal PATH on macOS -- codex/claude/agy CLIs installed via
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
        # The function only does anything on macOS, where os.pathsep is ":";
        # pin it so this also passes on a Windows test host (issue #84).
        with mock.patch.object(webui.sys, "platform", "darwin"), mock.patch.object(
            webui.os, "pathsep", ":"
        ), mock.patch.object(webui.subprocess, "run", return_value=fake_result) as run:
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
            ), mock.patch.object(webui, "WebUIServer", return_value=mock.Mock()), mock.patch.object(
                webui, "prewarm_agy_models"
            ), mock.patch("builtins.print") as print_spy:
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
        cls.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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


class WebUIServerBacklogTests(unittest.TestCase):
    def test_the_listen_backlog_is_larger_than_socketservers_default_of_5(self):
        # With the default, 8 simultaneous clients got "Connection reset by
        # peer" on macOS / "Remote end closed connection" on Windows.
        self.assertGreaterEqual(webui.WebUIServer.request_queue_size, 64)

    def test_it_is_still_a_threading_http_server(self):
        from http.server import ThreadingHTTPServer

        self.assertTrue(issubclass(webui.WebUIServer, ThreadingHTTPServer))


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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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

    def test_default_session_id_uses_the_original_unscoped_path(self):
        # M1 (multi-session): a workspace's history from before this
        # feature existed must keep working with zero migration -- the
        # default session id is a no-op on chat_dir(), not a real
        # subfolder.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(webui_chat_storage.chat_dir(root), root / ".handoff" / "webui" / "chat")
            self.assertEqual(
                webui_chat_storage.chat_dir(root, webui_chat_storage.DEFAULT_SESSION_ID),
                webui_chat_storage.chat_dir(root),
            )

    def test_non_default_session_id_gets_its_own_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                webui_chat_storage.chat_dir(root, "abc123"),
                root / ".handoff" / "webui" / "chat" / "abc123",
            )

    def test_two_sessions_in_the_same_workspace_have_independent_histories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "in the default session", [], now)
            webui_chat_storage.append_chat_message(root, "user", "in session A", [], now, session_id="session-a")
            webui_chat_storage.append_chat_message(root, "user", "in session B", [], now, session_id="session-b")

            default_msgs = webui_chat_storage.read_month_messages(root, "2026-09")
            a_msgs = webui_chat_storage.read_month_messages(root, "2026-09", session_id="session-a")
            b_msgs = webui_chat_storage.read_month_messages(root, "2026-09", session_id="session-b")

            self.assertEqual([m["text"] for m in default_msgs], ["in the default session"])
            self.assertEqual([m["text"] for m in a_msgs], ["in session A"])
            self.assertEqual([m["text"] for m in b_msgs], ["in session B"])

    def test_list_available_months_is_session_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
            webui_chat_storage.append_chat_message(root, "user", "default", [], now)
            webui_chat_storage.append_chat_message(root, "user", "session A", [], now, session_id="session-a")
            self.assertEqual(webui_chat_storage.list_available_months(root), ["2026-09"])
            self.assertEqual(webui_chat_storage.list_available_months(root, "session-a"), ["2026-09"])
            self.assertEqual(webui_chat_storage.list_available_months(root, "session-b"), [])

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

# M3 (docs/research-session-splitting.md, verified concurrent execution):
# a deliberately slow fake provider -- real timing (not a mocked lock) is
# the only way to actually prove two calls ran side by side rather than
# one after the other. 1.2s is comfortably above scheduling/process-spawn
# noise on a normal dev/CI machine while keeping the test suite fast.
FAKE_CODEX_SLOW_SUCCESS = """#!/bin/sh
cat >/dev/null
sleep 1.2
cat <<'EOF'
{"type": "thread.started", "thread_id": "fake-codex-session"}
{"type": "item.completed", "item": {"type": "agent_message", "text": "fake codex reply"}}
{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
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
        # A held run_lock must make a second call using the *same lock
        # object* fail immediately (RunAlreadyInProgressError), not block
        # for the full timeout or silently race. M1: the caller now
        # supplies the lock (AppState.get_run_lock_for() in real use,
        # keyed by workspace) rather than this module holding one itself.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            run_lock = threading.Lock()
            run_lock.acquire()
            try:
                with self.assertRaises(webui_bridge_run.RunAlreadyInProgressError):
                    webui_bridge_run.run_provider_via_bridge(run_lock, root, "codex", "hello", None, "continue")
            finally:
                run_lock.release()

    def test_lock_is_released_after_a_normal_call_so_the_next_one_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            run_lock = threading.Lock()
            webui_bridge_run.run_provider_via_bridge(run_lock, root, "codex", "first", None, "continue")
            # Would raise RunAlreadyInProgressError if the lock leaked --
            # reusing the *same* run_lock object for both calls is the
            # point of this test.
            records = webui_bridge_run.run_provider_via_bridge(run_lock, root, "codex", "second", None, "continue")
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
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", "hello", None, "continue")
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
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", "hello", "--weird-model-name", "continue")
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
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", prompt, None, "continue")

        self.assertIn("do the thing", captured["prompt_text"])
        self.assertIn("a.py", captured["prompt_text"])
        self.assertIn("print('hi')", captured["prompt_text"])

    def test_successful_run_produces_one_history_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SUCCESS)
            records = webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", "hello", None, "continue")
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
            records = webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", "hello", None, "continue")
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
            threading.Lock(), Path("/definitely/does/not/exist"), "codex", "hello", None, "continue"
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
            threading.Lock(), Path("/definitely/does/not/exist"), "auto", "hello", None, "continue"
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
                records = webui_bridge_run.run_provider_via_bridge(threading.Lock(), root, "codex", "hello", None, "continue")

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
            with mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), workspace, "codex", "hello", None, "continue")
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
            with mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), workspace, "codex", "hello", None, "continue", auto_fallback)
        return run_spy.call_args.args[0]

    def test_default_includes_auto_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with mock.patch(
                "webui_bridge_run.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ) as run_spy:
                webui_bridge_run.run_provider_via_bridge(threading.Lock(), workspace, "codex", "hello", None, "continue")
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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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

    def test_run_marks_an_allow_all_agy_run_in_the_message(self):
        # run_provider()'s stdout warning never reaches a Web UI user, so the
        # record's permissions field has to become visible in the message.
        def record(permissions):
            return {
                "provider": "agy",
                "model": None,
                "exit_code": 0,
                "session_id": None,
                "final_text": "done",
                "handoff_needed": False,
                "reason": "none",
                **({"permissions": permissions} if permissions else {}),
            }

        with mock.patch("handoff_webui.run_provider_via_bridge", return_value=[record("allow-all")]):
            status, data = self._post("/api/run", {"provider": "agy", "text": "hi"})
        self.assertEqual(status, 200)
        text = data["messages"][0]["text"]
        self.assertIn("auto-approved", text)
        self.assertTrue(text.endswith("done"))

        with mock.patch("handoff_webui.run_provider_via_bridge", return_value=[record(None)]):
            _status, data = self._post("/api/run", {"provider": "agy", "text": "hi"})
        self.assertEqual(data["messages"][0]["text"], "done")

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


    def test_concurrent_run_gets_409_not_a_hang_or_duplicate_message(self):
        # M1: the lock is keyed by workspace path (AppState.get_run_lock_for()),
        # not a single global lock -- must grab the *same* lock object the
        # server itself will resolve for this session's workspace (self.root)
        # to actually simulate "already in progress" for it.
        run_lock = self.state.get_run_lock_for(self.root)
        run_lock.acquire()
        try:
            status, data = self._post("/api/run", {"provider": "codex", "text": "hi"})
        finally:
            run_lock.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_open_folder_is_rejected_while_a_run_is_in_flight(self):
        # A run writes into whatever workspace was active when it started
        # and persists into that workspace's chat log on completion --
        # switching state.workspace out from under it mid-run would
        # misdirect where that write (and the client's eventual render of
        # it) ends up.
        run_lock = self.state.get_run_lock_for(self.root)
        run_lock.acquire()
        try:
            status, data = self._post("/api/open-folder", {"path": str(self.root)})
        finally:
            run_lock.release()
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
        run_lock = self.state.get_run_lock_for(self.root)
        run_lock.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "user", "text": "a second message", "attachments": []})
        finally:
            run_lock.release()
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
        run_lock = self.state.get_run_lock_for(self.root)
        run_lock.acquire()
        try:
            status, data = self._post("/api/chat", {"role": "system", "text": "note", "attachments": []})
        finally:
            run_lock.release()
        self.assertEqual(status, 200)

    def test_run_with_invalid_provider_is_rejected(self):
        # A provider name that was once real (agy, removed after Google
        # ended individual-account service) is as invalid as any other.
        for name in ("not-a-real-provider", "gemini"):
            status, data = self._post("/api/run", {"provider": name, "text": "hi"})
            self.assertEqual(status, 400, msg=name)
            self.assertIn("error", data)


class GetRunLockForTests(unittest.TestCase):
    """AppState.get_run_lock_for() in isolation, no HTTP involved -- the
    exact mechanism M1 depends on to make "same workspace, two sessions"
    safe (one shared lock) and "different workspaces, two sessions"
    genuinely parallel (independent locks). See its own docstring in
    handoff_webui.py for the real state.json race this was found fixing
    mid-implementation."""

    def test_same_workspace_path_returns_the_same_lock_object(self):
        state = webui.AppState(None)
        workspace = Path("/some/workspace")
        self.assertIs(state.get_run_lock_for(workspace), state.get_run_lock_for(workspace))

    def test_different_workspace_paths_get_different_lock_objects(self):
        state = webui.AppState(None)
        self.assertIsNot(
            state.get_run_lock_for(Path("/workspace/a")), state.get_run_lock_for(Path("/workspace/b"))
        )

    def test_holding_one_workspaces_lock_does_not_affect_anothers(self):
        state = webui.AppState(None)
        lock_a = state.get_run_lock_for(Path("/workspace/a"))
        lock_b = state.get_run_lock_for(Path("/workspace/b"))
        lock_a.acquire()
        try:
            self.assertTrue(lock_a.locked())
            self.assertFalse(lock_b.locked())
        finally:
            lock_a.release()


class PersistedSessionsTests(unittest.TestCase):
    """read/write/restore_persisted_sessions() (M2): sessions.json survives
    a restart for every session *other than* the default one, which is
    always freshly resolved from --workspace/discovery instead, exactly as
    before M1 existed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.workspace = (Path(self.tmp.name) / "some-project").resolve()
        self.workspace.mkdir()

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(webui.read_persisted_sessions(), [])

    def test_write_then_read_round_trips(self):
        state = webui.AppState(self.workspace)  # the default session
        state.sessions["extra-1"] = webui.SessionState(self.workspace)
        webui.write_persisted_sessions(state)
        persisted = webui.read_persisted_sessions()
        self.assertEqual(persisted, [{"session_id": "extra-1", "workspace": str(self.workspace)}])

    def test_default_session_is_never_persisted(self):
        state = webui.AppState(self.workspace)
        webui.write_persisted_sessions(state)
        self.assertEqual(webui.read_persisted_sessions(), [])

    def test_a_session_with_no_workspace_persists_as_null(self):
        state = webui.AppState(None)
        state.sessions["extra-1"] = webui.SessionState(None)
        webui.write_persisted_sessions(state)
        self.assertEqual(webui.read_persisted_sessions(), [{"session_id": "extra-1", "workspace": None}])

    def test_restore_recreates_extra_sessions_alongside_the_default_one(self):
        writer_state = webui.AppState(self.workspace)
        writer_state.sessions["extra-1"] = webui.SessionState(self.workspace)
        webui.write_persisted_sessions(writer_state)

        booted_state = webui.AppState(self.workspace)  # simulates a fresh main() boot
        webui.restore_persisted_sessions(booted_state)
        self.assertEqual(set(booted_state.sessions), {"default", "extra-1"})
        self.assertEqual(booted_state.sessions["extra-1"].workspace, self.workspace)
        self.assertEqual(booted_state.sessions["default"].workspace, self.workspace)

    def test_restore_drops_a_session_whose_workspace_no_longer_exists(self):
        gone = self.workspace / "deleted-later"
        gone.mkdir()
        writer_state = webui.AppState(None)
        writer_state.sessions["extra-1"] = webui.SessionState(gone)
        webui.write_persisted_sessions(writer_state)
        shutil.rmtree(gone)

        booted_state = webui.AppState(None)
        webui.restore_persisted_sessions(booted_state)
        self.assertIn("extra-1", booted_state.sessions)
        self.assertIsNone(booted_state.sessions["extra-1"].workspace)


class MultiSessionLiveServerTests(FakeProviderPathMixin, unittest.TestCase):
    """POST/GET/DELETE /api/sessions and the X-AHB-Session header contract
    (docs/research-session-splitting.md, M1) over a real HTTP server --
    covers both required session-unit shapes: a second session on the
    *same* workspace as the default one, and a second session on a
    *different* workspace."""

    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        # .resolve(): validate_workspace_candidate() (POST /api/open-folder)
        # resolves whatever path it's given, so session.workspace ends up
        # e.g. /private/var/... on macOS even when given a /var/... path
        # (a symlink). Matching that here up front means str(self.root)/
        # get_run_lock_for(self.root) actually refer to the same path the
        # server itself resolves to -- unresolved, a couple of assertions
        # below would silently compare/lock against a *different* path
        # than the one the server is really using.
        self.root = (Path(self.tmp.name) / "workspace-a").resolve()
        self.root.mkdir()
        self.root_b = (Path(self.tmp.name) / "workspace-b").resolve()
        self.root_b.mkdir()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.state = webui.AppState(self.root)
        handler = webui.build_handler(self.state)
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, payload=None, session_id=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if session_id is not None:
            headers[webui.SESSION_HEADER] = session_id
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path, session_id=None):
        return self._request("GET", path, session_id=session_id)

    def _post(self, path, payload, session_id=None):
        return self._request("POST", path, payload=payload, session_id=session_id)

    def _delete(self, path, session_id=None):
        return self._request("DELETE", path, session_id=session_id)

    def test_get_sessions_lists_the_default_session_at_boot(self):
        status, data = self._get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(data["sessions"], [{"session_id": "default", "workspace": str(self.root)}])

    def test_no_session_header_resolves_to_default_unchanged(self):
        # The whole point of the fallback: a request that predates M1 (or
        # a direct API script) keeps working exactly as before.
        status, data = self._get("/api/info")
        self.assertEqual(status, 200)
        self.assertEqual(data["workspace"], str(self.root))

    def test_post_sessions_creates_a_new_empty_session(self):
        status, data = self._post("/api/sessions", {})
        self.assertEqual(status, 200)
        self.assertIsNone(data["workspace"])
        session_id = data["session_id"]
        self.assertTrue(session_id)

        _, listing = self._get("/api/sessions")
        ids = {s["session_id"] for s in listing["sessions"]}
        self.assertEqual(ids, {"default", session_id})

    def test_new_session_starts_with_no_workspace_independent_of_default(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        status, info = self._get("/api/info", session_id=session_id)
        self.assertEqual(status, 200)
        self.assertIsNone(info["workspace"])
        # The default session is completely unaffected by a new session existing.
        _, default_info = self._get("/api/info")
        self.assertEqual(default_info["workspace"], str(self.root))

    def test_switching_a_new_sessions_workspace_does_not_move_the_default_one(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        status, data = self._post("/api/open-folder", {"path": str(self.root_b)}, session_id=session_id)
        self.assertEqual(status, 200)

        _, session_info = self._get("/api/info", session_id=session_id)
        self.assertEqual(session_info["workspace"], str(self.root_b))
        _, default_info = self._get("/api/info")
        self.assertEqual(default_info["workspace"], str(self.root))

    def test_two_sessions_on_the_same_workspace_have_independent_chat_history(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root)}, session_id=session_id)
        self._post("/api/chat", {"role": "user", "text": "from default", "attachments": []})
        self._post("/api/chat", {"role": "user", "text": "from the new session", "attachments": []}, session_id=session_id)

        _, default_chat = self._get("/api/chat")
        _, session_chat = self._get("/api/chat", session_id=session_id)
        self.assertEqual([m["text"] for m in default_chat["messages"]], ["from default"])
        self.assertEqual([m["text"] for m in session_chat["messages"]], ["from the new session"])

    def test_unknown_session_header_is_rejected(self):
        status, data = self._get("/api/info", session_id="totally-made-up")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_app_level_endpoints_do_not_require_a_known_session(self):
        # Provider credentials/models are app-level, not workspace state --
        # never needed a session concept even before M1.
        status, data = self._post(
            "/api/cli-model", {"provider": "codex", "model": "x"}, session_id="totally-made-up"
        )
        self.assertEqual(status, 200)

    def test_default_session_cannot_be_closed(self):
        status, data = self._delete("/api/sessions/default")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_deleting_an_unknown_session_is_404(self):
        status, data = self._delete("/api/sessions/totally-made-up")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_closed_session_id_is_no_longer_usable(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        status, _ = self._delete(f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)

        status, data = self._get("/api/info", session_id=session_id)
        self.assertEqual(status, 400)
        self.assertIn("error", data)
        _, listing = self._get("/api/sessions")
        self.assertEqual([s["session_id"] for s in listing["sessions"]], ["default"])

    def test_closing_a_session_does_not_delete_its_chat_history_on_disk(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root)}, session_id=session_id)
        self._post("/api/chat", {"role": "user", "text": "keep me", "attachments": []}, session_id=session_id)
        self._delete(f"/api/sessions/{session_id}")
        chat_dir = webui_chat_storage.chat_dir(self.root, session_id)
        self.assertTrue(chat_dir.exists())
        self.assertTrue(any(chat_dir.glob("*.jsonl")))

    def test_deleting_a_session_with_a_run_in_flight_is_409(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root_b)}, session_id=session_id)
        run_lock = self.state.get_run_lock_for(self.root_b)
        run_lock.acquire()
        try:
            status, data = self._delete(f"/api/sessions/{session_id}")
        finally:
            run_lock.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_a_run_in_one_session_blocks_a_new_message_in_another_session_on_the_SAME_workspace(self):
        # The critical regression this whole redesign exists for: two
        # sessions pointed at the same workspace share handoff_bridge.py's
        # one <workspace>/.handoff/state.json, so their runs must still
        # serialize against each other even though they're different
        # sessions -- otherwise the before/after history-length diff in
        # webui_bridge_run.py can misattribute or duplicate a record.
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root)}, session_id=session_id)  # same workspace as default
        run_lock = self.state.get_run_lock_for(self.root)
        run_lock.acquire()
        try:
            status, data = self._post(
                "/api/chat", {"role": "user", "text": "hi", "attachments": []}, session_id=session_id
            )
        finally:
            run_lock.release()
        self.assertEqual(status, 409)
        self.assertIn("error", data)

    def test_a_run_in_one_session_does_NOT_block_a_new_message_in_another_session_on_a_DIFFERENT_workspace(self):
        # The actual parallelism guarantee this feature is supposed to
        # deliver: two sessions on two different workspaces have
        # independent state.json files and independent locks, so one
        # being "busy" must never affect the other.
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root_b)}, session_id=session_id)  # different workspace
        run_lock = self.state.get_run_lock_for(self.root)  # holding the DEFAULT session's workspace lock
        run_lock.acquire()
        try:
            status, data = self._post(
                "/api/chat", {"role": "user", "text": "hi", "attachments": []}, session_id=session_id
            )
        finally:
            run_lock.release()
        self.assertEqual(status, 200)

    def test_creating_a_session_persists_it_to_disk(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        persisted = webui.read_persisted_sessions()
        self.assertEqual(persisted, [{"session_id": session_id, "workspace": None}])

    def test_closing_a_session_removes_it_from_disk(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._delete(f"/api/sessions/{session_id}")
        self.assertEqual(webui.read_persisted_sessions(), [])

    def test_switching_a_sessions_workspace_updates_the_persisted_entry(self):
        _, created = self._post("/api/sessions", {})
        session_id = created["session_id"]
        self._post("/api/open-folder", {"path": str(self.root_b)}, session_id=session_id)
        persisted = webui.read_persisted_sessions()
        self.assertEqual(persisted, [{"session_id": session_id, "workspace": str(self.root_b)}])

    def test_default_session_workspace_switch_does_not_appear_on_disk(self):
        # The default session's workspace is always freshly resolved from
        # --workspace/discovery at boot, not from this file (see
        # restore_persisted_sessions()'s own docstring) -- persisting it
        # here too would be redundant, not just harmless.
        self._post("/api/open-folder", {"path": str(self.root_b)})
        self.assertEqual(webui.read_persisted_sessions(), [])


class RealConcurrentExecutionTests(FakeProviderPathMixin, unittest.TestCase):
    """M3 (docs/research-session-splitting.md): confirm the locking model
    holds up against genuinely slow, real subprocess calls running
    through the actual HTTP server -- not just mocked locks acquired
    instantly in a test body (GetRunLockForTests/MultiSessionLiveServerTests
    already covered that at the unit level). Uses FAKE_CODEX_SLOW_SUCCESS
    (a real subprocess that actually sleeps) and wall-clock timing as the
    only way to actually distinguish "ran in parallel" from "ran one after
    the other" -- a mocked lock can't tell those apart, only real elapsed
    time can."""

    def setUp(self):
        self.setUpFakeProviders()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = (Path(self.tmp.name) / "workspace-a").resolve()
        self.root.mkdir()
        self.root_b = (Path(self.tmp.name) / "workspace-b").resolve()
        self.root_b.mkdir()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.state = webui.AppState(self.root)
        handler = webui.build_handler(self.state)
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)
        _write_fake_provider(self.fake_bin, "codex", FAKE_CODEX_SLOW_SUCCESS)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, payload=None, session_id=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if session_id is not None:
            headers[webui.SESSION_HEADER] = session_id
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_two_sessions_on_different_workspaces_run_genuinely_in_parallel(self):
        _, created = self._request("POST", "/api/sessions", {})
        session_b = created["session_id"]
        self._request("POST", "/api/open-folder", {"path": str(self.root_b)}, session_id=session_b)

        results = {}

        def run_default():
            results["default"] = self._request("POST", "/api/run", {"provider": "codex", "text": "hi"})

        def run_b():
            results["b"] = self._request(
                "POST", "/api/run", {"provider": "codex", "text": "hi"}, session_id=session_b
            )

        start = time.monotonic()
        t1 = threading.Thread(target=run_default)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        elapsed = time.monotonic() - start

        self.assertEqual(results["default"][0], 200)
        self.assertEqual(results["b"][0], 200)
        # Two different workspaces -> two different locks (AppState.
        # get_run_lock_for()) -> both 1.2s calls genuinely overlap. Fully
        # serialized would take >=2.4s; allow generous slack for process-
        # spawn/scheduling noise while still clearly distinguishing
        # "parallel" from "serial".
        self.assertLess(elapsed, 2.0, "two different workspaces' runs did not overlap -- locking is over-serializing")

    def test_two_sessions_on_the_same_workspace_correctly_reject_the_overlap(self):
        # The exact scenario M1's real bug was found in: two sessions on
        # the SAME workspace both racing handoff_bridge.py run's history-
        # length diff against one shared .handoff/state.json. This uses
        # the real subprocess + real lock end-to-end (no mocks), unlike
        # GetRunLockForTests/MultiSessionLiveServerTests' instant mocked
        # locks.
        #
        # Note this is NOT a timing assertion, deliberately: run_lock.
        # acquire(blocking=False) in webui_bridge_run.py means the *correct*
        # behavior for a genuine overlap is an immediate 409 for whichever
        # request loses the race, not a queued wait -- so a buggy version
        # where both requests wrongly ran fully-concurrent subprocesses
        # would show the *same* ~1.2s elapsed time as the correct
        # one-runs/one-rejected outcome. Elapsed time can't tell those
        # apart here; only the actual status codes can -- which is exactly
        # what the timing-based test above is for the *different*-workspace
        # case, where there's no fail-fast rejection to check instead.
        _, created = self._request("POST", "/api/sessions", {})
        session_b = created["session_id"]
        self._request("POST", "/api/open-folder", {"path": str(self.root)}, session_id=session_b)  # same workspace

        results = {}

        def run_default():
            results["default"] = self._request("POST", "/api/run", {"provider": "codex", "text": "from default"})

        def run_b():
            results["b"] = self._request(
                "POST", "/api/run", {"provider": "codex", "text": "from b"}, session_id=session_b
            )

        t1 = threading.Thread(target=run_default)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Whichever request's HTTP handler thread reached run_lock.acquire()
        # second must have been rejected outright (409) -- both getting 200
        # would mean two subprocesses genuinely ran concurrently against
        # the same workspace, the exact bug this whole redesign exists to
        # prevent. (Both landing 409 is not a real possible outcome here --
        # at least one request necessarily acquires the uncontended lock.)
        statuses = sorted([results["default"][0], results["b"][0]])
        self.assertEqual(statuses, [200, 409])

    def test_two_sequential_runs_on_the_same_workspace_each_get_exactly_their_own_record(self):
        # Complements the test above: two *sequential* (not overlapping)
        # runs against the same workspace, one from each of two sessions,
        # must each independently see exactly one new record -- proving
        # the lock being shared for the same workspace doesn't also cause
        # cross-session record misattribution when there's no actual race
        # (the specific bug class M1 was fixed for, reproduced here
        # end-to-end through the real HTTP server + real subprocess
        # instead of only at the webui_bridge_run.py unit level).
        _, created = self._request("POST", "/api/sessions", {})
        session_b = created["session_id"]
        self._request("POST", "/api/open-folder", {"path": str(self.root)}, session_id=session_b)

        status_a, data_a = self._request("POST", "/api/run", {"provider": "codex", "text": "first"})
        self.assertEqual(status_a, 200)
        self.assertEqual(len(data_a["messages"]), 1)

        status_b, data_b = self._request(
            "POST", "/api/run", {"provider": "codex", "text": "second"}, session_id=session_b
        )
        self.assertEqual(status_b, 200)
        self.assertEqual(len(data_b["messages"]), 1)  # not two -- the first call's record must not leak in here


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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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


class CliProviderModelTests(unittest.TestCase):
    """read_cli_provider_models()/save_cli_provider_model(): a provider's
    default --model override -- no key is ever involved."""

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
        webui_credentials.save_cli_provider_model("agy", "   ")
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

    def test_legacy_api_key_entries_are_preserved_untouched_on_save(self):
        # API-key mode was removed, but a pre-removal install may still have
        # key entries in the same file. Nothing here reads them, and a save
        # must not silently delete a user's data either.
        legacy = {
            "claude": {"key": "sk-legacy", "model": "claude-sonnet-5"},
            "custom_providers": {"openrouter": {"key": "sk-or", "base_url": "https://x", "api_format": "openai"}},
        }
        self.base_dir.mkdir(parents=True)
        path = self.base_dir / "credentials.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        webui_credentials.save_cli_provider_model("codex", "gpt-5-codex")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["claude"], legacy["claude"])
        self.assertEqual(saved["custom_providers"], legacy["custom_providers"])
        self.assertEqual(webui_credentials.read_cli_provider_models(), {"codex": "gpt-5-codex"})


class AgyLiveModelsTests(unittest.TestCase):
    """`agy models` is the CLI's own current list; the app asks it instead of
    trusting a copy in the source that goes stale when Google ships a model."""

    def setUp(self):
        patcher = mock.patch.dict(
            webui_credentials._agy_models_cache,
            {"models": [], "ok_at": None, "failed_at": None, "refreshing": False},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parsing_skips_the_banner_and_malformed_lines(self):
        out = "Fetching available models...\n" "a-model-high\tA Model (High)\n" "\n" "no tab here\n" "-bad\tx\n" "a-model-high\tdup\n" "b.model:1\n" "c-model\t\n"
        self.assertEqual(
            webui_credentials._parse_agy_models(out),
            [{"id": "a-model-high", "label": "A Model (High)"}, {"id": "c-model", "label": "c-model"}],
        )

    def test_a_live_list_is_authoritative_and_cached(self):
        live = [{"id": "m1", "label": "M1"}]
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._fetch_agy_models", return_value=live
        ) as fetch:
            first = webui_credentials.cli_model_catalog("agy", wait=True)
            second = webui_credentials.cli_model_catalog("agy", wait=True)
        self.assertEqual(first, {"models": live, "authoritative": True, "source": "agy-live", "refreshing": False})
        self.assertEqual(second, first)
        self.assertEqual(fetch.call_count, 1)

    def test_a_failed_lookup_falls_back_to_the_built_in_list_and_backs_off(self):
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._fetch_agy_models", return_value=[]
        ) as fetch:
            first = webui_credentials.cli_model_catalog("agy", wait=True)
            webui_credentials.cli_model_catalog("agy", wait=True)
        self.assertEqual(first["source"], "built-in")
        self.assertFalse(first["authoritative"])  # the built-in copy can itself be old
        self.assertEqual(first["models"], webui_credentials._AGY_KNOWN_MODELS)
        self.assertEqual(fetch.call_count, 1)  # not retried within the backoff

    def test_a_later_failure_keeps_the_last_good_list(self):
        live = [{"id": "m1", "label": "M1"}]
        with mock.patch("webui_credentials.cli_available", return_value=True):
            with mock.patch("webui_credentials._fetch_agy_models", return_value=live):
                webui_credentials.cli_model_catalog("agy", wait=True)
            webui_credentials._agy_models_cache["ok_at"] = None  # expire the TTL
            with mock.patch("webui_credentials._fetch_agy_models", return_value=[]):
                again = webui_credentials.cli_model_catalog("agy", wait=True)
        self.assertEqual(again["models"], live)
        # Still useful as suggestions, but no longer the CLI's *current* list.
        self.assertEqual(again["source"], "agy-cache")
        self.assertFalse(again["authoritative"])

    def test_an_expired_list_never_calls_a_pinned_model_stale(self):
        # agy shipped a new model after our last successful lookup, the user pinned
        # it, and the refresh failed: the old list lacks it, which proves nothing.
        old_list = [{"id": "old-model", "label": "Old"}]
        webui_credentials._agy_models_cache.update(
            models=old_list, ok_at=time.monotonic() - webui_credentials._AGY_MODELS_TTL_SECONDS - 1
        )
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._start_agy_refresh"
        ):
            catalog = webui_credentials.cli_model_catalog("agy")
        self.assertEqual(catalog["models"], old_list)
        self.assertFalse(catalog["authoritative"])
        self.assertFalse(webui_credentials.model_is_stale("brand-new-model", catalog))
        # ...and the same pin is only judged once a fresh lookup is in.
        webui_credentials._agy_models_cache.update(ok_at=time.monotonic())
        with mock.patch("webui_credentials.cli_available", return_value=True):
            fresh = webui_credentials.cli_model_catalog("agy")
        self.assertTrue(webui_credentials.model_is_stale("brand-new-model", fresh))

    def test_the_catalog_says_when_a_refresh_is_in_flight(self):
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._start_agy_refresh"
        ):
            first = webui_credentials.cli_model_catalog("agy")
            second = webui_credentials.cli_model_catalog("agy")
        self.assertTrue(first["refreshing"])
        self.assertTrue(second["refreshing"])
        for provider in ("codex", "claude"):
            self.assertFalse(webui_credentials.cli_model_catalog(provider, live=False)["refreshing"])

    def test_by_default_it_answers_at_once_and_refreshes_in_the_background(self):
        # The first `agy models` takes ~5s; the Settings panel must not wait for it.
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._start_agy_refresh"
        ) as start, mock.patch("webui_credentials._fetch_agy_models") as fetch:
            first = webui_credentials.cli_model_catalog("agy")
            second = webui_credentials.cli_model_catalog("agy")
        self.assertEqual(first["source"], "built-in")
        self.assertEqual(second["source"], "built-in")
        fetch.assert_not_called()  # nothing ran inline
        self.assertEqual(start.call_count, 1)  # and only one refresh is ever in flight

    def test_the_background_refresh_fills_the_cache_for_the_next_call(self):
        live = [{"id": "m1", "label": "M1"}]
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "webui_credentials._fetch_agy_models", return_value=live
        ), mock.patch("webui_credentials._start_agy_refresh") as start:
            first = webui_credentials.cli_model_catalog("agy")  # asks for a refresh, answers at once
            start.assert_called_once()
            # The real refresh runs on its own thread after the lock is released;
            # run it here the same way (never from inside _start_agy_refresh,
            # which is called under the lock).
            webui_credentials._refresh_agy_models()
            second = webui_credentials.cli_model_catalog("agy")
        self.assertEqual(first["source"], "built-in")
        self.assertEqual(second, {"models": live, "authoritative": True, "source": "agy-live", "refreshing": False})
        self.assertFalse(webui_credentials._agy_models_cache["refreshing"])

    def test_the_off_switch_stops_every_lookup_not_just_the_startup_one(self):
        # A test server on a machine with agy installed must never spawn the real
        # agy (it opens local sockets and made a port check report a false hit).
        with mock.patch.dict(os.environ, {"AHB_NO_MODEL_PREWARM": "1"}), mock.patch(
            "webui_credentials.cli_available", return_value=True
        ), mock.patch("webui_credentials._start_agy_refresh") as start, mock.patch(
            "webui_credentials._fetch_agy_models"
        ) as fetch:
            webui_credentials.prewarm_agy_models()
            catalog = webui_credentials.cli_model_catalog("agy")
            waited = webui_credentials.cli_model_catalog("agy", wait=True)
        start.assert_not_called()
        fetch.assert_not_called()
        for result in (catalog, waited):
            self.assertEqual(result["source"], "built-in")
            self.assertFalse(result["refreshing"])  # the page must not poll for a lookup that will never come

    def test_prewarm_only_runs_when_agy_is_installed(self):
        with mock.patch("webui_credentials._start_agy_refresh") as start:
            with mock.patch("webui_credentials.cli_available", return_value=False):
                webui_credentials.prewarm_agy_models()
            start.assert_not_called()
            with mock.patch("webui_credentials.cli_available", return_value=True):
                webui_credentials.prewarm_agy_models()
            start.assert_called_once()

    def test_no_process_is_started_when_live_is_off_or_the_cli_is_missing(self):
        with mock.patch("webui_credentials._fetch_agy_models") as fetch:
            with mock.patch("webui_credentials.cli_available", return_value=True):
                webui_credentials.cli_model_catalog("agy", live=False)
            with mock.patch("webui_credentials.cli_available", return_value=False):
                webui_credentials.cli_model_catalog("agy", wait=True)
        fetch.assert_not_called()

    def test_fetch_survives_timeouts_missing_binaries_and_errors(self):
        for effect in (subprocess.TimeoutExpired("agy", 8), FileNotFoundError("agy")):
            with mock.patch("webui_credentials.subprocess.run", side_effect=effect):
                self.assertEqual(webui_credentials._fetch_agy_models(), [])
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="x\ty\n", stderr="")
        with mock.patch("webui_credentials.subprocess.run", return_value=failed):
            self.assertEqual(webui_credentials._fetch_agy_models(), [])
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="Fetching...\nm1\tM1\n", stderr="")
        with mock.patch("webui_credentials.subprocess.run", return_value=ok) as run:
            self.assertEqual(webui_credentials._fetch_agy_models(), [{"id": "m1", "label": "M1"}])
        self.assertEqual(run.call_args.args[0], ["agy", "models"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class ModelIsStaleTests(unittest.TestCase):
    def test_only_an_authoritative_catalog_can_call_a_pin_stale(self):
        catalog = {"models": [{"id": "gpt-6.1-sol", "label": "x"}], "authoritative": True, "source": "codex-cache"}
        self.assertTrue(webui_credentials.model_is_stale("gpt-5-codex", catalog))
        self.assertFalse(webui_credentials.model_is_stale("gpt-6.1-sol", catalog))
        self.assertFalse(webui_credentials.model_is_stale(None, catalog))
        self.assertFalse(webui_credentials.model_is_stale("", catalog))
        self.assertFalse(webui_credentials.model_is_stale("gpt-5-codex", {**catalog, "authoritative": False}))
        self.assertFalse(webui_credentials.model_is_stale("gpt-5-codex", {"models": [], "authoritative": False}))


class KnownCliModelsTests(unittest.TestCase):
    """known_cli_models(): real model suggestions surfaced in the Settings
    panel and titlebar model-override <datalist> (message: "회사별로 cli
    모델을 리스트 형식으로 보여줘서 선택할 수 있도록 해줘"), so a user picks
    a valid id instead of typing one by hand -- which is exactly how a
    prior real-world bug report ("Sonnet5", not a valid claude alias or
    full model name) happened."""

    def test_claude_returns_the_documented_help_text_aliases(self):
        models = webui_credentials.known_cli_models("claude")
        ids = [m["id"] for m in models]
        self.assertEqual(ids, ["sonnet", "opus", "fable"])
        for m in models:
            self.assertIn("id", m)
            self.assertIn("label", m)

    def test_agy_returns_a_nonempty_static_list_that_includes_non_google_models(self):
        models = webui_credentials.known_cli_models("agy")
        self.assertTrue(models)
        for m in models:
            self.assertIn("id", m)
        ids = {m["id"] for m in models}
        self.assertTrue(any(i.startswith("claude-") for i in ids))
        self.assertTrue(any(i.startswith("gemini-") for i in ids))

    def test_removed_agy_provider_has_no_suggestions(self):
        self.assertEqual(webui_credentials.known_cli_models("gemini"), [])

    def test_unknown_provider_returns_empty_list(self):
        self.assertEqual(webui_credentials.known_cli_models("totally-unknown"), [])

    def test_codex_reads_a_real_models_cache_file(self):
        with tempfile.TemporaryDirectory() as home_dir:
            codex_dir = Path(home_dir) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
                            {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                models = webui_credentials.known_cli_models("codex")
        # "hide"-visibility entries (codex's own vocabulary for "not meant
        # to be user-facing") are filtered out; only "list" ones surface.
        self.assertEqual(models, [{"id": "gpt-5.5", "label": "GPT-5.5"}])

    def test_codex_missing_cache_file_returns_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as home_dir:
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                self.assertEqual(webui_credentials.known_cli_models("codex"), [])

    def test_codex_malformed_cache_file_returns_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as home_dir:
            codex_dir = Path(home_dir) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "models_cache.json").write_text("not json", encoding="utf-8")
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                self.assertEqual(webui_credentials.known_cli_models("codex"), [])

    def test_codex_entry_missing_slug_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as home_dir:
            codex_dir = Path(home_dir) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "models_cache.json").write_text(
                json.dumps({"models": [{"display_name": "No Slug", "visibility": "list"}]}),
                encoding="utf-8",
            )
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                self.assertEqual(webui_credentials.known_cli_models("codex"), [])


class SharedContextTests(unittest.TestCase):
    """webui_common.read_shared_context()/write_shared_context() (DEC-27):
    the Web UI's read/write path for `.handoff/shared-context.md` -- the
    same file handoff_bridge.py's build_prompt() folds into every
    prompt (see BuildPromptSharedContextTests in
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


class NoCliAvailableTests(FakeProviderPathMixin, unittest.TestCase):
    """run_provider_via_bridge() when a provider's CLI is genuinely absent.
    There is no HTTP-API fallback: the call goes through the bridge
    subprocess like any other and fails there, with a record the caller
    can still show and persist."""

    def setUp(self):
        self.setUpFakeProviders()
        self.workspace = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    def test_specific_provider_with_no_cli_fails_with_exit_127(self):
        # PATH is replaced outright (not just prepended, like
        # setUpFakeProviders() does) so the spawned handoff_bridge.py child
        # genuinely cannot find a real `claude` either -- its own
        # FileNotFoundError -> exit_code 127 handling is what is exercised.
        with mock.patch.dict(os.environ, {"PATH": str(self.fake_bin)}):
            records = webui_bridge_run.run_provider_via_bridge(threading.Lock(), self.workspace, "claude", "hello", None, "continue")
        # auto-fallback is on by default, but with no other CLI installed
        # the bridge must stop at the first failure, not retry the same one.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["exit_code"], 127)
        self.assertTrue(records[0]["reason"].startswith("tool_failure"))

    def test_auto_with_no_cli_returns_a_failure_record_never_the_auto_literal(self):
        # The /api/run handler persists record["provider"] verbatim into the
        # chat log, so the literal string "auto" must never reach it.
        with mock.patch.dict(os.environ, {"PATH": str(self.fake_bin)}):
            records = webui_bridge_run.run_provider_via_bridge(threading.Lock(), self.workspace, "auto", "hello", None, "continue")
        # No CLI at all: auto-fallback has nothing to switch to, so exactly
        # one failure record -- not the same provider twice.
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["handoff_needed"])
        self.assertNotEqual(records[0]["provider"], "auto")
        self.assertIn(records[0]["provider"], webui.PROVIDERS)


class CliProviderModelLiveServerTests(unittest.TestCase):
    """POST /api/cli-model and GET /api/providers over a real HTTP server.
    No key/validation involved here at all -- the CLI itself handles auth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = (Path(self.tmp.name) / "Agent Handoff Bridge").resolve()
        self.patcher = mock.patch("webui_common.AUTO_WORKSPACE_BASE_DIR", self.base_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # Never run the machine's real `agy models` from a test; each test
        # starts with an empty live-list cache.
        for patcher in (
            mock.patch("webui_credentials._fetch_agy_models", return_value=[]),
            mock.patch("webui_credentials._start_agy_refresh"),
            mock.patch.dict(
                webui_credentials._agy_models_cache,
                {"models": [], "ok_at": None, "failed_at": None, "refreshing": False},
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.state = webui.AppState(None)
        handler = webui.build_handler(self.state)
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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

    def test_providers_list_reports_model_source_and_staleness(self):
        with tempfile.TemporaryDirectory() as home_dir:
            codex_dir = Path(home_dir) / ".codex"
            codex_dir.mkdir()
            (codex_dir / "models_cache.json").write_text(
                json.dumps({"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"}]}),
                encoding="utf-8",
            )
            self._post("/api/cli-model", {"provider": "codex", "model": "gpt-5-codex"})
            with mock.patch("pathlib.Path.home", return_value=Path(home_dir)):
                _, data = self._get("/api/providers")
                by_name = {p["provider"]: p for p in data["providers"]}
                self.assertTrue(by_name["codex"]["model_stale"])
                self.assertEqual(by_name["codex"]["models_source"], "codex-cache")
                self._post("/api/cli-model", {"provider": "codex", "model": "gpt-6.1-sol"})
                _, data = self._get("/api/providers")
                self.assertFalse({p["provider"]: p for p in data["providers"]}["codex"]["model_stale"])
                self._post("/api/cli-model", {"provider": "codex", "model": ""})
                _, data = self._get("/api/providers")
                self.assertFalse({p["provider"]: p for p in data["providers"]}["codex"]["model_stale"])
        # claude takes full model names too, so a pinned one is never "stale"
        self._post("/api/cli-model", {"provider": "claude", "model": "claude-some-full-name"})
        _, data = self._get("/api/providers")
        self.assertFalse({p["provider"]: p for p in data["providers"]}["claude"]["model_stale"])

    def test_providers_list_uses_the_live_agy_list_when_available(self):
        live = [{"id": "gemini-9-nova-high", "label": "Gemini 9 Nova (High)"}]
        webui_credentials._agy_models_cache.update(models=live, ok_at=time.monotonic())
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "handoff_webui.cli_available", return_value=True
        ):
            _, data = self._get("/api/providers")
            agy = next(p for p in data["providers"] if p["provider"] == "agy")
            self.assertEqual(agy["known_models"], live)
            self.assertEqual(agy["models_source"], "agy-live")
            self.assertFalse(agy["models_refreshing"])
            self._post("/api/cli-model", {"provider": "agy", "model": "gemini-4-argon-medium"})
            _, data = self._get("/api/providers")
            self.assertTrue(next(p for p in data["providers"] if p["provider"] == "agy")["model_stale"])

    def test_providers_list_reports_a_refresh_in_flight_so_the_page_asks_again(self):
        with mock.patch("webui_credentials.cli_available", return_value=True), mock.patch(
            "handoff_webui.cli_available", return_value=True
        ):
            _, data = self._get("/api/providers")
        by_name = {p["provider"]: p for p in data["providers"]}
        self.assertTrue(by_name["agy"]["models_refreshing"])
        self.assertEqual(by_name["agy"]["models_source"], "built-in")  # until the lookup lands
        self.assertFalse(by_name["codex"]["models_refreshing"])
        self.assertFalse(by_name["claude"]["models_refreshing"])

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

    def test_providers_list_exposes_only_cli_fields(self):
        _, data = self._get("/api/providers")
        self.assertEqual(set(data), {"providers"})
        by_name = {p["provider"]: p for p in data["providers"]}
        self.assertEqual(set(by_name), {"codex", "claude", "agy"})
        for info in by_name.values():
            self.assertEqual(
                set(info),
                {
                    "provider",
                    "cli_detected",
                    "cli_model",
                    "known_models",
                    "models_source",
                    "models_refreshing",
                    "model_stale",
                },
            )
        # claude's list doesn't depend on any local cache file, so it's a
        # reliable non-empty check at the HTTP layer too.
        self.assertTrue(by_name["claude"]["known_models"])

    def test_removed_api_key_endpoints_are_no_longer_served(self):
        for path in ("/api/provider-key", "/api/custom-provider"):
            status, _ = self._post(path, {"provider": "claude", "key": "sk-x", "model": "m"})
            self.assertEqual(status, 405, path)

    def test_run_rejects_a_custom_provider_value(self):
        status, data = self._post("/api/run", {"provider": "custom:openrouter", "text": "hi"})
        self.assertEqual(status, 400)
        self.assertIn("invalid provider", data["error"])

    def test_empty_model_removes_a_previously_saved_one(self):
        self._post("/api/cli-model", {"provider": "agy", "model": "claude-sonnet-5-5-medium"})
        status, data = self._post("/api/cli-model", {"provider": "agy", "model": ""})
        self.assertEqual(status, 200)
        self.assertIsNone(data["model"])
        _, providers = self._get("/api/providers")
        agy = next(p for p in providers["providers"] if p["provider"] == "agy")
        self.assertIsNone(agy["cli_model"])

    def test_invalid_provider_is_rejected_with_400(self):
        status, data = self._post("/api/cli-model", {"provider": "totally-unknown", "model": "x"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


    def test_saving_one_providers_model_does_not_affect_another(self):
        self._post("/api/cli-model", {"provider": "codex", "model": "gpt-5-codex"})
        self._post("/api/cli-model", {"provider": "claude", "model": "claude-opus"})
        _, data = self._get("/api/providers")
        by_name = {p["provider"]: p for p in data["providers"]}
        self.assertEqual(by_name["codex"]["cli_model"], "gpt-5-codex")
        self.assertEqual(by_name["claude"]["cli_model"], "claude-opus")


class SharedContextLiveServerTests(unittest.TestCase):
    """GET/POST /api/shared-context (DEC-27) over a real HTTP server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = webui.AppState(self.root)
        handler = webui.build_handler(self.state)
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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
        self.httpd = webui.WebUIServer(("127.0.0.1", 0), handler)
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
