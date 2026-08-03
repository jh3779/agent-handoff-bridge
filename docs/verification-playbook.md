# Verification Playbook

Use this playbook for both Codex CLI and Claude Code CLI. The exact commands may
change by project, but the verification shape should stay consistent.

## No-Token Bridge Checks

These checks do not call a model:

```bash
python3 handoff_bridge.py check
python3 handoff_bridge.py diagnose
python3 handoff_bridge.py run auto "Preview next handoff prompt"
```

`handoff_bridge.py check` now also runs a secret scan, verifies the handoff
failure classification matches `docs/shared-agent-contract.md`, and runs
`tests/`. See [Quality Gates](quality-gates.md) for the full rule set,
including branch naming (checked separately, not part of `check` — it is a
convention for this repo, not for downstream projects that install it).

## Generic Project Checks

Run the narrowest relevant checks first:

- Python scripts changed: `python3 -m py_compile <files>`
- JSON changed: `python3 -m json.tool <file>`
- Markdown-only change: inspect rendered structure or run available markdown
  lint if the project has one
- Shell scripts changed: run available shellcheck if present, otherwise inspect
  quoting and exit behavior manually
- Application code changed: use the repository's documented lint/test/build
  commands

If a check is unavailable, record `not run` with the reason.

## Review Checklist

- The task goal is stated in `.handoff/current.md`.
- `AGENTS.md` and `CLAUDE.md` both point to the shared contract.
- `docs/preflight-setup-guide.md` exists for account, host, workspace, and app
  setup.
- `docs/agent-targeting-protocol.md` exists for provider/model routing.
- Mobile remote workflows are documented in `docs/mobile-app-remote-guide.md`
  when phone-based instructions are part of the setup.
- The latest task or handoff instruction names the target provider and model.
- The bridge prompt includes the shared contract, current handoff packet, and
  git snapshot.
- Raw model/provider logs are ignored by git.
- Secrets or auth tokens are not present in tracked files — enforced by
  `scripts/scan_secrets.py`, not just a manual review step.
- The final summary follows `Changed`, `Verified`, `Remaining`, `Blocked`,
  `Next`.
- Machine-readable summaries conform to `schemas/handoff-summary.schema.json`
  when that mode is requested.

## Acceptance Rubric

Use this rubric before stopping:

- Correctness: the requested work is actually satisfied.
- Continuity: the other provider can continue from files alone.
- Minimality: unrelated changes are absent.
- Reproducibility: verification commands are recorded.
- Safety: secrets, destructive commands, and broad permissions are avoided.

If any item fails, either fix it or record it as a blocker with a concrete next
step.
