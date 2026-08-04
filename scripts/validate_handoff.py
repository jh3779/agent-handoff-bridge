#!/usr/bin/env python3
"""Validate the local handoff bridge without calling any model provider."""

from __future__ import annotations

import json
import argparse
import py_compile
import subprocess
import sys
import unittest
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "handoff_bridge.py",
    "handoff_control.py",
    "handoff_desktop.py",
    "remote_handoff_server.py",
    "remote_handoff_submit.py",
    "scripts/handoff_hook.py",
    "scripts/package_platforms.py",
    "scripts/scan_secrets.py",
    "scripts/check_branch_name.py",
    "docs/index.md",
    "docs/architecture.md",
    "docs/cli-reference.md",
    "docs/workflow-guide.md",
    "docs/ko-operator-guide.md",
    "docs/platform-setup.md",
    "docs/security-model.md",
    "docs/release-notes.md",
    "docs/research.md",
    "docs/mobile-app-remote-guide.md",
    "docs/preflight-setup-guide.md",
    "docs/agent-targeting-protocol.md",
    "docs/shared-agent-contract.md",
    "docs/verification-playbook.md",
    "docs/quality-gates.md",
    "docs/release-process.md",
    "docs/provider-extensibility.md",
    "docs/webui-chat-storage.md",
    "schemas/handoff-summary.schema.json",
    "launchers/macos/handoff-bridge.command",
    "launchers/macos/install.sh",
    "launchers/windows/handoff-bridge.cmd",
    "launchers/windows/handoff-bridge.ps1",
    "launchers/windows/install.ps1",
    "handoff_webui.py",
    "webui/index.html",
    "webui/app.css",
    "webui/app.js",
    "tests/__init__.py",
    "tests/test_handoff_bridge.py",
    "tests/test_scan_secrets.py",
    "tests/test_check_branch_name.py",
    "tests/test_handoff_webui.py",
    ".handoff/current.md",
    ".handoff/task-template.md",
]

JSON_FILES = [
    "examples/claude-settings.handoff.json",
    "examples/codex-hooks.handoff.json",
    "schemas/handoff-summary.schema.json",
]

PYTHON_FILES = [
    "handoff_bridge.py",
    "handoff_control.py",
    "handoff_desktop.py",
    "remote_handoff_server.py",
    "remote_handoff_submit.py",
    "scripts/handoff_hook.py",
    "scripts/validate_handoff.py",
    "scripts/package_platforms.py",
    "scripts/scan_secrets.py",
    "scripts/check_branch_name.py",
    "handoff_webui.py",
    "tests/test_handoff_bridge.py",
    "tests/test_scan_secrets.py",
    "tests/test_check_branch_name.py",
    "tests/test_handoff_webui.py",
]

# Must match HANDOFF_LABELS in handoff_bridge.py and the enum documented in
# docs/shared-agent-contract.md's "Start Of Turn Checklist" section.
HANDOFF_CLASSIFICATION_LABELS = (
    "quota",
    "rate_limit",
    "auth",
    "billing",
    "context_limit",
    "overloaded",
    "tool_failure",
    "unknown",
)


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def check_required_files(root: Path) -> None:
    for rel_path in REQUIRED_FILES:
        path = root / rel_path
        if not path.exists():
            fail(f"missing required file: {rel_path}")
        if path.is_file() and path.stat().st_size == 0:
            fail(f"empty required file: {rel_path}")


def check_contract_references(root: Path) -> None:
    expected = "docs/shared-agent-contract.md"
    for rel_path in ["AGENTS.md", "CLAUDE.md", "handoff_bridge.py", ".handoff/current.md"]:
        text = (root / rel_path).read_text(encoding="utf-8")
        if expected not in text:
            fail(f"{rel_path} does not reference {expected}")

    playbook = "docs/verification-playbook.md"
    for rel_path in ["AGENTS.md", "CLAUDE.md", "handoff_bridge.py", ".handoff/current.md"]:
        text = (root / rel_path).read_text(encoding="utf-8")
        if playbook not in text:
            fail(f"{rel_path} does not reference {playbook}")

    targeting = "docs/agent-targeting-protocol.md"
    for rel_path in ["handoff_bridge.py", ".handoff/current.md", "docs/shared-agent-contract.md"]:
        text = (root / rel_path).read_text(encoding="utf-8")
        if targeting not in text:
            fail(f"{rel_path} does not reference {targeting}")


def check_json(root: Path) -> None:
    for rel_path in JSON_FILES:
        path = root / rel_path
        if not path.exists():
            fail(f"missing JSON file: {rel_path}")
        with path.open(encoding="utf-8") as handle:
            json.load(handle)


def check_python(root: Path) -> None:
    for rel_path in PYTHON_FILES:
        py_compile.compile(str(root / rel_path), doraise=True)


def check_failure_classification(root: Path) -> None:
    """Keep handoff_bridge.py's failure vocabulary in sync with the contract.

    docs/shared-agent-contract.md defines the canonical set of handoff failure
    labels agents must use. handoff_bridge.py's classifier must recognize
    every one of them, or a run can be misclassified in a way that neither
    document nor code will otherwise catch.
    """
    bridge_text = (root / "handoff_bridge.py").read_text(encoding="utf-8")
    contract_text = (root / "docs/shared-agent-contract.md").read_text(encoding="utf-8")
    for label in HANDOFF_CLASSIFICATION_LABELS:
        if label not in contract_text:
            fail(f"docs/shared-agent-contract.md is missing the '{label}' handoff label")
        if label not in bridge_text:
            fail(f"handoff_bridge.py does not recognize the '{label}' handoff label")


def check_secrets(root: Path) -> None:
    scanner = root / "scripts" / "scan_secrets.py"
    result = subprocess.run(
        [sys.executable, str(scanner), "--root", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(f"secret scan failed:\n{result.stdout}{result.stderr}")


def check_tests(root: Path) -> None:
    """Run the repo's minimum unit test suite (tests/, stdlib unittest)."""
    tests_dir = root / "tests"
    if not tests_dir.exists() or not any(tests_dir.glob("test_*.py")):
        fail("tests/ directory has no test_*.py files")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(tests_dir), top_level_dir=str(root))
    if suite.countTestCases() == 0:
        fail("no test cases discovered under tests/")
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=0)
    result = runner.run(suite)
    if not result.wasSuccessful():
        fail("unit tests failed; see output above")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate handoff bridge files.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Workspace root to validate.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    check_required_files(root)
    check_contract_references(root)
    check_json(root)
    check_python(root)
    check_failure_classification(root)
    check_secrets(root)
    check_tests(root)
    print("PASS: handoff bridge contract and validation files are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
