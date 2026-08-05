# Workflow Guide

Use these workflows as the normal operator playbook.

## New Project Setup

1. Confirm local tools:

   ```bash
   python3 handoff_bridge.py diagnose
   ```

2. Install shared handoff files:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project install
   ```

3. Create a task packet:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project init \
     "Describe the work" \
     --primary codex \
     --target-model "app-selected default"
   ```

4. Preview the next prompt:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project run auto \
     "Start the task"
   ```

5. Execute only when ready:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project run auto \
     --execute \
     --auto-fallback \
     "Start the task"
   ```

## Phone-Based Codex Work

1. Follow [Preflight Setup Guide](preflight-setup-guide.md).
2. Pair the host through ChatGPT desktop app **Set up Remote**.
3. Open ChatGPT mobile **Remote**.
4. Paste the header from [Agent Targeting Protocol](agent-targeting-protocol.md).
5. Name `Provider: Codex` and the visible model or `app-selected default`.
6. Ask Codex to update `.handoff/current.md` before stopping.

## Phone-Based Claude Code Work

1. Run `claude auth login`.
2. Start local Remote Control:

   ```bash
   claude remote-control --name "Project Name"
   ```

3. Open Claude app **Code**.
4. Paste the header from [Agent Targeting Protocol](agent-targeting-protocol.md).
5. Name `Provider: Claude Code`.
6. Ask Claude Code to update `.handoff/current.md` before stopping.

## Provider Handoff

Use this when one provider hits quota, auth, context, or tool limits.

1. Confirm `.handoff/current.md` has the latest summary.
2. Start or open the other provider surface.
3. Send an instruction with:

   ```text
   Instruction type: handoff
   Provider: Claude Code
   Model: app-selected default
   Continue from .handoff/current.md. Do not assume the Codex transcript is
   available.
   ```

4. Verify changed files and update the packet again.

## Review-Only Pass

Use review mode when you want a second agent to inspect work without changing
files.

```text
[작업 대상]
- Provider: Codex
- Model: app-selected default
- Account/App: OpenAI ChatGPT Remote
- Workspace: /path/to/project
- Instruction type: review
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
현재 변경사항을 리뷰만 해줘. 파일은 수정하지 말고 위험, 누락 테스트,
handoff packet 불일치를 찾아줘.
```

## Verification Pass

Use verify mode when implementation is done but checks need to be repeated.

```bash
python3 handoff_bridge.py --workspace /path/to/project check
```

Then run project-specific commands from `docs/verification-playbook.md`.

## Web UI (v0.2 chat redesign)

Use this to browse a workspace, draft attachments, and — as of Phase 1 —
actually run Codex/Claude from the v0.2 chat-style layout. Opens as a
native app window (not a browser tab) when `pywebview` is installed — this
was tested end-to-end, including a real screenshot of the rendered window,
before being called done.

```bash
pip install pywebview   # optional; auto-falls-back to a browser tab without it
python3 handoff_webui.py --workspace /path/to/project   # or omit --workspace (Phase 2)
```

`--workspace` is optional as of Phase 2: omit it and the current directory
is used automatically if it's already an initialized handoff workspace,
otherwise the app starts with no workspace selected and auto-creates one
under `~/Documents/Agent Handoff Bridge/` from your first message (or click
"폴더 직접 선택…" to pick an existing folder instead).

Click a file in the sidebar, or drag one onto the chat area, to attach it to
the draft message. Use **Open Folder** (top-left, VS Code-style) to switch
workspace at runtime instead of restarting with a different `--workspace`.
Pick a provider from the titlebar (`auto`/`codex`/`claude`/`gemini`), then **Send**
actually runs it — the first send in a browser session asks for
confirmation ("this may spend tokens"), later sends in the same session run
immediately. Every message, yours and the agent's, persists to that
workspace's own `.handoff/webui/chat/` (monthly files, older months
auto-compressed). History is per-folder: switching to a different project
shows that project's own history, not a mixed feed. Click **History**
(titlebar, top-right) to see recent activity across *every* project you've
opened, not just the current one — grouped by project, current one pinned
first, click any entry to switch straight to it (Phase 3).
See [CLI Reference § Web UI (MVP)](cli-reference.md#web-ui-mvp) for the
endpoint details and [Design System Docs](design-system/README.md) for
where this is headed.

## Contributing To This Repo

Use this when changing the bridge tool itself, not a downstream project.

1. Install the local git hooks once per clone:

   ```bash
   ./scripts/install_git_hooks.sh
   ```

2. Branch as `type/short-description` (see
   [Quality Gates](quality-gates.md)).
3. `.githooks/pre-commit` scans staged files for secrets on every commit.
4. `.githooks/pre-push` checks branch naming and runs
   `python3 handoff_bridge.py check` before the push leaves the machine.
5. The same checks run again in CI (`.github/workflows/ci.yml`) on every pull
   request, so a skipped local hook still gets caught.
