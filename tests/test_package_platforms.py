#!/usr/bin/env python3
"""Unit tests for scripts/package_platforms.py.

Regression coverage for an audit finding (production-audit, 2026-09-02):
package_files() included docs/*.md (top-level only, non-recursive) but not
README.ko.md or docs/design-system/** -- while README.md and docs/index.md
both link to README.ko.md, docs/design-system/README.md, and
docs/design-system/roadmap.md. A source-zip user following either link hit a
missing file even though `python3 handoff_bridge.py check` still passed
(check has no opinion on doc-link completeness).

Run with: python3 -m unittest tests.test_package_platforms -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import package_platforms as pp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class PackagedDocLinksTests(unittest.TestCase):
    def setUp(self):
        self.packaged = set(pp.package_files())

    def test_readme_ko_is_packaged(self):
        self.assertIn(ROOT / "README.ko.md", self.packaged)

    def test_design_system_readme_is_packaged(self):
        self.assertIn(ROOT / "docs" / "design-system" / "README.md", self.packaged)

    def test_design_system_roadmap_is_packaged(self):
        self.assertIn(ROOT / "docs" / "design-system" / "roadmap.md", self.packaged)

    def test_design_system_html_assets_are_packaged(self):
        # design-system/README.md itself links onward to these pages --
        # bundling just the *.md would leave those links broken too.
        for name in ("wireframes.html", "components.html", "patterns.html", "styles.css"):
            with self.subTest(name=name):
                self.assertIn(ROOT / "docs" / "design-system" / name, self.packaged)

    def test_every_packaged_file_actually_exists(self):
        for path in self.packaged:
            with self.subTest(path=path):
                self.assertTrue(path.exists(), f"listed for packaging but missing on disk: {path}")


if __name__ == "__main__":
    unittest.main()
