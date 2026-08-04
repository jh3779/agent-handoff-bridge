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
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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

    def test_open_folder_switches_workspace(self):
        status, data = self._post("/api/open-folder", {"path": str(self.other)})
        self.assertEqual(status, 200)
        self.assertEqual(Path(data["workspace"]), self.other.resolve())

        status, tree = self._get("/api/tree?path=")
        self.assertEqual(status, 200)
        names = {e["name"] for e in tree["entries"]}
        self.assertEqual(names, {"NOTES.md"})

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


if __name__ == "__main__":
    unittest.main()
