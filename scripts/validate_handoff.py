#!/usr/bin/env python3
"""Validate the local handoff bridge without calling any model provider."""

from __future__ import annotations

import json
import argparse
import py_compile
import sys
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "handoff_bridge.py",
    "handoff_control.py",
    "remote_handoff_server.py",
    "remote_handoff_submit.py",
    "scripts/handoff_hook.py",
    "docs/index.md",
    "docs/architecture.md",
    "docs/cli-reference.md",
    "docs/workflow-guide.md",
    "docs/ko-operator-guide.md",
    "docs/security-model.md",
    "docs/release-notes.md",
    "docs/research.md",
    "docs/mobile-app-remote-guide.md",
    "docs/preflight-setup-guide.md",
    "docs/agent-targeting-protocol.md",
    "docs/shared-agent-contract.md",
    "docs/verification-playbook.md",
    "schemas/handoff-summary.schema.json",
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
    "remote_handoff_server.py",
    "remote_handoff_submit.py",
    "scripts/handoff_hook.py",
    "scripts/validate_handoff.py",
]


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
        with (root / rel_path).open(encoding="utf-8") as handle:
            json.load(handle)


def check_python(root: Path) -> None:
    for rel_path in PYTHON_FILES:
        py_compile.compile(str(root / rel_path), doraise=True)


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
    print("PASS: handoff bridge contract and validation files are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
