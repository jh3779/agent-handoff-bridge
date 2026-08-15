#!/usr/bin/env python3
"""Interactive controller for issuing tasks through the handoff bridge."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from handoff_bridge import PROVIDERS as BRIDGE_PROVIDERS
from handoff_bridge import normalize_path


BRIDGE_SCRIPT = Path(__file__).resolve().parent / "handoff_bridge.py"
# Derived from handoff_bridge.PROVIDERS (the canonical set, currently
# codex/claude/gemini) instead of a stale local copy -- see handoff_webui.py,
# which already imports PROVIDERS the same way. "auto" is a menu/CLI-only
# concept added on top; handoff_bridge.py itself has no "auto" provider.
PROVIDERS = ("auto",) + BRIDGE_PROVIDERS


def resolve_workspace(raw_path: str, create: bool = False) -> Path:
    path = normalize_path(raw_path)
    if path.exists() and path.is_dir():
        return path
    if path.exists():
        raise ValueError(f"not a directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
        return path
    answer = input(f"Create folder {path}? [y/N] ").strip().lower()
    if answer != "y":
        raise ValueError(f"workspace does not exist: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_provider(default: str = "auto") -> str:
    choices = "/".join(PROVIDERS)
    while True:
        value = ask(f"Provider ({choices})", default).lower()
        if value in PROVIDERS:
            return value
        print(f"Choose one of: {', '.join(PROVIDERS)}")


def ask_model(default: str = "app-selected default") -> str:
    return ask("Model (exact model, app-selected default, or blank)", default)


def confirm_execute() -> bool:
    answer = input("This will call a model provider and may spend tokens. Continue? [y/N] ").strip().lower()
    return answer == "y"


def run_bridge(workspace: Path, bridge_args: list[str]) -> int:
    command = [sys.executable, str(BRIDGE_SCRIPT), "--workspace", str(workspace), *bridge_args]
    print()
    print("$ " + " ".join(str(part) for part in command), flush=True)
    print(flush=True)
    return subprocess.run(command, check=False).returncode


def select_workspace(initial: str | None = None) -> Path:
    default = initial or str(Path.cwd())
    while True:
        raw = ask("Workspace folder", default)
        try:
            return resolve_workspace(raw)
        except ValueError as exc:
            print(exc)


def initialize_task(workspace: Path) -> int:
    task = ask("Task instruction")
    if not task:
        print("Task instruction is required.")
        return 1
    provider = ask_provider("codex")
    model = ask_model()
    return run_bridge(workspace, ["init", task, "--primary", provider, "--target-model", model])


def run_with_prompt(workspace: Path, args_prefix: list[str], prompt: str) -> int:
    """Run the bridge with the turn prompt passed via --prompt-file rather
    than a trailing argv positional: a bare positional appended after
    --instruction-type <value> parses inconsistently across argparse/Python
    versions (works on 3.14, fails as "unrecognized argument" on 3.11) --
    see handoff_webui.py's --prompt-file invocation for the same fix.
    """
    fd, prompt_path_str = tempfile.mkstemp(prefix="handoff-control-prompt-", suffix=".txt")
    prompt_path = Path(prompt_path_str)
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
        return run_bridge(workspace, [*args_prefix, "--prompt-file", str(prompt_path)])
    finally:
        prompt_path.unlink(missing_ok=True)


def preview_run(workspace: Path) -> int:
    provider = ask_provider("auto")
    model = ask_model("")
    prompt = ask("Turn prompt", "Continue the task")
    args = ["run", provider, "--instruction-type", "continue"]
    if model:
        # "--model=value", not ["--model", value]: with the latter, a model
        # string that happens to start with "-" would make argparse treat
        # it as the next flag instead of --model's value.
        args.append(f"--model={model}")
    return run_with_prompt(workspace, args, prompt)


def execute_run(workspace: Path) -> int:
    provider = ask_provider("auto")
    model = ask_model("")
    prompt = ask("Turn prompt", "Continue the task")
    if not confirm_execute():
        print("Cancelled.")
        return 0
    args = ["run", provider, "--execute", "--auto-fallback", "--instruction-type", "continue"]
    if model:
        args.append(f"--model={model}")
    return run_with_prompt(workspace, args, prompt)


def menu(workspace: Path) -> int:
    while True:
        print()
        print(f"Workspace: {workspace}")
        print("1. Install shared handoff files")
        print("2. Create new task packet")
        print("3. Preview next provider prompt")
        print("4. Execute provider run")
        print("5. Show handoff status")
        print("6. Diagnose providers")
        print("7. Run no-token checks")
        print("8. Change workspace")
        print("0. Quit")
        choice = ask("Choose", "3")

        if choice == "1":
            run_bridge(workspace, ["install"])
        elif choice == "2":
            initialize_task(workspace)
        elif choice == "3":
            preview_run(workspace)
        elif choice == "4":
            execute_run(workspace)
        elif choice == "5":
            run_bridge(workspace, ["status"])
        elif choice == "6":
            run_bridge(workspace, ["diagnose"])
        elif choice == "7":
            run_bridge(workspace, ["check"])
        elif choice == "8":
            workspace = select_workspace(str(workspace))
        elif choice == "0":
            return 0
        else:
            print("Unknown choice.")


def run_once(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace or str(Path.cwd()), create=True)
    run_bridge(workspace, ["install"])
    target_model = args.model or "app-selected default"
    run_bridge(workspace, ["init", args.task, "--primary", args.primary, "--target-model", target_model])
    bridge_args = ["run", args.provider]
    if args.execute:
        if not args.yes and not confirm_execute():
            print("Cancelled.")
            return 0
        bridge_args.extend(["--execute", "--auto-fallback"])
    bridge_args.extend(["--instruction-type", "new-task"])
    if args.model:
        bridge_args.append(f"--model={args.model}")
    return run_with_prompt(workspace, bridge_args, args.prompt or "Start the task")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Choose a folder and issue a handoff task.")
    parser.add_argument("--workspace", "-W", help="Workspace folder to work in.")
    parser.add_argument("--provider", choices=PROVIDERS, default="auto", help="Provider to run for the task.")
    parser.add_argument("--primary", choices=BRIDGE_PROVIDERS, default="codex", help="Primary provider for new packets.")
    parser.add_argument("--execute", action="store_true", help="Actually call the provider after creating the task.")
    parser.add_argument("--yes", action="store_true", help="Skip execute confirmation.")
    parser.add_argument("--model", help="Model override or exact app-selected model label.")
    parser.add_argument("--prompt", default="", help="Turn prompt for the provider run.")
    parser.add_argument("task", nargs="?", help="If supplied, run once instead of opening the menu.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.task:
        return run_once(args)
    workspace = select_workspace(args.workspace)
    return menu(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
