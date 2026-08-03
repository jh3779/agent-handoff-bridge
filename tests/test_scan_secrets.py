#!/usr/bin/env python3
"""Unit tests for scripts/scan_secrets.py.

This scanner is the enforcement mechanism behind the "no secrets in tracked
files" rule in docs/quality-gates.md -- it was previously only smoke-tested
by hand, which does not survive a future edit. Run with:
python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan_secrets as ss  # noqa: E402


def run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


class ScanFileTests(unittest.TestCase):
    def test_clean_file_has_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.py").write_text("print('hello world')\n", encoding="utf-8")
            self.assertEqual(ss.scan_file(root, "clean.py"), [])

    def test_aws_key_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "leak.txt").write_text("aws_key = AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
            findings = ss.scan_file(root, "leak.txt")
            self.assertEqual(len(findings), 1)
            self.assertIn("aws_access_key_id", findings[0])

    def test_private_key_block_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n", encoding="utf-8")
            findings = ss.scan_file(root, "id_rsa")
            self.assertTrue(any("generic_private_key" in f for f in findings))

    def test_anthropic_key_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("key: sk-ant-" + "a" * 30 + "\n", encoding="utf-8")
            findings = ss.scan_file(root, "notes.md")
            self.assertTrue(any("anthropic_api_key" in f for f in findings))

    def test_generic_assigned_secret_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.py").write_text('API_KEY = "abcdefghijklmnop1234"\n', encoding="utf-8")
            findings = ss.scan_file(root, "config.py")
            self.assertTrue(any("generic_assigned_secret" in f for f in findings))

    def test_banned_filename_is_flagged_regardless_of_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.json").write_text("{}", encoding="utf-8")
            findings = ss.scan_file(root, "auth.json")
            self.assertEqual(len(findings), 1)
            self.assertIn("auth.json", findings[0])

    def test_missing_file_has_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ss.scan_file(Path(tmp), "does-not-exist.txt"), [])

    def test_binary_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "binary.bin").write_bytes(b"\x00\x01\xff\xfe\x80")
            self.assertEqual(ss.scan_file(root, "binary.bin"), [])


class AllowlistTests(unittest.TestCase):
    def test_dist_path_is_allowlisted(self):
        self.assertTrue(ss.is_allowlisted("dist/agent-handoff-bridge-macos.zip"))

    def test_handoff_runs_is_allowlisted(self):
        self.assertTrue(ss.is_allowlisted(".handoff/runs/20260101T000000Z-codex/stdout.jsonl"))

    def test_regular_source_file_is_not_allowlisted(self):
        self.assertFalse(ss.is_allowlisted("handoff_bridge.py"))


class ScanIntegrationTests(unittest.TestCase):
    """Exercises scan() against a real (throwaway) git repo, since list_files()
    shells out to `git ls-files` / `git diff --cached`."""

    def test_scan_tracked_files_finds_committed_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            (root / "secret.txt").write_text("AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
            run_git(root, "add", "secret.txt")
            run_git(root, "commit", "-q", "-m", "add secret")

            findings = ss.scan(root, staged_only=False)
            self.assertTrue(any("secret.txt" in f for f in findings))

    def test_scan_staged_only_ignores_committed_secret_and_sees_new_staged_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            (root / "committed_secret.txt").write_text("AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
            run_git(root, "add", "committed_secret.txt")
            run_git(root, "commit", "-q", "-m", "add secret")

            (root / "staged_secret.txt").write_text("AKIAZZZZZZZZZZZZZZZZ\n", encoding="utf-8")
            run_git(root, "add", "staged_secret.txt")

            findings = ss.scan(root, staged_only=True)
            joined = "\n".join(findings)
            self.assertIn("staged_secret.txt", joined)
            self.assertNotIn("committed_secret.txt", joined)

    def test_scan_clean_repo_has_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            (root / "readme.md").write_text("# Hello\n", encoding="utf-8")
            run_git(root, "add", "readme.md")
            run_git(root, "commit", "-q", "-m", "init")

            self.assertEqual(ss.scan(root, staged_only=False), [])


if __name__ == "__main__":
    unittest.main()
