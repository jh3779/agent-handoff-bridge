# Agent Targeting Protocol

Use this when giving instructions from the phone, desktop app, or CLI. The
target provider and model must be explicit whenever the task changes, work is
handed off, or you are not sure which agent is currently active.

## Required Header

```text
[작업 대상]
- Provider: Codex | Claude Code | Either
- Model: <exact model name, app-selected default, or unknown>
- Account/App: OpenAI ChatGPT Remote | Claude Code Remote Control | CLI bridge
- Workspace: <absolute project path or project name shown in app>
- Instruction type: new-task | continue | handoff | review | verify
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md
```

## Required Instruction Body

```text
[지시]
<what should be done>

[검증]
<commands/checks expected, or "use docs/verification-playbook.md">

[종료 전]
.handoff/current.md를 Changed, Verified, Remaining, Blocked, Next 형식으로 업데이트해줘.
```

## Codex Mobile Example

```text
[작업 대상]
- Provider: Codex
- Model: app-selected default
- Account/App: OpenAI ChatGPT Remote
- Workspace: /path/to/my-app
- Instruction type: continue
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
이전 작업을 이어서 구현해줘. 현재 파일 상태와 git status를 먼저 확인해.

[검증]
프로젝트의 기존 테스트/빌드 명령을 우선 사용하고, 없으면 변경 파일에 맞는 최소 검증을 실행해.

[종료 전]
.handoff/current.md를 Changed, Verified, Remaining, Blocked, Next 형식으로 업데이트해줘.
```

## Claude Mobile Example

```text
[작업 대상]
- Provider: Claude Code
- Model: app-selected default
- Account/App: Claude Code Remote Control
- Workspace: /path/to/my-app
- Instruction type: handoff
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
Codex가 남긴 handoff packet을 읽고 남은 작업만 이어서 진행해.

[검증]
docs/verification-playbook.md 기준으로 필요한 검증을 수행해.

[종료 전]
.handoff/current.md를 Changed, Verified, Remaining, Blocked, Next 형식으로 업데이트해줘.
```

## Model Field Rules

- If you choose a model in the app UI, write that visible model name.
- If the app chooses automatically, write `app-selected default`.
- If the bridge command uses `--model`, write that exact model string.
- In the bridge, `app-selected default`, `provider default`, `default`, and
  `unknown` are recording labels only; they are not passed as provider model
  overrides.
- If you do not know the current active model, write `unknown` and ask the
  agent to report its provider/model if the surface exposes it.

## Handoff Rule

When switching providers, the new instruction must explicitly say who should
continue:

```text
Instruction type: handoff
Provider: Claude Code
Model: app-selected default
Continue from .handoff/current.md. Do not assume the Codex transcript is
available.
```
