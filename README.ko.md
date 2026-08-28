# Claude Code / Codex / Gemini CLI Handoff Bridge (한글)

이 문서는 [`README.md`](README.md)의 한글 번역본입니다. 영어 원문이
정본(source of truth)이며, 이 문서는 `docs/ko-operator-guide.md`와 같은
방식으로 별도 파일로 병기됩니다.

Claude Code CLI, Codex CLI, Gemini CLI는 현재 "남은 토큰을 공유하고
다른 CLI에서 이어서 작업"하는 단일 공식 스위치를 제공하지 않습니다. 이
저장소는 그 실용적인 버전을 위한 작은 브릿지 뼈대입니다: 작업 상태를
공유 파일에 보관하고, 세 CLI 중 어느 것이든 스크립트 가능한 모드로
실행하고, 할당량/rate/컨텍스트 실패를 감지한 뒤, 현재 워크스페이스
상태와 함께 작업을 다른 CLI로 넘깁니다. Gemini CLI는 Phase 5에서 세
번째 provider로 추가됐습니다 — Codex/Claude와의 실질적인 차이점(실제
ID로 세션 재개 불가, 무료 인증 상태 확인 없음 — 웹 UI의 API 키 모드는
v0.3.0부터 Gemini도 지원)은
[docs/research-gemini-cli.md](docs/research-gemini-cli.md) 참고.

## 다운로드

이 저장소는 공개 저장소입니다 — 아래 링크는 GitHub 계정이나 접근 권한
없이 누구나 열 수 있습니다. 받는 방법은 두 가지 독립적인 경로가
있습니다(DEC-23) — 맞는 쪽을 고르세요:

### 데스크톱 인스톨러 (GUI, Python 불필요)

