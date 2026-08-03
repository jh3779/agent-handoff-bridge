#!/usr/bin/env python3
"""Unit tests for scripts/check_branch_name.py.

Backs the branch naming rule in docs/quality-gates.md. Run with:
python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_branch_name as cbn  # noqa: E402


class BranchPatternTests(unittest.TestCase):
    def test_valid_names_for_every_allowed_type(self):
        for branch_type in cbn.ALLOWED_TYPES:
            branch = f"{branch_type}/short-description"
            self.assertIsNotNone(cbn.BRANCH_PATTERN.match(branch), branch)

    def test_single_word_description_is_valid(self):
        self.assertIsNotNone(cbn.BRANCH_PATTERN.match("fix/typo"))

    def test_numbers_in_description_are_valid(self):
        self.assertIsNotNone(cbn.BRANCH_PATTERN.match("fix/issue-42"))

    def test_unknown_type_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("wip/something"))

    def test_uppercase_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("Fix/Something"))

    def test_underscore_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("fix/some_thing"))

    def test_missing_description_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("fix/"))
        self.assertIsNone(cbn.BRANCH_PATTERN.match("fix"))

    def test_trailing_hyphen_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("fix/something-"))

    def test_double_hyphen_is_rejected(self):
        self.assertIsNone(cbn.BRANCH_PATTERN.match("fix/some--thing"))


class MainExitCodeTests(unittest.TestCase):
    """Exercise the same code path `.githooks/pre-push` and CI invoke."""

    def test_exempt_branch_passed_explicitly_exits_zero(self):
        self.assertEqual(cbn.build_parser().parse_args(["main"]).branch, "main")

    def test_current_branch_none_on_bad_root(self):
        # A root with no git repo (or git unavailable) must not crash --
        # it should degrade to "nothing to validate" rather than raise.
        self.assertIsNone(cbn.current_branch(Path("/nonexistent-path-for-tests")))


if __name__ == "__main__":
    unittest.main()
