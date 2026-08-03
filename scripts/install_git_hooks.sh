#!/bin/sh
# Point git at the repo-tracked hooks in .githooks/ so branch naming, secret
# scanning, and the validation suite run locally instead of only in CI.
# See docs/quality-gates.md for what each hook checks and why.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

chmod +x .githooks/pre-commit .githooks/pre-push
git config core.hooksPath .githooks

echo "Installed git hooks from .githooks/ (core.hooksPath set for this repo)."
echo "Windows: run this from Git Bash, which ships with Git for Windows."