| 플랫폼 | 다운로드 |
|---|---|
| 🪟 **Windows** | **[v0.4.1 인스톨러 (.exe)](https://github.com/jh3779/agent-handoff-bridge/releases/download/v0.4.1/agent-handoff-bridge_0.4.1_x64-setup.exe)** |
| 🍎 **macOS** (Apple Silicon 전용) | **[v0.4.1 dmg](https://github.com/jh3779/agent-handoff-bridge/releases/download/v0.4.1/agent-handoff-bridge_0.4.1_aarch64.dmg)** — Intel Mac은 아직 미지원 |
| 🐧 **Linux** | **[.AppImage](https://github.com/jh3779/agent-handoff-bridge/releases/download/v0.4.1/agent-handoff-bridge_0.4.1_amd64.AppImage)** |

> ⚠️ **설치 파일은 미서명입니다** (DEC-24 — 이 프로젝트 규모에서 코드
> 서명 비용은 정당화되지 않는다고 판단, 재논의 시한 없음). Windows/macOS
> 모두 첫 실행 시 경고가 뜨는 게 정상이며 악성코드 탐지가 아닙니다:
>
> - **Windows**: SmartScreen이 빨간 "Windows의 PC 보호" 화면으로 실행을
>   **차단**합니다(경고가 아니라 차단). 이 화면엔 "실행" 버튼이 없습니다
>   — 화면 안의 (버튼이 아닌) **"추가 정보"** 텍스트를 누르면 그제야
>   "실행" 버튼이 나타납니다.
> - **macOS**: Gatekeeper가 앱을 노터라이즈(Apple 공증)하지 않았다는
>   이유로 막습니다 — macOS 버전에 따라 "확인되지 않은 개발자" 또는
>   "Apple은 ... 악성 코드가 없음을 확인할 수 없습니다"로 문구가 다르게
>   뜨는데 둘 다 같은 의미입니다. Finder에서 앱을 **control+클릭(우클릭)
>   → "열기"**를 누르면 대부분 한 번만 이 경고를 넘길 수 있습니다.
>   그래도 "열기" 옵션이 안 보이면 **시스템 설정 → 개인정보 보호 및
>   보안**으로 가서(한 번 실행을 시도한 뒤에만) 화면 아래쪽에 나타나는
>   **"그래도 열기"** 버튼을 누르세요.
>
> 자세한 내용은 [Security Model](docs/security-model.md) 참고.

### 소스 zip (터미널/CLI 전용, 자신의 Python 3 필요)

`git clone` 불필요. zip을 받으세요 —
[macOS](https://github.com/jh3779/agent-handoff-bridge/releases/latest/download/agent-handoff-bridge-macos.zip)
·
[Windows](https://github.com/jh3779/agent-handoff-bridge/releases/latest/download/agent-handoff-bridge-windows.zip)
— 압축을 풀고 안에 있는 `START_HERE_MACOS.txt` / `START_HERE_WINDOWS.txt`
파일을 따라가세요. provider 토큰을 쓰지 않고 다운로드를 검증:

```bash
python3 handoff_bridge.py --version
python3 handoff_bridge.py check
```

두 명령 다 압축 해제한 상태에서 git 저장소 없이 실행됩니다. 릴리스를
어떻게 만드는지는 [docs/release-process.md](docs/release-process.md)를,
무엇이 바뀌었는지는
[docs/release-notes.md](docs/release-notes.md)(또는
[한글 번역](docs/release-notes.ko.md))를 참고하세요.

## 현재 로컬 상태

- `codex`, `claude`, `gemini` 중 최소 하나 설치, 그리고 전체 로컬
  워크플로(릴리스 업데이트 확인, `--auto-fallback`이 실제로 설치된
  셋 중 하나로 넘어감)를 위한 `gh` 필요.
- `python3 handoff_bridge.py diagnose`로 로컬 경로와 인증 상태 확인.
- Claude를 자동 fallback으로 쓰기 전에 `claude auth login` 실행. Gemini
  CLI는 무료 인증 상태 확인 명령이 없어서, `diagnose`는 설치 여부만
  알려주고 인증 여부는 알려주지 않습니다.

## 빠른 시작

작업 컨트롤러 열기:

```bash
python3 handoff_control.py
```

폴더 선택 기능이 있는 크로스플랫폼 데스크톱 컨트롤러 열기:

```bash
python3 handoff_desktop.py
```

macOS 런처:

```bash
./launchers/macos/handoff-bridge.command
```

Windows 런처:

```bat
launchers\windows\handoff-bridge.cmd
```

v0.2 채팅 스타일 재설계 사용해보기(파일 탐색, 드래그/클릭으로 첨부,
VS Code 스타일 Open Folder, 워크스페이스별 로컬 채팅 기록, Phase 1부터
실제 Codex/Claude 호출과 auto-fallback(Phase 5부터 Gemini도 세 번째
선택 가능한 provider로 합류), Phase 2부터 `--workspace`가 선택
사항이 됨(고르지 않으면 첫 메시지로부터
`~/Documents/Agent Handoff Bridge/` 아래에 폴더 자동 생성) — 그리고
Phase 3부터 현재 프로젝트뿐 아니라 열어본 모든 프로젝트의 최근 활동을
보여주는 History 드로어):

```bash
pip install pywebview   # 선택 사항, 브라우저 탭 대신 실제 앱 창을 원하면
python3 handoff_webui.py --workspace /path/to/project   # 또는 --workspace를 아예 생략
```

네이티브 앱 창으로 열립니다(`pywebview`가 없으면 자동으로 브라우저
탭으로 대체). 무엇을 하고 아직 안 하는지는
[Web UI (MVP)](docs/cli-reference.md#web-ui-mvp)를, 이게 첫 조각인 전체
재설계는 [docs/design-system/](docs/design-system/README.md)를
참고하세요.

macOS·Windows zip 패키지 빌드:

```bash
python3 scripts/package_platforms.py
```

휴대폰 기반 지시를 위해서는 공식 앱 원격 기능을 먼저 우선하세요:

- Codex: ChatGPT 모바일 앱 -> **Remote**.
- Claude Code: Claude 모바일 앱 -> **Code** 또는 `claude.ai/code`.

휴대폰 지시를 보내기 전에
[docs/preflight-setup-guide.md](docs/preflight-setup-guide.md)를
완료하고, [docs/agent-targeting-protocol.md](docs/agent-targeting-protocol.md)의
헤더를 사용하세요.

[docs/mobile-app-remote-guide.md](docs/mobile-app-remote-guide.md) 참고.

모델 토큰을 쓰지 않고 로컬 설정 점검:

```bash
python3 handoff_bridge.py diagnose
```

작업을 위한 handoff 패킷 만들기:

```bash
python3 handoff_bridge.py init "Implement the requested feature and keep tests passing" --primary codex --target-model "app-selected default"
```

다른 프로젝트 폴더에 handoff 파일 설치하기:

```bash
python3 handoff_bridge.py --workspace /path/to/project install
```

토큰을 쓰지 않고 Codex에 무엇이 전송될지 미리보기:

```bash
python3 handoff_bridge.py run codex "Start the task"
```

실제로 Codex 실행하기:

```bash
python3 handoff_bridge.py run codex --execute --instruction-type continue "Start the task"
```

다음에 실행돼야 할 provider를 돌리고, 할당량/rate/컨텍스트/인증 실패가
감지되면 브릿지가 provider를 전환하게 하기:

```bash
python3 handoff_bridge.py run auto --execute --auto-fallback --instruction-type continue "Continue the task"
```

선택한 폴더에 대해 컨트롤러로 한 번 실행하기:

```bash
python3 handoff_control.py --workspace /path/to/project "Implement the requested feature"
```

## Handoff가 작동하는 방식

- `.handoff/current.md`가 공유 작업 패킷입니다.
- `.handoff/state.json`은 provider 세션 ID와 마지막 실행 상태를
  저장합니다.
- `.handoff/runs/<timestamp>/`는 각 CLI 실행의 원시 stdout/stderr를
  저장합니다.
- `handoff_control.py`는 폴더를 고르고 작업을 지시하는 작업
  컨트롤러입니다.
- `docs/shared-agent-contract.md`는 공통 작업 방향, 품질 기준, 출력
  형태, handoff 기준을 정의합니다.
- `docs/preflight-setup-guide.md`는 원격 사용 전 계정, 호스트, 앱,
  워크스페이스 설정을 정의합니다.
- `docs/agent-targeting-protocol.md`는 모든 작업 변경과 handoff에
  쓰이는 provider/모델 헤더를 정의합니다.
- `docs/verification-playbook.md`는 공통 검증 루틴을 정의합니다.
- `schemas/handoff-summary.schema.json`은 공유되는 기계가 읽을 수
  있는 최종 요약 형태를 정의합니다.
- `AGENTS.md`는 Codex에게 공유 계약을 가리키는 영구적인 저장소 지침을
  줍니다.
- `CLAUDE.md`는 Claude Code에게 공유 계약을 가리키는 영구적인 저장소
  지침을 줍니다.
- `examples/`에는 대화형 세션에서 handoff 이벤트를 기록하기 위한
  선택적 훅 설정이 들어 있습니다.

브릿지는 의도적으로 미리보기 모드로 시작합니다. 토큰을 쓰고 싶을
때만 `--execute`를 추가하세요.

## 문서

- [문서 색인](docs/index.md)
- [플랫폼 설정](docs/platform-setup.md)
- [아키텍처](docs/architecture.md)
- [CLI 레퍼런스](docs/cli-reference.md)
- [워크플로 가이드](docs/workflow-guide.md)
- [한국어 운영자 가이드](docs/ko-operator-guide.md)
- [보안 모델](docs/security-model.md)
- [품질 게이트](docs/quality-gates.md)
- [릴리스 노트](docs/release-notes.md)

## 선택적 훅 설정

훅 예시는 기본적으로 활성화되어 있지 않습니다:

- `examples/claude-settings.handoff.json`
- `examples/codex-hooks.handoff.json`

명령을 검토한 뒤 참고용으로 사용하세요. Claude Code와 Codex 둘 다
프로젝트 훅이 실행되기 전에 훅 신뢰/검토 절차를 요구합니다.

## 선택적 커스텀 HTTP 원격

휴대폰 기반 지시에는 공식 모바일 원격 기능이 권장되는 경로입니다.
신뢰할 수 있는 자동화 실험을 위해, 이 저장소는 다음도 포함합니다:

- `remote_handoff_server.py`: 로컬 HTTP 작업 수신기.
- `remote_handoff_submit.py`: JSON 작업 제출 클라이언트.

미리보기 전용 모드로 서버 시작:

```bash
python3 remote_handoff_server.py --host 127.0.0.1 --port 8765
```

미리보기 작업 제출:

```bash
python3 remote_handoff_submit.py --workspace /path/to/project --wait "Inspect the handoff setup"
```

원격 요청이 provider 토큰을 쓰는 걸 허용할 때만 `--allow-execute`로
서버를 시작하세요.

## 정합성 검사

토큰을 쓰지 않는 검증 스위트 실행:

```bash
python3 handoff_bridge.py check
```

이 명령은 공유 계약, 문서 세트, provider 지침 파일, JSON 예시, Python
스크립트가 내부적으로 일관되는지, 추적된 비밀이 없는지, handoff 실패
분류가 계약과 일치하는지, `tests/`가 통과하는지를 확인합니다.

## 품질 게이트와 브랜치 명명

이 저장소는 브랜치 명명(`type/short-description`), 비밀 스캔, 실패
분류 일관성, 최소 유닛 테스트 기준을 강제합니다 — 전체 규칙 세트와
각각 어떻게 확인되는지는
[docs/quality-gates.md](docs/quality-gates.md) 참고. 클론당 한 번
로컬 git 훅을 설치해서 커밋/푸시 시 자동으로 실행되게 하세요:

```bash
./scripts/install_git_hooks.sh
```

같은 규칙이 모든 pull request에서 CI로도 실행됩니다
(`.github/workflows/ci.yml`).

## 리서치

출처가 있는 조사 노트와 구현 계획은
[docs/research.md](docs/research.md) 참고.
