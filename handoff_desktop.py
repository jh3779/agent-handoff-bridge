#!/usr/bin/env python3
"""Cross-platform desktop controller for the handoff bridge."""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError as exc:  # pragma: no cover - depends on local Python build
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    ScrolledText = None  # type: ignore[assignment]
    TKINTER_IMPORT_ERROR = exc
else:
    TKINTER_IMPORT_ERROR = None


BRIDGE_ROOT = Path(__file__).resolve().parent
BRIDGE_SCRIPT = BRIDGE_ROOT / "handoff_bridge.py"
PROVIDERS = ("auto", "codex", "claude")
PRIMARY_PROVIDERS = ("codex", "claude")
INSTRUCTION_TYPES = ("new-task", "continue", "handoff", "review", "verify")
TK_BASE = tk.Tk if tk is not None else object


def display_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def normalize_workspace(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve()


def provider_label(provider: str) -> str:
    if provider == "codex":
        return "Codex"
    if provider == "claude":
        return "Claude Code"
    return "Either"


class HandoffDesktop(TK_BASE):
    def __init__(self) -> None:
        super().__init__()
        self.title("Agent Handoff Bridge")
        self.minsize(980, 720)
        self.remote_process: subprocess.Popen[str] | None = None

        self.workspace_var = tk.StringVar(value=str(Path.cwd()))
        self.provider_var = tk.StringVar(value="auto")
        self.primary_var = tk.StringVar(value="codex")
        self.model_var = tk.StringVar(value="app-selected default")
        self.instruction_type_var = tk.StringVar(value="continue")
        self.execute_var = tk.BooleanVar(value=False)
        self.auto_fallback_var = tk.BooleanVar(value=True)
        self.remote_port_var = tk.StringVar(value="8765")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.append_log(f"Agent Handoff Bridge desktop controller on {platform.system()}.")
        self.append_log("Select a workspace, install support files, then initialize or continue a task.")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        workspace = ttk.LabelFrame(self, text="Workspace")
        workspace.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        workspace.columnconfigure(0, weight=1)
        ttk.Entry(workspace, textvariable=self.workspace_var).grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ttk.Button(workspace, text="Browse", command=self.browse_workspace).grid(row=0, column=1, padx=(0, 8), pady=8)
        ttk.Button(workspace, text="Open Packet", command=self.open_current_packet).grid(row=0, column=2, padx=(0, 8), pady=8)
        ttk.Button(workspace, text="Open Prompt", command=self.open_next_prompt).grid(row=0, column=3, padx=(0, 8), pady=8)

        target = ttk.LabelFrame(self, text="Target")
        target.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        for index in range(8):
            target.columnconfigure(index, weight=1 if index in {1, 3, 5, 7} else 0)

        ttk.Label(target, text="Provider").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=8)
        ttk.Combobox(target, textvariable=self.provider_var, values=PROVIDERS, state="readonly", width=12).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=8
        )
        ttk.Label(target, text="Primary").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=8)
        ttk.Combobox(target, textvariable=self.primary_var, values=PRIMARY_PROVIDERS, state="readonly", width=12).grid(
            row=0, column=3, sticky="ew", padx=(0, 8), pady=8
        )
        ttk.Label(target, text="Model").grid(row=0, column=4, sticky="w", padx=(0, 4), pady=8)
        ttk.Entry(target, textvariable=self.model_var).grid(row=0, column=5, sticky="ew", padx=(0, 8), pady=8)
        ttk.Label(target, text="Instruction").grid(row=0, column=6, sticky="w", padx=(0, 4), pady=8)
        ttk.Combobox(target, textvariable=self.instruction_type_var, values=INSTRUCTION_TYPES, state="readonly", width=12).grid(
            row=0, column=7, sticky="ew", padx=(0, 8), pady=8
        )

        text_area = ttk.Frame(self)
        text_area.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        text_area.columnconfigure(0, weight=1)
        text_area.columnconfigure(1, weight=1)
        text_area.rowconfigure(1, weight=1)

        ttk.Label(text_area, text="Task").grid(row=0, column=0, sticky="w")
        ttk.Label(text_area, text="Turn Prompt").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.task_text = ScrolledText(text_area, height=6, wrap="word")
        self.task_text.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(4, 0))
        self.prompt_text = ScrolledText(text_area, height=6, wrap="word")
        self.prompt_text.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(4, 0))
        self.task_text.insert("1.0", "Describe the task.")
        self.prompt_text.insert("1.0", "Continue the task.")

        actions = ttk.LabelFrame(self, text="Actions")
        actions.grid(row=3, column=0, sticky="ew", padx=12, pady=6)
        for index in range(10):
            actions.columnconfigure(index, weight=1)

        self.buttons: list[ttk.Button] = []
        self._button(actions, "Install", self.install, 0, 0)
        self._button(actions, "Init Task", self.init_task, 0, 1)
        self._button(actions, "Preview", self.preview_run, 0, 2)
        self._button(actions, "Execute", self.execute_run, 0, 3)
        self._button(actions, "Status", self.status, 0, 4)
        self._button(actions, "Diagnose", self.diagnose, 0, 5)
        self._button(actions, "Check", self.check, 0, 6)
        self._button(actions, "Copy Codex Prompt", lambda: self.copy_mobile_prompt("codex"), 1, 0)
        self._button(actions, "Copy Claude Prompt", lambda: self.copy_mobile_prompt("claude"), 1, 1)
        self._button(actions, "Save Mobile Prompts", self.save_mobile_prompts, 1, 2)
        self._button(actions, "Start Remote Preview", self.start_remote_preview, 1, 3)
        self._button(actions, "Stop Remote", self.stop_remote_preview, 1, 4)
        ttk.Checkbutton(actions, text="Execute provider", variable=self.execute_var).grid(
            row=1, column=5, sticky="w", padx=8, pady=6
        )
        ttk.Checkbutton(actions, text="Auto fallback", variable=self.auto_fallback_var).grid(
            row=1, column=6, sticky="w", padx=8, pady=6
        )
        ttk.Label(actions, text="Port").grid(row=1, column=7, sticky="e", padx=(8, 4), pady=6)
        ttk.Entry(actions, textvariable=self.remote_port_var, width=8).grid(row=1, column=8, sticky="w", padx=(0, 8), pady=6)

        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.grid(row=4, column=0, sticky="nsew", padx=12, pady=(6, 12))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = ScrolledText(log_frame, height=18, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    def _button(self, parent: ttk.Widget, label: str, command: object, row: int, column: int) -> None:
        button = ttk.Button(parent, text=label, command=command)
        button.grid(row=row, column=column, sticky="ew", padx=4, pady=6)
        self.buttons.append(button)

    def browse_workspace(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.workspace_var.get() or str(Path.cwd()))
        if selected:
            self.workspace_var.set(selected)

    def text_value(self, widget: ScrolledText) -> str:
        return widget.get("1.0", "end").strip()

    def workspace(self) -> Path:
        return normalize_workspace(self.workspace_var.get() or ".")

    def append_log(self, text: str = "") -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in self.buttons:
            button.configure(state=state)

    def bridge_command(self, args: list[str]) -> list[str]:
        return [sys.executable, str(BRIDGE_SCRIPT), "--workspace", str(self.workspace()), *args]

    def run_bridge(self, args: list[str], label: str) -> None:
        command = self.bridge_command(args)
        self.append_log()
        self.append_log(f"## {label}")
        self.append_log("$ " + display_command(command))
        self.set_busy(True)

        def worker() -> None:
            try:
                result = subprocess.run(
                    command,
                    cwd=str(BRIDGE_ROOT),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.after(0, self.finish_command, result.returncode, result.stdout, result.stderr)
            except Exception as exc:  # pragma: no cover - defensive GUI path
                self.after(0, self.finish_command, 1, "", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def finish_command(self, exit_code: int, stdout: str, stderr: str) -> None:
        if stdout:
            self.append_log(stdout.rstrip())
        if stderr:
            self.append_log(stderr.rstrip())
        self.append_log(f"Exit code: {exit_code}")
        self.set_busy(False)

    def install(self) -> None:
        self.run_bridge(["install"], "Install shared files")

    def init_task(self) -> None:
        task = self.text_value(self.task_text)
        if not task:
            messagebox.showwarning("Task required", "Enter a task before initializing.")
            return
        args = [
            "init",
            task,
            "--primary",
            self.primary_var.get(),
            "--target-model",
            self.model_var.get() or "app-selected default",
            "--instruction-type",
            self.instruction_type_var.get(),
        ]
        self.run_bridge(args, "Initialize task packet")

    def run_args(self) -> list[str]:
        prompt = self.text_value(self.prompt_text) or "Continue the task."
        args = ["run", self.provider_var.get(), "--instruction-type", self.instruction_type_var.get()]
        model = self.model_var.get().strip()
        if model:
            args.extend(["--model", model])
        if self.execute_var.get():
            args.append("--execute")
            if self.auto_fallback_var.get():
                args.append("--auto-fallback")
        args.append(prompt)
        return args

    def preview_run(self) -> None:
        self.execute_var.set(False)
        self.run_bridge(self.run_args(), "Preview provider prompt")

    def execute_run(self) -> None:
        if not messagebox.askyesno("Execute provider", "This may spend provider tokens. Continue?"):
            return
        self.execute_var.set(True)
        self.run_bridge(self.run_args(), "Execute provider run")

    def status(self) -> None:
        self.run_bridge(["status"], "Show status")

    def diagnose(self) -> None:
        self.run_bridge(["diagnose"], "Diagnose providers")

    def check(self) -> None:
        self.run_bridge(["check"], "Run no-token checks")

    def open_file(self, rel_path: str) -> None:
        path = self.workspace() / rel_path
        if not path.exists():
            messagebox.showinfo("Missing file", f"{path} does not exist yet.")
            return
        webbrowser.open(path.as_uri())

    def open_current_packet(self) -> None:
        self.open_file(".handoff/current.md")

    def open_next_prompt(self) -> None:
        self.open_file(".handoff/next-prompt.md")

    def mobile_prompt(self, provider: str) -> str:
        provider_name = provider_label(provider)
        account_app = "OpenAI ChatGPT Remote" if provider == "codex" else "Claude mobile Code"
        instruction_type = self.instruction_type_var.get()
        prompt = self.text_value(self.prompt_text) or self.text_value(self.task_text) or "Continue the task."
        return f"""[작업 대상]
- Provider: {provider_name}
- Model: {self.model_var.get() or "app-selected default"}
- Account/App: {account_app}
- Workspace: {self.workspace()}
- Instruction type: {instruction_type}
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
{prompt}

이전 대화 기록이 보인다고 가정하지 말고 현재 파일 상태와 .handoff/current.md를 기준으로 판단해줘.
작업 후 변경 파일, 검증 결과, 남은 일, 다음 안전한 행동을 .handoff/current.md에 갱신해줘.
"""

    def copy_mobile_prompt(self, provider: str) -> None:
        prompt = self.mobile_prompt(provider)
        self.clipboard_clear()
        self.clipboard_append(prompt)
        self.append_log()
        self.append_log(f"Copied {provider_label(provider)} mobile prompt to clipboard.")
        self.append_log(prompt)

    def save_mobile_prompts(self) -> None:
        handoff_dir = self.workspace() / ".handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        codex_path = handoff_dir / "mobile-codex-instruction.txt"
        claude_path = handoff_dir / "mobile-claude-instruction.txt"
        codex_path.write_text(self.mobile_prompt("codex"), encoding="utf-8")
        claude_path.write_text(self.mobile_prompt("claude"), encoding="utf-8")
        self.append_log()
        self.append_log(f"Saved {codex_path}")
        self.append_log(f"Saved {claude_path}")

    def start_remote_preview(self) -> None:
        if self.remote_process and self.remote_process.poll() is None:
            messagebox.showinfo("Remote server", "Remote preview server is already running.")
            return
        port = self.remote_port_var.get().strip() or "8765"
        command = [
            sys.executable,
            str(BRIDGE_ROOT / "remote_handoff_server.py"),
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--allow-root",
            str(self.workspace()),
            "--quiet",
        ]
        self.append_log()
        self.append_log("## Start remote preview server")
        self.append_log("$ " + display_command(command))
        try:
            self.remote_process = subprocess.Popen(
                command,
                cwd=str(BRIDGE_ROOT),
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # pragma: no cover - defensive GUI path
            messagebox.showerror("Remote server failed", str(exc))
            return
        self.append_log(f"Remote preview server started on http://127.0.0.1:{port}")
        self.append_log("Execution is disabled. Use the CLI server with --allow-execute for trusted automation.")

    def stop_remote_preview(self) -> None:
        if not self.remote_process or self.remote_process.poll() is not None:
            self.append_log("Remote preview server is not running.")
            return
        self.remote_process.terminate()
        try:
            self.remote_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.remote_process.kill()
            self.remote_process.wait(timeout=5)
        self.append_log("Remote preview server stopped.")

    def on_close(self) -> None:
        self.stop_remote_preview()
        self.destroy()


def main() -> int:
    if TKINTER_IMPORT_ERROR is not None:
        print("tkinter is not available in this Python build.")
        print("Falling back to the terminal controller.")
        print("Install a Python build with Tcl/Tk support to use the desktop GUI.")
        from handoff_control import main as control_main

        return control_main()
    app = HandoffDesktop()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
