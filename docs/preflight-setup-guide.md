# Preflight Setup Guide

Use this before giving work instructions from a phone, desktop app, or CLI. The
goal is to make both Codex and Claude Code see the same workspace, standards,
and routing information.

## 1. Account Setup

### Codex

- Sign in to the ChatGPT desktop app with the OpenAI account/workspace that has
  Codex access.
- Confirm Codex Local is available in that workspace.
- Pair the phone through ChatGPT desktop app **Set up Remote**.
- On the phone, use ChatGPT mobile app **Remote**.

### Claude Code

- Run `claude auth login` on the host machine.
- Confirm `claude auth status --text` reports a logged-in account.
- For local mobile control, start:

  ```bash
  claude remote-control --name "<project name>"
  ```

- On the phone, use the Claude app **Code** tab or `claude.ai/code`.

## 2. Host Setup

- Keep the host awake, online, and signed in.
- Confirm the selected project folder exists on the host.
- Confirm the host has the project dependencies, credentials, MCP servers, and
  local tools needed for the task.
- If the project is on SSH, connect the desktop app or Claude Code session to
  the SSH environment before sending phone instructions.

## 3. Workspace Setup

Install the shared handoff files into the target project:

```bash
python3 <bridge-repo>/handoff_bridge.py --workspace <project> install
```

Create or refresh the task packet:

```bash
python3 <bridge-repo>/handoff_bridge.py --workspace <project> init "<task>" --primary codex --target-model "<model or app-selected default>"
```

Run no-token checks:

```bash
python3 <bridge-repo>/handoff_bridge.py --workspace <project> check
```

## 4. Instruction Setup

Before sending a phone instruction, decide:

- target provider: `Codex`, `Claude Code`, or `Either`;
- target model: the exact selected model if known, otherwise
  `app-selected default`;
- instruction type: `new-task`, `continue`, `handoff`, `review`, or `verify`;
- workspace folder;
- expected verification.

Use `docs/agent-targeting-protocol.md` for the exact instruction header.

## 5. Stop Rule

Every agent must update `.handoff/current.md` before stopping. The update should
include:

- changed files or behavior;
- verification run;
- remaining work;
- blockers;
- next safe action.
