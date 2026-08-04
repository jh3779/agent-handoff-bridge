# Design System Docs — Agent Handoff Bridge

이 폴더는 자체 완결형 HTML 디자인 문서 세트다. 시각 토큰·컴포넌트·상태
표현·화면 흐름·구현 매핑만 소유하며, 이 프로젝트가 실제로 무엇을 하는지에
대한 정본은 각 페이지에 링크된 `docs/*.md`와 실제 소스 파일이다.

**v0.2** (2026-08-03, 2026-08-04 두 차례 요구사항 반영): 데스크톱 화면을
v0.1 폼 기반 창에서 Codex/Claude 스타일 **채팅형 작업지시 화면**으로
재설계 — 사용자 요구사항이 이 재설계의 정본. v0.1 폼 UI는 실제 구현
참고용으로 각 페이지에 "레거시"로 남겨뒀다.

**MVP 구현 시작** (2026-08-04, Phase 0): `../../handoff_webui.py` +
`../../webui/`가 이 재설계의 첫 실제 동작 슬라이스다 — 사이드바 파일 트리,
드래그&amp;드롭/클릭 첨부, 대화 스레드 UI (MVP 범위: "일단 CLI를 붙이지
않고 파일 열람·삽입만"). 백엔드는 DEC-01(프레임워크 전환)을 아직
채택하지 않고 **Python 표준 라이브러리 `http.server` + 순수 JS**로 구현 —
새 의존성 없이 가장 빠르게 "실제로 테스트 가능한 것"을 만들기 위한
실용적 선택이며, DEC-01이 가리키는 최종 프로덕션 스택을 대체하는
결정은 아니다.

**Provider 연결** (2026-08-04, Phase 1): `POST /api/run`이 실제로
`handoff_bridge.py run <provider> --execute --auto-fallback`을 서브프로세스로
호출한다 — **provider 호출이 이제 있다.** DEC-02(세션당 첫 전송만 확인)와
DEC-03(코드블록 렌더링)도 이 단계에서 실제 구현됨. 가짜 `codex`/`claude`
스크립트로 auto-fallback 체인까지 토큰 소비 없이 재현해 검증. 자세한
내용은 [roadmap.md](roadmap.md) Phase 1.

**"웹이 아니라 프로그램처럼" 요구사항 반영** (2026-08-04): 위 서버를
브라우저 탭이 아니라 **네이티브 앱 창**으로 띄우도록
[pywebview](https://pywebview.flowrl.com/)를 선택적 의존성으로 추가했다
(`pip install pywebview` 시 자동 감지, 없으면 브라우저 탭으로 자동
대체). 실제로 macOS에서 `pyobjc`+WebKit 백엔드를 설치해 창을 띄우고
스크린샷으로 렌더링을 직접 확인했다 — 다크모드 토큰, 한글 텍스트, 실제
파일 트리 전부 정상 렌더링됨.

**"폴더 선택이 제대로 안 됨" 후속 반영** (2026-08-04): `--workspace`로
시작 시 한 번 고정되던 워크스페이스를 **VS Code식 Open Folder**로
런타임에 전환 가능하게 만들었다 — 네이티브 창에서는 실제 OS 폴더 선택
창(pywebview JS API), 브라우저 모드에서는 절대경로 직접 입력으로 대체.
동시에 **대화 기록을 폴더(워크스페이스) 단위로 로컬 저장** —
`.handoff/current.md`와 같은 자리(`.handoff/webui/chat/YYYY-MM.jsonl`)에
있어 프로젝트 폴더를 복사·동기화하면 대화 기록도 함께 이동한다("다른
환경에서 작업 용이"). 월이 지나면 이전 달 로그는 자동으로 gzip
압축된다. 자세한 내용은
[CLI Reference § Web UI (MVP)](../cli-reference.md#web-ui-mvp).

**자동 폴더 생성** (2026-08-04, Phase 2): `--workspace`가 이제 선택사항이다
— 생략하면 cwd가 이미 초기화된 workspace일 때만 그대로 열리고, 아니면
"워크스페이스 없음" 상태로 시작해 첫 메시지 전송 시
`~/Documents/Agent Handoff Bridge/<날짜-요약>`을 자동 생성한다(한글 요약
보존, install+init까지 실행). 구현 전 사전 인터뷰로 DEC-04~07 확정 후
착수 — 자세한 내용은 [roadmap.md](roadmap.md) Phase 2와
[flutter-mapping.html §1c](flutter-mapping.html#s1c).

**로드맵**: 지금(Phase 0~2, 완료)부터 DEC-01이 가리키는 최종 목표까지를
순서 있는 단계로 쪼갠 [roadmap.md](roadmap.md) 참고 — Provider 연결(CLI) →
자동 폴더 생성 → 멀티 프로젝트 히스토리 → API 키 모드 → Gemini →
자동 업데이트 확인 → 프레임워크 전환.

## 보는 법

브라우저로 `design-system.html`을 열면 상단 sticky 네비로 5개 페이지를
오갈 수 있다. 우측 상단 "◐ 테마" 버튼으로 라이트/다크를 전환한다(선택은
로컬에 저장됨). CDN 폰트(Pretendard/Roboto Mono/Material Symbols)를 쓰므로
인터넷 연결이 필요하다.

```bash
open docs/design-system/design-system.html   # macOS
# 또는 브라우저에서 파일을 직접 연다
```

## 페이지

| 페이지 | 내용 |
|---|---|
| [design-system.html](design-system.html) | 1 · 원칙, 색("Bridge Indigo"/"Signal Amber"), 타이포그래피, 간격/모양/고도, 모션 |
| [components.html](components.html) | 2 · §1–6 v0.1 레거시 + §8–15 v0.2 신규(사이드바 트리·메시지 버블·입력창·히스토리 항목·히스토리 그룹·Provider 연결/API 키·업데이트 배지) + 제외 목록(§16) |
| [patterns.html](patterns.html) | 3 · run 상태 전이도, 실행 상태 5종, 빈 상태, 비용 행동 확인 패턴(DEC-02 반영), 접근성 |
| [flutter-mapping.html](flutter-mapping.html) | 4 · §1 v0.1 ttk 역매핑 + §1b 기술 스택 결정(프레임워크 전환) + §1c 결정 기록(DEC-01~03) + Conflict List(미해결 10건, CFL-01 Phase 1로 해소) |
| [wireframes.html](wireframes.html) | 5 · 전체 워크플로우, **v0.2 화면 8종**(기본/드래그오버/히스토리(프로젝트별)/폴더선택/자동폴더생성/Provider 온보딩/업데이트확인), 터미널 메뉴 |

## 이 문서가 다루는 것 / 다루지 않는 것

**다룸** — 이 저장소가 실제로 소유했거나(v0.1) 소유하기로 한(v0.2) 화면:
- `handoff_desktop.py`의 데스크톱 컨트롤러 — v0.1(폼, 실제 구현) / v0.2(채팅형, 제안)
- `handoff_control.py`의 터미널 가이드 메뉴 — 이번 재설계와 무관, 변경 없음
- 위를 감싸는 전체 핸드오프 워크플로우(다운로드 → 설치 → 실행 → 핸드오프 → 검증 → 릴리즈)
- 워크스페이스 미선택 시 자동 폴더 생성, 프로젝트별 히스토리, CLI 미설치
  사용자를 위한 API 키 연결 온보딩(Gemini CLI 포함), 자동 업데이트 확인

**다루지 않음** (근거 없이 만들지 않음 — [제외 목록](components.html#s16) 참고):
- ChatGPT 모바일 Remote, Claude 모바일 Code 앱 화면 — OpenAI/Anthropic 소유
- 이 프로젝트에 존재하지 않는 웹 대시보드
- 메시지 본문 전체 마크다운 렌더링 — 코드블록만 지원하기로 결정(DEC-03)
- API 키 연결 이후의 실제 호출 아키텍처, Gemini 외 추가 모델의 화면 —
  [`../provider-extensibility.md`](../provider-extensibility.md)에 문서로만

## 정본 문서 (동작의 원천)

- [`../architecture.md`](../architecture.md) — 컴포넌트 구성, 상태 경계
- [`../workflow-guide.md`](../workflow-guide.md) — 실제 운영 워크플로우
- [`../cli-reference.md`](../cli-reference.md) — 커맨드 전체 목록
- [`../shared-agent-contract.md`](../shared-agent-contract.md) — 실패 분류 라벨
- [`../security-model.md`](../security-model.md) — 확인 절차가 필요한 행동 기준
- [`../provider-extensibility.md`](../provider-extensibility.md) — 새 AI
  provider(CLI 또는 API 키 기반)를 추가할 때 실제로 무엇이 걸리는지
- [`../webui-chat-storage.md`](../webui-chat-storage.md) — 로컬 채팅
  기록의 실제 데이터 모델(스키마·원자성·압축·git 노출 여부)
- `handoff_desktop.py`, `handoff_control.py` — v0.1 실제 구현
- **v0.2 채팅형 재설계는 코드가 아니라 사용자 요구사항이 정본**(요구사항
  원문은 [wireframes.html §REDESIGN](wireframes.html#s2)에 표로 정리)

## 결정된 것 / 아직 미해결인 것

2026-08-04 인터뷰로 12건 결정됨(v0.2 재설계 3건 + Phase 2 착수 전 사전
인터뷰 4건 + Phase 3 착수 전 사전 인터뷰 5건) — 자세한 내용은
[flutter-mapping.html §1c 결정 기록](flutter-mapping.html#s1c):

- **DEC-01**: 구현 기술 스택 = 프레임워크 전환(Tauri/Electron류). 순정
  tkinter·pywebview는 기각. *(아직 미실행 — 지금은 stdlib + pywebview로
  구현 중, CFL-14)*
- **DEC-02**: 입력창 send 버튼 = 세션당 첫 전송만 확인, 이후 즉시 실행.
  **Phase 1에서 실제 구현됨.**
- **DEC-03**: 메시지 렌더링 = 코드블록만 지원(전체 마크다운·무지원 기각).
  **Phase 1에서 실제 구현됨.**
- **DEC-04**: "워크스페이스 없음" 진입 조건 = cwd에 handoff 흔적이 없을
  때만(명시적 `--workspace` 오류는 기존처럼 즉시 에러). **Phase 2에서
  실제 구현됨.**
- **DEC-05**: 자동 생성 폴더명 = 토큰 미사용 로컬 slugify, 생성은 첫
  메시지 전송 시점으로 미룸. **Phase 2에서 실제 구현됨.**
- **DEC-06**: 자동 생성 폴더 초기 구성 = 기존 폴더 선택과 동일하게
  install+init 전부. **Phase 2에서 실제 구현됨.**
- **DEC-07**: 생성 확인 UX = 다이얼로그 없이 조용히 생성. **Phase 2에서
  실제 구현됨.**
- **DEC-08**: 히스토리 드로어 데이터 출처 = `.handoff/webui/chat/` 로그
  (CFL-16 해소).
- **DEC-09**: "최근 워크스페이스" 레지스트리 = `~/Documents/Agent Handoff
  Bridge/` 안에 저장, 최대 50개 LRU(CFL-10 잔여분 해소).
- **DEC-10**: 레지스트리 갱신 시점 = `AppState.workspace`가 설정되는
  모든 지점(Open Folder·자동생성·CLI 시작 포함).
- **DEC-11**: 그룹당 항목 5개까지, 클릭 시 워크스페이스 전환 + 해당 월
  채팅 로드(이후 정상 편집).
- **DEC-12**: auto-fallback으로 agent 메시지가 여러 개면 마지막 메시지
  기준으로 표시.

남은 미해결 사항은 [flutter-mapping.html §Conflict List](flutter-mapping.html#s2)
(CFL-03, 05, 09, 11~15)에 있다 — CFL-01은 Phase 1로, CFL-10/16은 Phase 3
착수 전 인터뷰로 완전히 해소되어 목록에서 빠졌다. 그중 무게가 큰 것:

- **CFL-09**: 프레임워크 전환(DEC-01) 시 현재 "zip 하나로 git 없이 실행"
  배포 모델이 깨진다 — 릴리즈 프로세스 자체를 다시 설계해야 함.
- **CFL-12**: API 키 연결은 UI 진입점만 설계됨 — 실제 호출 아키텍처(세션
  재개, 이벤트 파싱)는 현재 CLI 서브프로세스 구조와 근본적으로 다르다.
- **CFL-11**: 자동 업데이트 확인이 조회해야 할 GitHub Releases가 private
  저장소라 익명 조회가 안 된다.

결정 전에는 실제 코드를 바꾸지 않았다 — Phase 1·2·3도 위 결정들을 실제
코드로 옮긴 것이지 새 결정을 내린 게 아니다.
