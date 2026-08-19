#!/usr/bin/env python3
"""Unit tests for scripts/build_updater_manifest.py (DEC-28)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_updater_manifest as bum  # noqa: E402


def _write_artifact(installers_dir: Path, target_triple: str, subdir: str, filename: str, signature: str) -> None:
    bundle_dir = installers_dir / f"installers-{target_triple}" / subdir
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / filename).write_bytes(b"fake bundle bytes")
    (bundle_dir / f"{filename}.sig").write_text(signature, encoding="utf-8")


class BuildManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.installers_dir = Path(self._tmp.name)

    def _write_all_three(self):
        _write_artifact(self.installers_dir, "x86_64-pc-windows-msvc", "nsis", "agent-handoff-bridge_0.4.0_x64-setup.exe", "windows-sig-content")
        _write_artifact(self.installers_dir, "aarch64-apple-darwin", "macos", "agent-handoff-bridge.app.tar.gz", "macos-sig-content")
        _write_artifact(self.installers_dir, "x86_64-unknown-linux-gnu", "appimage", "agent-handoff-bridge_0.4.0_amd64.AppImage", "linux-sig-content")

    def test_manifest_has_all_three_platform_keys(self):
        self._write_all_three()
        manifest = bum.build_manifest(self.installers_dir, "0.4.0", "some notes", "v0.4.0")
        self.assertEqual(set(manifest["platforms"]), {"windows-x86_64", "darwin-aarch64", "linux-x86_64"})

    def test_signature_is_the_sig_files_exact_content(self):
        self._write_all_three()
        manifest = bum.build_manifest(self.installers_dir, "0.4.0", "", "v0.4.0")
        self.assertEqual(manifest["platforms"]["windows-x86_64"]["signature"], "windows-sig-content")
        self.assertEqual(manifest["platforms"]["darwin-aarch64"]["signature"], "macos-sig-content")
        self.assertEqual(manifest["platforms"]["linux-x86_64"]["signature"], "linux-sig-content")

    def test_download_url_points_at_the_given_tag_and_real_filename(self):
        self._write_all_three()
        manifest = bum.build_manifest(self.installers_dir, "0.4.0", "", "v0.4.0")
        self.assertEqual(
            manifest["platforms"]["windows-x86_64"]["url"],
            "https://github.com/jh3779/agent-handoff-bridge/releases/download/v0.4.0/agent-handoff-bridge_0.4.0_x64-setup.exe",
        )

    def test_top_level_fields_are_set_correctly(self):
        self._write_all_three()
        manifest = bum.build_manifest(self.installers_dir, "0.4.0", "fixed a bug", "v0.4.0")
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertEqual(manifest["notes"], "fixed a bug")
        self.assertIn("pub_date", manifest)
        # ISO 8601 with a literal "Z" suffix, matching Tauri's documented format.
        self.assertTrue(manifest["pub_date"].endswith("Z"))

    def test_missing_installer_directory_raises_a_clear_error(self):
        # Only wrote windows/macos, not linux.
        _write_artifact(self.installers_dir, "x86_64-pc-windows-msvc", "nsis", "app.exe", "sig")
        _write_artifact(self.installers_dir, "aarch64-apple-darwin", "macos", "app.app.tar.gz", "sig")
        with self.assertRaises(FileNotFoundError) as ctx:
            bum.build_manifest(self.installers_dir, "0.4.0", "", "v0.4.0")
        self.assertIn("linux", str(ctx.exception))

    def test_missing_sig_file_raises_a_clear_error(self):
        bundle_dir = self.installers_dir / "installers-x86_64-pc-windows-msvc" / "nsis"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "app.exe").write_bytes(b"unsigned bundle, no .sig next to it")
        _write_artifact(self.installers_dir, "aarch64-apple-darwin", "macos", "app.app.tar.gz", "sig")
        _write_artifact(self.installers_dir, "x86_64-unknown-linux-gnu", "appimage", "app.AppImage", "sig")
        with self.assertRaises(FileNotFoundError) as ctx:
            bum.build_manifest(self.installers_dir, "0.4.0", "", "v0.4.0")
        self.assertIn(".sig", str(ctx.exception))

    def test_two_matching_bundle_files_is_an_error_not_a_silent_pick(self):
        _write_artifact(self.installers_dir, "x86_64-pc-windows-msvc", "nsis", "one.exe", "sig1")
        _write_artifact(self.installers_dir, "x86_64-pc-windows-msvc", "nsis", "two.exe", "sig2")
        with self.assertRaises(RuntimeError):
            bum.find_one(self.installers_dir / "installers-x86_64-pc-windows-msvc" / "nsis", "*.exe")


if __name__ == "__main__":
    unittest.main()
