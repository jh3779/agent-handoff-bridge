#!/usr/bin/env python3
"""Unit tests for scripts/scan_secrets.py.

This scanner is the enforcement mechanism behind the "no secrets in tracked
files" rule in docs/quality-gates.md -- it was previously only smoke-tested
by hand, which does not survive a future edit. Run with:
python3 -m unittest discover -s tests -v

Fixture "secrets" below are assembled at runtime via _fake() rather than
written as contiguous literals, so this file's own committed source doesn't
trip scan_secrets.py's full-tree scan in `handoff_bridge.py check` / CI.
That actually happened once while writing this file -- see
docs/quality-gates.md's testing section.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan_secrets as ss  # noqa: E402


def _fake(*parts: str) -> str:
    return "".join(parts)


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
            fake_key = _fake("AKIA", "ABCDEFGHIJKLMNOP")
            (root / "leak.txt").write_text(f"aws_key = {fake_key}\n", encoding="utf-8")
            findings = ss.scan_file(root, "leak.txt")
            self.assertEqual(len(findings), 1)
            self.assertIn("aws_access_key_id", findings[0])

    def test_private_key_block_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = _fake("-----BEGIN ", "RSA PRIVATE KEY-----")
            (root / "id_rsa").write_text(f"{marker}\nMIIB...\n", encoding="utf-8")
            findings = ss.scan_file(root, "id_rsa")
            self.assertTrue(any("generic_private_key" in f for f in findings))

    def test_anthropic_key_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_key = _fake("sk-ant-", "a" * 30)
            (root / "notes.md").write_text(f"key: {fake_key}\n", encoding="utf-8")
            findings = ss.scan_file(root, "notes.md")
            self.assertTrue(any("anthropic_api_key" in f for f in findings))

    def test_generic_assigned_secret_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            line = _fake("API_KEY", ' = "', "abcdefghijklmnop1234", '"')
            (root / "config.py").write_text(line + "\n", encoding="utf-8")
            findings = ss.scan_file(root, "config.py")
            self.assertTrue(any("generic_assigned_secret" in f for f in findings))

    def test_generic_assigned_secret_unquoted_is_detected(self):
        # YAML/.env-style assignments have no surrounding quotes -- the
        # original pattern required quotes and missed these entirely.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yaml_line = _fake("password", ": ", "SuperSecretValue123456")
            env_line = _fake("PASSWORD", "=", "SuperSecretValue123456")
            (root / "config.yaml").write_text(yaml_line + "\n", encoding="utf-8")
            (root / ".env.example.txt").write_text(env_line + "\n", encoding="utf-8")
            self.assertTrue(
                any("generic_assigned_secret" in f for f in ss.scan_file(root, "config.yaml"))
            )
            self.assertTrue(
                any("generic_assigned_secret" in f for f in ss.scan_file(root, ".env.example.txt"))
            )

    def test_generic_assigned_secret_underscore_label_is_detected(self):
        # `\b(...)\b` never matched an underscore-adjacent label like
        # `db_password` because `_` is a regex word character, so there is
        # no boundary between it and the label -- even when the value is
        # quoted. Only bare or hyphenated labels used to match.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            line = _fake("db_password", ': "', "abcdefghijklmnop1234", '"')
            (root / "settings.py").write_text(line + "\n", encoding="utf-8")
            findings = ss.scan_file(root, "settings.py")
            self.assertTrue(any("generic_assigned_secret" in f for f in findings))

    def test_generic_assigned_secret_does_not_flag_ordinary_code(self):
        # Broadening the pattern to accept unquoted values must not turn it
        # into "any right-hand-side expression after a label word" -- the
        # value side still needs to look secret-shaped.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = "\n".join(
                [
                    "secret = None",
                    "token = get_token()",
                    "SECRET_KEY = os.environ['SECRET_KEY']",
                    # Regression: a real false positive hit while adding the
                    # unquoted alternative above -- a dotted attribute-access
                    # expression is long enough (17 chars) and every
                    # character was in the unquoted charset, including `.`,
                    # so it matched as if it were a secret-shaped value.
                    # Real unquoted secrets (YAML/.env-style) are never
                    # written as dotted identifiers, so excluding `.` from
                    # just the unquoted alternative loses no real coverage.
                    "token = self.server.token",
                ]
            )
            (root / "ordinary.py").write_text(lines + "\n", encoding="utf-8")
            self.assertEqual(ss.scan_file(root, "ordinary.py"), [])

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


class GitSubprocessEncodingTests(unittest.TestCase):
    """Regression coverage for a real crash class (2026-08-14, see
    handoff_bridge.py's run_provider() fix): without an explicit
    encoding, subprocess.run() falls back to
    locale.getpreferredencoding() -- not UTF-8 on a non-UTF-8-locale
    Windows machine -- to decode a git call's stdout/stderr, and file
    paths/content in a real repo can be non-ASCII (this repo's own
    Korean docs/README variants)."""

    def test_list_files_pins_utf8_encoding(self):
        with mock.patch.object(
            ss.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run_spy:
            ss.list_files(Path("/some/root"), staged_only=False)
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_spy.call_args.kwargs["errors"], "replace")

    def test_read_staged_text_pins_utf8_encoding(self):
        with mock.patch.object(
            ss.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="content", stderr=""),
        ) as run_spy:
            ss.read_staged_text(Path("/some/root"), "a.txt")
        self.assertEqual(run_spy.call_args.kwargs["encoding"], "utf-8")


class ScanIntegrationTests(unittest.TestCase):
    """Exercises scan() against a real (throwaway) git repo, since list_files()
    shells out to `git ls-files` / `git diff --cached`."""

    def test_scan_tracked_files_finds_committed_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            fake_key = _fake("AKIA", "ABCDEFGHIJKLMNOP")
            (root / "secret.txt").write_text(f"{fake_key}\n", encoding="utf-8")
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
            committed_key = _fake("AKIA", "ABCDEFGHIJKLMNOP")
            (root / "committed_secret.txt").write_text(f"{committed_key}\n", encoding="utf-8")
            run_git(root, "add", "committed_secret.txt")
            run_git(root, "commit", "-q", "-m", "add secret")

            staged_key = _fake("AKIA", "ZZZZZZZZZZZZZZZZ")
            (root / "staged_secret.txt").write_text(f"{staged_key}\n", encoding="utf-8")
            run_git(root, "add", "staged_secret.txt")

            findings = ss.scan(root, staged_only=True)
            joined = "\n".join(findings)
            self.assertIn("staged_secret.txt", joined)
            self.assertNotIn("committed_secret.txt", joined)

    def test_scan_staged_only_reads_index_not_working_tree(self):
        # Regression: scan_file() used to read staged files off disk, not
        # the git index -- if the working copy is overwritten with clean
        # content *without* re-staging, the disk-based scan would pass even
        # though the still-staged (about-to-be-committed) blob has a secret.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            fake_key = _fake("AKIA", "ABCDEFGHIJKLMNOP")
            (root / "secret.txt").write_text(f"{fake_key}\n", encoding="utf-8")
            run_git(root, "add", "secret.txt")
            # Overwrite the working copy with clean content, but do NOT
            # re-stage -- the index still holds the version with the secret.
            (root / "secret.txt").write_text("clean now\n", encoding="utf-8")

            findings = ss.scan(root, staged_only=True)
            self.assertTrue(any("secret.txt" in f for f in findings))

    def test_scan_staged_only_finds_secret_in_renamed_and_edited_file(self):
        # Regression: list_files() used --diff-filter=ACM (Added/Copied/
        # Modified), which excludes git status "R" (Renamed) -- so a file
        # that's renamed and edited in the same staged change was invisible
        # to the staged scan even though the renamed blob is what gets
        # committed. Keep the edit small so git's rename-similarity
        # detection still recognizes it as a rename rather than a delete+add.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_git(root, "init", "-q")
            run_git(root, "config", "user.email", "test@example.com")
            run_git(root, "config", "user.name", "Test")
            original = "\n".join(f"line{i}" for i in range(1, 11))
            (root / "old.txt").write_text(original + "\n", encoding="utf-8")
            run_git(root, "add", "old.txt")
            run_git(root, "commit", "-q", "-m", "add old.txt")

            run_git(root, "mv", "old.txt", "new.txt")
            fake_key = _fake("AKIA", "ABCDEFGHIJKLMNOP")
            (root / "new.txt").write_text(original + f"\n{fake_key}\n", encoding="utf-8")
            run_git(root, "add", "new.txt")

            findings = ss.scan(root, staged_only=True)
            self.assertTrue(any("new.txt" in f for f in findings))

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
