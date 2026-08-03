# Mobile App Remote Guide

Date: 2026-08-03

Use official mobile remote features first. A custom HTTP server is only for
special automation cases. For normal phone-based work direction, Codex and
Claude already provide mobile control surfaces.

Before sending work from a phone, complete `docs/preflight-setup-guide.md` and
use the instruction header in `docs/agent-targeting-protocol.md`.

## Codex From A Phone

Use the ChatGPT mobile app's **Remote** tab to control supported Codex chats
running from a connected host.

### What Runs Where

- Phone: sends prompts, follow-ups, approvals, and decisions.
- Connected host: provides projects, files, credentials, permissions, plugins,
  browser setup, terminal output, diffs, tests, screenshots, and local tools.
- Secure relay: keeps trusted machines reachable without exposing the host
  directly to the public internet.

### Setup

1. Install or update the ChatGPT mobile app on iOS or Android.
2. Install or update the ChatGPT desktop app on the Mac or Windows host.
3. Sign in to the same ChatGPT account and workspace on both devices.
4. On the host, open the ChatGPT desktop app and choose **Set up Remote**.
5. Scan the QR code with the phone and finish pairing.
6. Open **Remote** in the ChatGPT mobile app.
7. Select the host and start or continue a Codex chat.

### Practical Notes

- Codex is not a normal selectable web/mobile chat mode. Use the mobile
  **Remote** tab for supported desktop Codex chats.
- Keep the host awake, online, signed in, and able to access the project.
- If the project lives on an SSH devbox, connect the desktop app host to that
  SSH environment first, then use the phone to control the host.
- Use this repository's `AGENTS.md`, `.handoff/current.md`,
  `docs/shared-agent-contract.md`, and `docs/verification-playbook.md` inside
  the selected project so phone instructions follow the same standards.

## Claude Code From A Phone

Use the Claude app's **Code** tab or `claude.ai/code` to reach Claude Code
sessions. The mobile app is a client; code runs either in Anthropic-managed
cloud sessions or in a local Claude Code session through Remote Control.

### Option A: Cloud Sessions

Use this when:

- the repository is on GitHub;
- the task should continue after the laptop is closed;
- you do not need local-only files, private tools, or local MCP servers.

From the Claude app, open **Code**, choose a repository and branch, describe the
task, and submit it. You can check progress, answer questions, and steer the
session from the phone.

### Option B: Remote Control For Local Sessions

Use this when:

- the work needs your local filesystem;
- local tools, credentials, MCP servers, or project configuration matter;
- you want phone control while execution stays on your machine.

Setup:

1. Run `claude` in the project directory once and accept workspace trust.
2. Sign in with `claude.ai` auth. API-key-only auth is not enough for Remote
   Control.
3. From the project directory, run:

   ```bash
   claude remote-control --name "Project Name"
   ```

   Or start an interactive session with:

   ```bash
   claude --remote-control "Project Name"
   ```

4. Use the shown URL or QR code, or open the Claude app and tap **Code**.
5. Pick the online session and send instructions from the phone.

### Practical Notes

- Remote Control is a local-session window: code execution and filesystem
  access stay on your machine.
- The local machine must stay awake and connected.
- On Team and Enterprise plans, an owner may need to enable Remote Control.
- Some terminal-only commands do not work from mobile.
- Attachments sent from the Claude app can be downloaded to the local machine
  and passed to Claude Code as file references.

## Recommended Workflow With This Handoff Setup

1. On the target project folder, install the shared standards:

   ```bash
   python3 <bridge-repo>/handoff_bridge.py --workspace <project> install
   ```

2. Create a task packet:

   ```bash
   python3 <bridge-repo>/handoff_bridge.py --workspace <project> init "<task>"
   ```

3. Start the relevant desktop/local remote session:

   - Codex: ChatGPT desktop app -> **Set up Remote** -> phone **Remote** tab.
   - Claude: `claude remote-control --name "<project>"` -> phone **Code** tab.

4. From the phone, instruct the agent to follow:

   - `.handoff/current.md`
   - `docs/agent-targeting-protocol.md`
   - `docs/shared-agent-contract.md`
   - `docs/verification-playbook.md`

5. Include the target provider and model in the instruction header.
6. Ask it to update `.handoff/current.md` before stopping.

## When To Use The Custom Bridge Instead

Use `handoff_bridge.py` and `handoff_control.py` when:

- you want provider fallback between Codex CLI and Claude Code CLI;
- you want JSON/JSONL logs under `.handoff/runs/`;
- you want dry-run previews before spending tokens;
- you are running from the terminal rather than an official mobile remote
  session.

Use official mobile app remote control when:

- you are simply trying to monitor, steer, approve, or start work from your
  phone;
- you want the provider's supported auth, relay, notifications, and approval UI.

## Sources

- OpenAI: [Work with Codex from anywhere](https://openai.com/index/work-with-codex-from-anywhere/)
- OpenAI: [ChatGPT Work and Codex](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)
- OpenAI Codex manual: Remote connections
- Anthropic: [Claude Code on mobile](https://code.claude.com/docs/en/mobile)
- Anthropic: [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control)
- Anthropic: [Claude app intents, shortcuts, and widgets on iOS](https://support.claude.com/en/articles/10263469-use-claude-app-intents-shortcuts-and-widgets-on-ios)
