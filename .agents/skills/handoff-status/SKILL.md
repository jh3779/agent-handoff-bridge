---
name: handoff-status
description: Check this project's shared cross-agent handoff state (.handoff/current.md, provider CLI availability, saved API keys) before starting or continuing work, or when asked to check/diagnose provider or handoff status. Use when the user asks to continue a task, check what another agent already did, or troubleshoot which providers are available.
---

# Handoff Status

This project uses agent-handoff-bridge (`handoff_bridge.py`) to hand work off
between Codex CLI, Claude Code, and Gemini CLI. Before starting or continuing
any task, check the shared state so you don't repeat work another agent
already did or miss a blocker it left behind.

## Steps

1. Read `.handoff/current.md` in full -- this is the single shared source of
   truth for what has been done, by which provider, and what remains. If it
   doesn't exist, this project hasn't adopted the bridge yet (see "If not
   initialized" below).
2. Run a no-token status check:

   ```bash
   python3 handoff_bridge.py status
   ```

   Reports the last run's provider, exit code, and whether a handoff was
   needed.
3. If provider availability or API-key configuration is in question, run:

   ```bash
   python3 handoff_bridge.py diagnose
   ```

   Reports which CLIs are installed and authenticated, with no tokens spent.
4. Before stopping work, append a summary to `.handoff/current.md` (what
   changed, what was verified, what remains, any blockers) so the next
   agent -- of any provider -- can pick up cleanly. See
   `docs/shared-agent-contract.md` for the exact expected shape.

## If not initialized

If `.handoff/` doesn't exist yet, this project hasn't adopted the bridge.
Do not create `.handoff/current.md` by hand -- run the bridge's own `install`
and `init` commands (from the bridge repo, or from `handoff_bridge.py`
directly if it's already sitting in this project's root):

```bash
python3 handoff_bridge.py --workspace . install
python3 handoff_bridge.py --workspace . init "<task description>"
```

## Do not

- Do not spend provider tokens (`--execute`) just to check status --
  `status`/`diagnose`/`check` are all free, no-token commands.
- Do not hand-edit `.handoff/state.json` -- it's machine-managed. Edit
  `.handoff/current.md` instead, the human/agent-readable summary.
