#!/usr/bin/env python3
"""Scan tracked (or staged) text files for likely secrets.

No-token check used by `handoff_bridge.py check`, `.githooks/pre-commit`, and
CI. It is intentionally pattern-based rather than exhaustive: the goal is to
catch obvious accidents (a pasted API key, a private key block, an auth.json
file) before they land in git history, not to replace a dedicated secret
scanner for a real production project.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# Paths that are expected to hold example/placeholder secrets or binary/
# generated content that should not be scanned.
PATH_ALLOWLIST = (
    ".git/",
    "dist/",
    "__pycache__/",
    ".handoff/runs/",
)

# (label, pattern) pairs. Patterns are matched per-line so a finding can be
# reported with a line number.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "generic_assigned_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*"
            r"['\"][A-Za-z0-9/+_.=-]{12,}['\"]"
        ),
    ),
]

# Filenames that should never be tracked, independent of their content.
BANNED_FILENAMES = (
    "auth.json",
    ".env",
    "credentials.json",
)


def is_allowlisted(rel_path: str) -> bool:
    return any(rel_path == p.rstrip("/") or rel_path.startswith(p) for p in PATH_ALLOWLIST)


def list_files(root: Path, staged_only: bool) -> list[str]:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged_only else ["git", "ls-files"]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def scan_file(root: Path, rel_path: str) -> list[str]:
    findings: list[str] = []
    name = Path(rel_path).name
    if name in BANNED_FILENAMES:
        findings.append(f"{rel_path}: filename is never allowed in tracked content ({name})")
        return findings

    path = root / rel_path
    if not path.exists() or not path.is_file():
        return findings
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return findings

    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{rel_path}:{lineno}: possible {label}")
    return findings


def scan(root: Path, staged_only: bool) -> list[str]:
    findings: list[str] = []
    for rel_path in list_files(root, staged_only):
        if is_allowlisted(rel_path):
            continue
        findings.extend(scan_file(root, rel_path))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan tracked or staged files for likely secrets.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Repository root to scan.")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan only staged files (for pre-commit) instead of every tracked file.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).expanduser().resolve()
    findings = scan(root, args.staged)
    if findings:
        print("FAIL: possible secrets found:")
        for finding in findings:
            print(f"  - {finding}")
        print(
            "If this is a false positive, rename the value or exclude the path in "
            "scripts/scan_secrets.py's PATH_ALLOWLIST."
        )
        return 1
    print("PASS: no likely secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
