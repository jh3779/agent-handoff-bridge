#!/usr/bin/env python3
"""Enforce the repo's branch naming convention: `type/short-description`.

Allowed types and the exact pattern are documented in
docs/quality-gates.md. Keep this list and that document in sync.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_TYPES = (
    "feature",
    "fix",
    "docs",
    "chore",
    "refactor",
    "test",
    "release",
    "hotfix",
)

# type/lowercase-kebab-case-description, e.g. fix/state-json-race
BRANCH_PATTERN = re.compile(r"^(" + "|".join(ALLOWED_TYPES) + r")/[a-z0-9]+(?:-[a-z0-9]+)*$")

# Branches that are never expected to follow the convention.
EXEMPT_BRANCHES = ("main", "master")


def current_branch(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if branch == "HEAD":
        # Detached HEAD (e.g. a CI checkout of a merge commit). Nothing to
        # validate; the branch-name gate belongs to whoever pushed the ref.
        return None
    return branch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the current (or given) branch name.")
    parser.add_argument("branch", nargs="?", help="Branch name to check. Defaults to the current git branch.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Repository root.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    branch = args.branch or current_branch(root)

    if branch is None:
        print("SKIP: no branch to validate (detached HEAD or not a git checkout).")
        return 0
    if branch in EXEMPT_BRANCHES:
        print(f"PASS: '{branch}' is exempt from the branch naming rule.")
        return 0
    if BRANCH_PATTERN.match(branch):
        print(f"PASS: '{branch}' matches type/short-description.")
        return 0

    print(f"FAIL: branch '{branch}' does not match the required pattern.")
    print(f"Expected: <type>/<short-kebab-case-description>, where type is one of: {', '.join(ALLOWED_TYPES)}")
    print("Example: fix/state-json-race")
    return 1


if __name__ == "__main__":
    sys.exit(main())
