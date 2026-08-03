# 한국어 운영 가이드

이 문서는 Claude Code CLI와 Codex CLI를 같은 작업 기준으로 운용하고,
한쪽이 토큰, 할당량, 인증, 컨텍스트 문제로 멈췄을 때 다른 쪽이
자연스럽게 이어받도록 지시하는 실무용 안내입니다.

## 기본 원칙

- 작업 기준은 `.handoff/current.md`, `docs/shared-agent-contract.md`,
  `docs/verification-playbook.md`를 기준으로 통일합니다.
- 작업을 새로 시작하거나 넘겨줄 때는 항상 대상 에이전트와 모델을
  명시합니다.
- 에이전트 개인 대화 기록은 공유된다고 가정하지 않습니다.
- 이어받는 에이전트는 파일, git 상태, handoff 문서를 보고 판단합니다.
- 멈추기 전에는 반드시 `.handoff/current.md`를 갱신하게 합니다.

## 사전 셋팅

1. 로컬 상태 확인:

   ```bash
   python3 handoff_bridge.py diagnose
   ```

2. 작업할 프로젝트 폴더에 공통 파일 설치:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project install
   ```

3. 작업 패킷 생성:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project init \
     "작업 내용을 적어주세요" \
     --primary codex \
     --target-model "app-selected default"
   ```

4. 토큰을 쓰지 않고 다음 지시문 미리보기:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project run auto \
     "작업을 시작해줘"
   ```

5. 실제 실행:

   ```bash
   python3 handoff_bridge.py --workspace /path/to/project run auto \
     --execute \
     --auto-fallback \
     "작업을 이어서 진행해줘"
   ```

## 휴대폰에서 Codex에 지시

1. Mac 또는 Windows에서 ChatGPT 데스크톱 앱을 열고 Codex 원격 연결을
   준비합니다.
2. 휴대폰 ChatGPT 앱에서 **Remote**를 엽니다.
3. 연결된 호스트와 작업 세션을 선택합니다.
4. 아래 지시 템플릿을 붙여넣습니다.

```text
[작업 대상]
- Provider: Codex
- Model: app-selected default
- Account/App: OpenAI ChatGPT Remote
- Workspace: /path/to/project
- Instruction type: continue
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
현재 작업을 이어서 진행해줘. 이전 대화 기록을 사용할 수 있다고 가정하지 말고,
workspace 파일과 .handoff/current.md를 기준으로 판단해줘.
작업 후 검증 결과와 남은 일을 .handoff/current.md에 갱신해줘.
```

## 휴대폰에서 Claude Code에 지시

1. 로컬에서 Claude Code 로그인 상태를 확인합니다.

   ```bash
   claude auth status --text
   ```

2. 프로젝트 폴더에서 원격 제어를 시작합니다.

   ```bash
   claude remote-control --name "Project Name"
   ```

3. 휴대폰 Claude 앱의 **Code** 또는 `claude.ai/code`에서 세션을 엽니다.
4. 아래 지시 템플릿을 붙여넣습니다.

```text
[작업 대상]
- Provider: Claude Code
- Model: app-selected default
- Account/App: Claude mobile Code
- Workspace: /path/to/project
- Instruction type: handoff
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
Codex가 이어서 진행하지 못한 작업을 넘겨받아 진행해줘.
Codex의 대화 기록이 보인다고 가정하지 말고, 현재 파일 상태와
.handoff/current.md를 기준으로 작업해줘.
작업 후 변경 파일, 검증 결과, 남은 일을 .handoff/current.md에 갱신해줘.
```

## 작업 변경 시 지시 형식

작업을 바꾸거나 중간에 지시를 추가할 때도 같은 헤더를 사용합니다.

```text
[작업 대상]
- Provider: Codex 또는 Claude Code
- Model: app-selected default 또는 실제 모델명
- Account/App: 사용하는 앱 또는 계정
- Workspace: /path/to/project
- Instruction type: new-task | continue | handoff | review | verify
- Source of truth: .handoff/current.md, docs/shared-agent-contract.md, docs/verification-playbook.md

[지시]
원하는 작업 내용을 구체적으로 작성합니다.
```

## 넘겨받기 기준

다음 상황이면 다른 에이전트로 넘깁니다.

- 토큰 또는 사용량 한도 도달
- rate limit
- 인증 또는 결제 문제
- 컨텍스트 길이 초과
- 모델 출력 중단
- 도구 실행 실패가 반복됨

넘겨받는 에이전트에게는 `Instruction type: handoff`를 사용하고,
마지막 작업자가 `.handoff/current.md`를 갱신했는지 먼저 확인하게 합니다.

## 검증 기준

기본 검증:

```bash
python3 handoff_bridge.py check
```

프로젝트별 검증은 `docs/verification-playbook.md`에 적습니다. 예를 들면:

- 테스트 명령
- 린트 명령
- 타입 검사
- 빌드 확인
- 수동 확인 항목

검증을 못 했으면 이유와 다음 확인 방법을 `.handoff/current.md`에 남깁니다.
