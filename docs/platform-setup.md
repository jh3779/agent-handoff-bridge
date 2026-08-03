# Platform Setup

The bridge uses one Python codebase and separate launchers for macOS and
Windows. Both versions open the same desktop controller and use the same
handoff files.

## Shared Requirements

- Python 3. Standard `tkinter` support is needed for the GUI; without it, the
  launcher falls back to the terminal controller.
- Git for workspace inspection.
- Optional but recommended: GitHub CLI `gh`.
- Codex CLI available as `codex` when Codex CLI execution is needed.
- Claude Code CLI available as `claude` when Claude Code execution is needed.

If a provider CLI is unavailable on a machine, the desktop app can still create
handoff packets, mobile prompts, previews, and documentation-driven task state.

## macOS Version

Use the Finder-friendly launcher:

```bash
./launchers/macos/install.sh
./launchers/macos/handoff-bridge.command
```

The macOS launcher:

- resolves the repo root relative to the launcher location;
- runs `handoff_desktop.py` with `python3`;
- opens a GUI where the operator can choose a workspace folder.

If macOS blocks the file because it was downloaded from the internet, approve
it from System Settings or run it once from Terminal.

## Windows Version

Use the Command Prompt launcher for the least friction:

```bat
launchers\windows\handoff-bridge.cmd
```

PowerShell users can run:

```powershell
powershell -ExecutionPolicy Bypass -File .\launchers\windows\install.ps1
.\launchers\windows\handoff-bridge.ps1
```

The Windows launchers:

- resolve the repo root relative to the launcher location;
- prefer the `py -3` launcher when available;
- fall back to `python`;
- open the same GUI workspace controller.

## Build Zip Packages

Create both platform packages:

```bash
python3 scripts/package_platforms.py
```

Outputs:

- `dist/agent-handoff-bridge-macos.zip`
- `dist/agent-handoff-bridge-windows.zip`

The generated packages include both launcher folders so the validation command
continues to work after unzipping.

## Desktop Controller

The GUI supports:

- folder selection for the active workspace;
- provider, primary provider, model, and instruction type selection;
- install, init, preview, execute, status, diagnose, and check actions;
- mobile prompt generation for Codex Remote and Claude Code;
- preview-only local HTTP remote server startup.

Execution is still explicit. The app asks before provider execution and the
remote preview server does not spend provider tokens.

## Mobile Handoff Usage

Use the GUI to copy a Codex or Claude mobile prompt, then paste it into:

- ChatGPT mobile app **Remote** for Codex;
- Claude mobile app **Code** or `claude.ai/code` for Claude Code.

Every prompt includes:

- provider;
- model;
- account or app surface;
- workspace path;
- instruction type;
- source-of-truth files.

## Contributing To This Repo

If you are changing this repo itself (not just installing it into a
project), install the local git hooks once per clone:

```bash
./scripts/install_git_hooks.sh
```

This enforces branch naming and runs the secret scan / validation suite
automatically on commit and push. See [Quality Gates](quality-gates.md).

## Verification

Run this from the repo root after changing platform files:

```bash
python3 handoff_bridge.py check
python3 -m py_compile handoff_desktop.py handoff_bridge.py handoff_control.py scripts/package_platforms.py
```

On macOS, also run:

```bash
zsh -n launchers/macos/install.sh
zsh -n launchers/macos/handoff-bridge.command
```
