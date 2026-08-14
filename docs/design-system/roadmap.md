# v0.2 Roadmap — MVP to Final Goal

**최종 목표**: [flutter-mapping.html DEC-01](flutter-mapping.html#s1b)이 가리키는
프로덕션 앱 — 프레임워크 전환(Tauri/Electron류) 스택 위에서, Codex/Claude/
Gemini를 실제로 호출하고, 워크스페이스를 자유롭게 오가며, 여러 프로젝트의
대화 기록을 한 곳에서 훑어보고, 최신 버전을 스스로 확인하는 채팅형
에이전트 클라이언트.

**지금 상태 (Phase 0 · Phase 1 · Phase 2 · Phase 3 완료)**: `handoff_webui.py` +
`webui/` — 파일 브라우징, 드래그/클릭 첨부, VS Code식 Open Folder,
워크스페이스별 로컬 채팅 기록(월별 gzip 압축), 네이티브 창(pywebview,
선택적 의존성), **실제 Codex/Claude 호출**(`POST /api/run`,
auto-fallback 포함), **워크스페이스 미선택 시 자동 폴더 생성**
(첫 메시지 전송 시 `~/Documents/Agent Handoff Bridge/<날짜-요약>`을
생성해 install+init까지 실행), 그리고 **멀티 프로젝트 히스토리 드로어**
(최근에 연 워크스페이스를 앱 레벨 레지스트리로 기억, 클릭 시 즉시 전환).
`python3 -m unittest discover -s tests -v`로 커버됨 — 정확한 테스트 개수는
드리프트하기 쉬우므로(리뷰에서 지적된 문서 간 불일치 참고) 여기 고정 숫자로
적지 않는다; 실행 결과를 신뢰하라.

이 문서는 Phase 0과 최종 목표 사이를 순서가 있는 단계로 쪼갠다. 각 단계는
[Conflict List](flutter-mapping.html#s2)의 특정 항목을 해소하는 것을
목표로 하며, 정본은 여전히 그 Conflict List다 — 여기서는 실행 순서와
근거만 정리한다.

## 순서를 정한 기준

1. **가치가 큰 것 먼저**: "파일만 보는 도구"를 "진짜 에이전트와 대화하는
   도구"로 만드는 Phase 1이 최우선 — 나머지는 전부 그 위의 부가 기능.
2. **이미 있는 코드를 재사용할 수 있는 것 먼저**: `handoff_bridge.py`가
   이미 구현한 CLI 호출·핸드오프 분류 로직을 재사용하는 단계(Phase 1)가
   API 키를 새로 설계해야 하는 단계(Phase 4)보다 앞선다.
3. **비싸고 되돌리기 어려운 것은 마지막**: 프레임워크 전환(Phase 7)은
   기능이 계속 바뀌는 지금 시점에 하면 이중 작업이 된다. 저비용 스택
   (stdlib + pywebview) 위에서 기능을 먼저 검증하고, 안정된 기능 집합을
   한 번에 옮기는 편이 싸다.
4. **서로 독립적인 것은 순서를 유연하게**: Phase 2(자동 폴더 생성)와
   Phase 6(업데이트 확인)은 다른 단계에 의존하지 않는다 — 우선순위가
   바뀌어도 안전하게 끼워 넣을 수 있다.

## Phase 1 — Provider 연결 (CLI 모드) ✅ 완료

**목표**: 채팅에서 실제로 Codex/Claude CLI를 호출하고 응답을 받는다.

**실제로 한 것**:
- `POST /api/run` 신설. 단, `handoff_bridge.py`의 `run_provider()`를
  프로세스 내에서 직접 호출하지 않고 **서브프로세스로 실행**하도록
  설계를 한 단계 구체화했다 — 그 함수가 `.handoff/state.json` 같은 경로를
  프로세스 cwd 기준 상대경로로 푸는데(`chdir_workspace()`),
  `ThreadingHTTPServer`의 요청 스레드에서 직접 부르면 `os.chdir()`이
  프로세스 전역이라 동시 요청끼리 서로의 cwd를 덮어쓸 수 있기 때문.
  `handoff_desktop.py`가 이미 쓰는 것과 같은 패턴(서브프로세스 + `--workspace`)을
  그대로 따랐다. 실행 후 `.handoff/state.json`의 `history[]`를
  실행 전/후로 diff해서 새로 추가된 레코드(fallback 발생 시 2개 이상)를
  구조화된 데이터로 돌려받는다. 자세한 이유는
  [CLI Reference § Web UI (MVP)](../cli-reference.md#web-ui-mvp)의
  "Why a subprocess" 참고.
- DEC-02(세션당 첫 전송만 확인, 이후 즉시 실행) 적용 —
  `webui/app.js`의 `sessionRunConfirmed` 플래그.
- 상태 배지(완료/핸드오프 필요/실패)를 실제 메시지에 붙였다 —
  `classify_run_status()`가 `handoff_bridge.py`의 `classify_handoff()`
  결과를 세 상태로 매핑.
- auto-fallback 발생 시 전환된 provider의 응답이 **별도 agent 메시지로**
  스레드에 그대로 나타난다(시스템 메시지로 요약하는 대신, 실제 두 번째
  provider의 실행 결과 자체를 보여줌 — 원래 계획보다 더 직접적인 해결).
- 코드블록 렌더링(DEC-03)도 함께 구현 — 계획대로 Phase 1에 묶었다.

**검증**: 가짜 `codex`/`claude` 스크립트를 `PATH`에 얹어 실제
서브프로세스 호출·`--auto-fallback` 체인(2개 provider)·HTTP 왕복까지
전부 실제로 돌려서 확인(`RunProviderViaBridgeTests`,
`ApiRunLiveServerTests`) — 토큰 소비나 네트워크 없이 결정적으로 재현.

**해소한 항목**: CFL-01, CFL-03, DEC-02/DEC-03 실제 적용.

## Phase 2 — 워크스페이스 미선택 시 자동 폴더 생성 ✅ 완료

**목표**: [SCR-05](wireframes.html#s7)를 실제 코드로 — 폴더를 고르지 않고
바로 메시지를 보내면 `~/Documents/Agent Handoff Bridge/<날짜-요약>`을
자동 생성해 워크스페이스로 쓴다.

작고 독립적이라 다른 단계와 순서를 바꿔도 안전하다. 온보딩 마찰을 줄이는
효과가 커서 Phase 1 직후에 배치했다.

**설계 확정** (2026-08-04, 구현 전 사전 인터뷰 — 3라운드에 걸쳐 질문 8개를
물어 아래 **결정 4건**(DEC-04~07)으로 확정, 자세한 내용은
[flutter-mapping.html DEC-04~07](flutter-mapping.html#s1c)):
- 진입 조건: `--workspace` 미지정 + cwd에 기존 handoff 흔적(`.handoff/`)이
  없을 때(이미 install/init된 폴더는 지금처럼 바로 열림). 명시적으로 준
  `--workspace` 경로가 없으면 기존처럼 즉시 에러(DEC-04, 2차 수정 — 최초
  안 "cwd 무효할 때만"은 cwd가 사실상 항상 존재해 거의 발동 안 하는 문제
  발견 후 교정).
- 폴더명: 토큰 미사용 로컬 slugify, 생성은 **첫 메시지 전송 시점**으로
  미룸(버튼-먼저/메시지-먼저 두 경로가 하나로 수렴), 충돌 시 숫자 접미사
  (DEC-05).
- 초기 구성: 기존 폴더 선택과 동일하게 `install`+`init` 전부 실행
  (DEC-06).
- 확인 UX: 다이얼로그 없이 조용히 생성 + 시스템 메시지 안내 — 토큰
  소비가 없는 순수 로컬 작업이라 DEC-02의 대상이 아님(DEC-07).

**실제로 한 것**:
- `AppState.workspace`가 `Path | None`이 됨 — `--workspace` 미지정 시
  기본값을 무조건 cwd로 해석하던 것을 그만두고, `resolve_startup_workspace()`
  (순수 함수, `main()`에서 호출)가 DEC-04를 그대로 코드로 옮겼다: 명시적
  `--workspace`는 기존과 동일하게 엄격히 검증, 무지정 시엔
  `has_handoff_marker()`로 cwd의 `.handoff/` 존재 여부만 확인.
- `workspace is None`일 때 모든 GET 엔드포인트가 죽지 않고 우아하게
  응답하도록 손봤다 — `/api/info`는 `{workspace: null}`,
  `/api/tree`/`/api/chat`은 빈 목록, `/api/file`/`/api/run`은 명확한 400.
- 실제 생성은 `create_workspace_for_first_message()` — `POST /api/chat`의
  `role: "user"` 경로에서만(즉 실제 사용자 메시지에서만) 호출된다.
  `slugify_for_folder_name()`은 토큰 없이 로컬에서 만들며 `\w`가
  유니코드를 인식해 한글도 그대로 보존(전형적인 ASCII 전용 slugify와
  다른 점, DEC-05가 요구한 그대로). 이름 충돌은 숫자 접미사로 처리.
  스캐폴딩은 `handoff_bridge.py --workspace <새 폴더> init "<첫 메시지>"`를
  서브프로세스로 호출해서 처리 — `run_provider_via_bridge()`와 같은
  chdir-안전성 이유로 인프로세스 호출 대신 서브프로세스를 그대로 따랐다.
  `init`이 기본으로 `install`까지 하므로 DEC-06이 요구한 "전부 실행"이
  한 번의 호출로 해결됨.
- "새 폴더 자동 생성" 버튼(`webui/app.js`)은 실제로 아무것도 만들지
  않는다 — DEC-05대로 composer에 포커스만 주고, 생성 자체는 첫 메시지
  전송(`POST /api/chat`) 시점까지 미룬다. "폴더 직접 선택…" 버튼은
  기존 Open Folder 로직(`pickFolder()`로 추출해 공유)을 그대로 재사용.
- 자동 생성 직후 프론트엔드가 `/api/info`+`/api/tree`를 다시 불러와
  타이틀바·파일 트리를 새 워크스페이스로 갱신(DEC-07: 확인 다이얼로그
  없이 조용히).

**검증**: `python3 -m unittest discover -s tests -v`에 28개 테스트 추가
(순수 함수 단위 테스트 + `AUTO_WORKSPACE_BASE_DIR`을 임시 디렉터리로
패치한 생성 테스트 + `AppState(None)`으로 띄운 실제 라이브 서버 테스트).
실제 프로세스로도 확인 — `$HOME`을 임시 디렉터리로 바꿔치기해 실제
`~/Documents/`를 건드리지 않고 서버를 띄운 뒤 curl로 전체 흐름(빈
워크스페이스 → 한글 메시지 전송 → 자동 생성된 폴더에 `.handoff/state.json`
존재 확인)을 검증.

**해소한 항목**: SCR-05 실제 구현, DEC-04(2차 수정)/05/06/07 실제 적용.

## Phase 3 — 멀티 프로젝트 히스토리 드로어 ✅ 완료

**목표**: [SCR-03](wireframes.html#s5)의 "여러 프로젝트를 한 드로어에서"를
완성한다. Phase 0에서 이미 워크스페이스별 저장(`.handoff/webui/chat/`)은
끝났으므로, 남은 건 "최근에 연 워크스페이스 목록"을 앱 레벨에 기억하는
레지스트리와 그걸 보여주는 드로어 UI뿐이다.

**선행 질문**(착수 전 해소됨): 이 드로어가 "채팅 메시지"(Phase 0의
`.handoff/webui/chat/`)와 "provider 실행 세션"(Phase 1의
`.handoff/runs/`) 중 무엇을 보여줄지 [CFL-16](flutter-mapping.html#s2)이
미결정이었다 — Phase 1이 끝나 둘 다 실제로 존재하게 된 뒤에 결정하는
편이 낫다고 보고 Phase 3을 Phase 1 뒤에 배치했었다.

**설계 확정** (2026-08-04, 구현 전 사전 인터뷰 5건 →
[flutter-mapping.html DEC-08~12](flutter-mapping.html#s1c)):
- 데이터 출처: `.handoff/webui/chat/` 로그(원래 가정이던 provider 실행
  이력 `.handoff/runs/`+`state.json` `history[]` 기각) — 사용자가 실제
  입력한 문장은 채팅 로그에만 있음(DEC-08).
- 레지스트리: `~/Documents/Agent Handoff Bridge/` 안에 저장(Phase 2가
  확립한 "앱 소유 위치" 재사용, OS별 app-data 경로 기각), 최대 50개
  LRU, 더 존재하지 않는 폴더는 렌더링 시 조용히 걸러냄(DEC-09).
- 갱신 시점: `AppState.workspace`가 설정되는 모든 지점 — Open
  Folder·자동생성뿐 아니라 `--workspace`로 시작하는 CLI 기동도 포함
  (DEC-10).
- 그룹당 항목: 워크스페이스당 최근 5개까지만. 클릭 시: 워크스페이스
  전환 + 해당 월 채팅 로드, 이후 정상 편집 가능(진짜 읽기 전용 뷰어
  기각, DEC-11).
- auto-fallback으로 한 turn에 agent 메시지가 여러 개면 마지막 메시지
  (최종 provider/상태) 기준으로 표시(DEC-12).

**해소하는 항목**: [CFL-10](flutter-mapping.html#s2) 완전 해소,
[CFL-16](flutter-mapping.html#s2) 완전 해소 — 둘 다 Conflict List에서
제거됨.

**실제로 한 것**:
- `registry_path()`가 함수인 이유: `~/Documents/Agent Handoff Bridge/registry.json`
  경로를 모듈 로드 시점에 상수로 고정하면 테스트가
  `AUTO_WORKSPACE_BASE_DIR`를 임시 디렉터리로 패치해도 반영되지 않아
  실제 `~/Documents/`를 건드릴 위험이 있었다 — 구현 중 직접 발견해
  함수로 바꿈.
- `touch_registry()`를 `AppState.workspace`가 설정되는 3곳 전부에 연결:
  `main()`(CLI 시작), `POST /api/open-folder`, `POST /api/chat`의
  자동 생성 경로. 항목은 경로 기준 dedupe 후 맨 앞으로, 50개
  초과분은 가장 오래된 것부터 제거.
- `pair_messages_into_turns()`가 채팅 로그를 turn 단위로 묶는다 —
  `user` 메시지 하나 + 뒤따르는 `agent` 메시지(들)를 항목 1개로,
  auto-fallback으로 `agent` 메시지가 여러 개면 마지막 메시지의
  provider/status로 계속 덮어써서 자연스럽게 "최종 결과"만 남긴다
  (DEC-12).
- `collect_recent_turns()`가 최신 달부터 거꾸로 훑어서 5개(DEC-11)를
  채울 때까지만 읽는다 — 오래된 프로젝트라고 모든 달을 다 읽지 않음.
- `build_history_drawer()`: 현재 워크스페이스를 레지스트리 유무와
  무관하게 항상 맨 앞에 고정하고, 그 다음은 레지스트리의
  최근-연-순서 그대로. 폴더가 사라진 레지스트리 항목은 조용히
  건너뜀(에러 없음, DEC-09).
- `GET /api/history` 신설, `webui/app.js`는 기존 `switchWorkspaceTo()`를
  그대로 재사용해 항목 클릭 시 워크스페이스 전환(DEC-11 — 새 코드
  경로 불필요).

**검증**: 레지스트리 CRUD(실패 격리 포함), turn 페어링(월 경계 케이스
포함), `collect_recent_turns()`의 달-건너뛰기, 드로어 조립 로직, 실
서버로 띄운 HTTP 통합 테스트 추가 — 정확한 개수는
`python3 -m unittest discover -s tests -v`로 확인(고정 숫자를 여기 적지
않는 이유는 위 Phase 1 문단과 동일 — 드리프트하기 쉬움). `$HOME`을
임시 디렉터리로 바꿔치기한 실제 프로세스로 전체 흐름(빈 워크스페이스 →
한글 첫 메시지로 자동 생성 → Open Folder로 두 번째 프로젝트 전환 →
히스토리 드로어에 둘 다 정확한 순서로 표시)을 curl로 검증.

## Phase 4 — CLI 미설치 사용자 온보딩 + API 키 모드 ✅ 완료

**목표**: [SCR-06](wireframes.html#s8)을 실제로 동작시킨다 — CLI가 없는
사용자가 API 키로 provider에 연결.

**왜 여기 오는가**: [CFL-12](flutter-mapping.html#s2)가 이미 지적했듯, 이건
"UI 옵션 하나 추가"가 아니라 `run_provider()`의 서브프로세스 구조와
근본적으로 다른 경로(HTTP/SDK 직접 호출, 세션 재개 재설계)가 필요하다.
Phase 1로 CLI 경로가 실제로 검증된 뒤에 그 대안 경로를 설계하는 게
순서상 맞다.

**착수 전 조사** ([docs/research.md](../research.md)와 같은 형식으로
[docs/research-api-key-mode.md](../research-api-key-mode.md) 작성): 두
벤더 모두 "세션 재개·파일 편집·명령 실행"을 일반 API 키 호출 뒤에
노출하지 않는다는 게 확인됐다 — Anthropic Messages API/OpenAI Responses
API는 상태 없는(stateless) 텍스트 API고, 벤더의 "CLI 없이 에이전트를
돌리는" 진짜 제품(Claude Code routines, Codex cloud tasks)은 fire-and-
forget이고 벤더 자체 웹 UI에서 사전 설정이 필요해 이 앱의 기존 채팅
UX와 맞지 않는다.

**설계 확정** (2026-08-05, 조사 결과를 바탕으로 사용자에게 범위 질문 1건
→ [flutter-mapping.html DEC-13~16](flutter-mapping.html#s1c)):
- 범위 = **채팅 전용**(이후 CFL-17로 전체 패리티까지 확장됨 — 아래
  "후속 수정" 참고). 전체 에이전트 기능 패리티(파일 편집·명령 실행)는
  사용자 지시에 따라 **의도적으로 미래 phase로 명시적으로 연기**
  (신규 CFL-17) — 지금 결정하지 않는 게 아니라
  "나중에 추가한다"고 결정한 것(DEC-13).
- 키 저장 위치 = `~/Documents/Agent Handoff Bridge/credentials.json`,
  `0600` 권한. OS 키체인·`keyring` 패키지 둘 다 기각(DEC-14).
- Codex·Claude 둘 다 대칭 지원(DEC-15).
- CLI가 감지되면 항상 CLI 경로, CLI가 없고 키가 저장된 경우에만 API 키
  경로 — provider별 독립, `auto`는 CLI가 하나라도 있으면 기존 동작 유지
  (DEC-16).

**해소하는 항목**: [CFL-12](flutter-mapping.html#s2) 완전 해소 — Conflict
List에서 제거됨. 새로 발생: CFL-17(전체 에이전트 기능 패리티, 의도적
연기 — 이후 [DEC-21](flutter-mapping.html#s1c)로 해소됨, 아래 "후속
수정" 참고).

**실제로 한 것**:
- `handoff_webui.py`: `credentials_path()`/`read_credentials()`/
  `save_credential()`(registry.json과 같은 패턴 — 함수형 경로, 실패
  격리, `0600` chmod), `cli_available()`, `call_anthropic_messages_api()`/
  `call_openai_responses_api()`(`urllib`만 사용, 새 의존성 없음),
  `build_api_message_history()`(세션이 없으므로 채팅 로그를 매 호출 다시
  재생), `run_provider_via_api_key()`(기존 subprocess 경로와 동일한
  레코드 shape 반환 — `classify_run_status()`/`append_chat_message()`가
  수정 없이 그대로 처리). `.handoff/state.json`은 건드리지 않음(CLI
  핸드오프 전용 상태로 남김).
- `_run_provider_via_bridge_locked()`에 분기 추가 — CLI 감지 시/CLI
  없고 키도 없을 시 기존 동작이 완전히 그대로임을 우선 보장.
- `GET /api/providers`(연결 패널이 읽는 상태), `POST /api/provider-key`
  신설 — **엔드포인트 자체의 계약**은 빈 키 = 해당 provider의 저장된 키
  삭제(`save_credential()`의 계약, 지금도 그대로). 다만 **프론트엔드
  "저장" 버튼**은 이후 리뷰 라운드에서 빈 키를 그대로 이 엔드포인트에
  보내지 않도록 고쳐졌다 — 저장된 키는 패널에 다시 echo되지 않으므로
  "저장" 클릭 시 키 필드가 비어 있으면 아무 요청도 보내지 않는 no-op이고
  (예: model만 고치려다 실수로 키를 지우는 사고 방지), 실제 삭제는 별도
  "연결 해제" 버튼만 이 엔드포인트에 빈 키를 보낸다. 즉 **엔드포인트
  계약과 UI 동작은 서로 다른 층**이며 상충하지 않는다 — 전체 경위는
  `docs/release-notes.md`의 Phase 4 "Round 2" 항목, UI 동작은
  [CLI Reference § Web UI](../cli-reference.md#web-ui-mvp) 참고.
- `webui/index.html`/`app.js`/`app.css`: Diagnose 버튼 + 연결 패널
  (components.html §14 그대로) — provider별 CLI 감지됨/없음 배지, CLI
  없을 때만 키+model 입력 노출.

**검증**: 자격증명 CRUD(실패 격리·권한 포함), `/api/providers`·
`/api/provider-key` 라우팅, 디스패치 우선순위(CLI가 감지되는 기존
케이스는 전부 이 phase 이전 동작과 동일하게 무변경 통과), `_http_post_json`
목(mock)을 통한 성공/에러 응답 매핑(Anthropic·OpenAI 두 응답 shape
모두), API 키가 어떤 에러 메시지에도 절대 나타나지 않음, 채팅 히스토리
재생(월 단위 캡). 정확한 개수는
`python3 -m unittest discover -s tests -v`로 확인(고정 숫자를 여기 적지
않는 이유는 위 Phase 1/3 문단과 동일 — 드리프트하기 쉬움).

- **후속 수정(DEC-21, CFL-17 해소)**: 채팅 전용으로 남겨뒀던 API 키
  모드에 전체 에이전트 기능 패리티를 추가 — `read_file`/`write_file`/
  `edit_file`/`run_shell` 4개 도구와 그 turn loop를
  `call_anthropic_messages_api()`/`call_openai_responses_api()` 내부에
  구현(기존 함수 이름·계약 그대로, 도구 호출이 없는 평범한 채팅 턴은
  루프 1회로 끝나 이전 동작과 동일). 인터뷰로 확정된 범위는 파일 도구와
  셸 실행을 함께 구현하는 더 큰 쪽, 도구 호출 확인 UX는 DEC-02 그대로
  재사용(세션 첫 전송만 확인, 이후 셸 실행 포함 모든 도구 호출 자동
  실행) — 자세한 트레이드오프는
  [flutter-mapping.html DEC-21](flutter-mapping.html#s1c) 참고. 파일
  도구는 기존 `safe_join()`을 그대로 재사용해 워크스페이스 밖 경로를
  차단하고, 도구 호출 활동은 새 메시지 스키마 없이 DEC-03(펜스
  코드블록)으로 채팅 로그에 그대로 노출된다.

## Phase 5 — Gemini CLI 실사용 지원 + provider 확장성 리팩터 ✅ 완료

**목표**: 세 번째 provider를 실제로 붙인다.

**착수 전 조사** (`docs/research.md`와 같은 형식으로
[docs/research-gemini-cli.md](../research-gemini-cli.md) 작성): Gemini
CLI는 codex/claude와 같은 "subprocess + 구조화 출력 파싱 + handoff 신호
감지" 아키텍처에 그대로 들어맞지만, 세 가지가 진짜로 다르다 — (1) 무료
인증-상태 확인 명령이 없음, (2) JSON 출력이 JSONL 스트림이 아니라 실행
끝에 객체 하나, (3) JSON 응답에 세션/스레드 ID가 없음. 이 세 가지가
단순 기계적 확장이 아니라 실제 설계 결정을 요구했다.

**설계 확정** (2026-08-05, 조사 결과를 바탕으로 사전 인터뷰 2건 — 1건은
세션 스코프 관련 추가 조사 후 재확정 →
[flutter-mapping.html DEC-17~18](flutter-mapping.html#s1c)):
- Gemini resume = **`--resume latest` 사용**. Gemini 세션이 전역이 아니라
  워크스페이스 디렉터리별로 스코프된다는 걸 확인한 뒤, codex/claude와
  동일한 컨텍스트 유지 UX를 택함 — 같은 디렉터리에서 사용자가 bridge
  밖에서 직접 대화형 gemini를 돌리면 섞일 수 있다는 잔여 위험은 문서화만
  (DEC-17).
- Gemini 인증 확인 = **probe 생략**. `diagnose()`는 CLI 설치 여부만
  확인하고 인증 상태는 "확인 안 함"으로 표시 — 토큰을 쓰는 probe 호출을
  강제하지 않아 diagnose가 항상 무료/예측 가능하게 유지 (DEC-18).

**해소하는 항목**: [CFL-13](flutter-mapping.html#s2) 완전 해소 — Conflict
List에서 제거됨.

**실제로 한 것**:
- `handoff_bridge.py`: `PROVIDERS`를 `("codex", "claude", "gemini")`로
  확장. `other_provider()`(이진 토글)를
  `next_provider(current, tried=frozenset())`로 교체 — `PROVIDERS` 순서를
  따라가며 랩어라운드, `tried`에 있는 항목은 건너뜀. 3개 호출부
  (`init_handoff()`, `choose_auto_provider()`, `run_provider()`의
  auto-fallback) 전부 연결 — auto-fallback은 여전히 한 홉만 시도(기존
  동작 그대로, "다음 provider를 어떻게 고르는지"만 일반화됨).
- `provider_command()`에 `gemini` 분기 추가 — 프롬프트는 codex/claude와
  동일하게 stdin으로 전달(`-p` 인자 없음), `session_id`가 있을 때만
  `--resume latest` 추가.
- `summarize_gemini(stdout)` 신설 — `parse_jsonl()`이 적용 안 되는 이유는
  Gemini의 `--output-format json`이 JSONL 스트림이 아니라 실행 끝에 JSON
  객체 하나만 반환하기 때문. `session_id`는 실제 ID가 아니라, 이 실행이
  `error` 필드 없이 깨끗하게 끝났을 때만 세팅되는 `"latest"` sentinel —
  `provider_command()`가 "이 워크스페이스에서 gemini가 성공적으로 실행된
  적 있는지"만 알면 되기 때문.
- `diagnose()`에 "gemini auth: not checked" 줄 추가(DEC-18) — CLI
  감지 자체는 기존 `PROVIDERS` 순회 루프가 자동으로 커버.
- `handoff_webui.py`: `API_KEY_MODE_PROVIDERS = ("codex", "claude")`를
  기존 `PROVIDERS`와 별도로 신설 — Phase 4의 API 키 모드가 Gemini로
  자동 확장되지 않도록(DEC-15는 여전히 codex/claude만 대상, "Gemini도
  지원할지"는 별개의 열린 질문으로 명시). `/api/run`은
  `PROVIDERS` 전체를 인식하도록 검증 완화, `/api/providers`는 Gemini의
  CLI 감지 배지는 보여주되(SCR-06이 원래 "미확인"으로 뒀던 자리가 이제
  실제 값으로 채워짐) 키 입력 UI는 노출하지 않음
  (`api_key_mode_supported` 플래그). **(이후 DEC-25로 뒤집힘 — 아래
  참고.)**
- `webui/index.html`/`app.js`: provider-select에 `gemini` 옵션 추가,
  연결 패널이 `api_key_mode_supported`를 확인해 Gemini에는 키 필드를
  숨김. **(이후 DEC-25로 뒤집힘 — 아래 참고.)**

**DEC-25 후속 (API 키 모드 대상 확장)**: DEC-15가 열어뒀던 질문이
나중에 해소됨 — `API_KEY_MODE_PROVIDERS`가 `("codex", "claude",
"gemini")`로 확장되어 Gemini도 API 키 모드를 지원. 새
`call_gemini_api()`가 다른 두 provider와 동일한 계약으로 구현. 자세한
내용은
[flutter-mapping.html DEC-25](flutter-mapping.html#s1c)와
[provider-extensibility.md](../provider-extensibility.md)의 "Adding An
API-Key-Based Provider" 절 참고.

**검증**: `next_provider()`(순서·랩어라운드·tried 스킵), `provider_command()`
gemini 분기, `summarize_gemini()`(성공·에러·malformed 입력), 가짜 `gemini`
바이너리를 이용한 실제 서브프로세스 통합 테스트, webui 쪽
`API_KEY_MODE_PROVIDERS` 분리가 실제로 Gemini를 API 키 모드에서
차단하는지, `/api/providers`가 Gemini를 CLI 감지 배지로는 보여주지만
키 UI는 숨기는지. 정확한 개수는
`python3 -m unittest discover -s tests -v`로 확인(고정 숫자를 여기 적지
않는 이유는 이전 phase들과 동일 — 드리프트하기 쉬움).

## Phase 6 — 자동 업데이트 확인 ✅ 완료

**목표**: [SCR-07](wireframes.html#s9)을 실제로 동작시킨다.

**막혔던 지점** ([CFL-11](flutter-mapping.html#s2)): 이 저장소가 private라
익명으로 GitHub Releases를 못 읽는다. 둘 중 하나 결정 필요했다:
사용자의 로컬 `gh` 인증을 재사용하거나, 버전 번호만 공개로 노출하는
별도 정적 엔드포인트를 둔다.

**설계 확정** (2026-08-05, 사전 인터뷰 1건 →
[flutter-mapping.html DEC-19](flutter-mapping.html#s1c)): **로컬 `gh` CLI
인증 재사용**. `docs/release-process.md`가 이미 릴리즈를 만들 때 `gh`를
전제하므로, 읽을 때도 같은 도구를 재사용하는 편이 새 호스팅/배포 단계를
만드는 것보다 이 프로젝트의 최소-의존성 원칙에 맞는다. 트레이드오프를
의식적으로 수용: `gh`가 없거나 인증 안 된 환경(예: zip으로 받은 일반
사용자)에서는 업데이트 확인이 조용히 실패한다 — 지금은 실사용자가
운영자 자신뿐인 단계라는 전제로 받아들임.

**해소하는 항목**: [CFL-11](flutter-mapping.html#s2) 완전 해소 — Conflict
List에서 제거됨.

**실제로 한 것**:
- `handoff_bridge.py`: `GITHUB_REPO` 상수, `parse_version_tuple()`("v0.2.0"
  → `(0, 2, 0)`, 파싱 실패 시 `None`), `check_for_update()` 신설 —
  `gh release view --repo <repo> --json tagName,url`을 `short_run()`으로
  호출(10초 타임아웃, `gh` 미설치/미인증/타임아웃 전부 이미 처리됨),
  `BRIDGE_VERSION`과 비교해 더 새 릴리즈가 있을 때만
  `{status: "available", latest_version, current_version, url}` 반환,
  성공했지만 최신이면 `{status: "current", current_version}`, `gh`
  미설치/미인증/오프라인/파싱 실패 등 확인 자체가 안 되면
  `{status: "unavailable", current_version}` — 절대 `None`을 반환하거나
  예외를 던지지 않고, 실패를 사용자에게 노출하지 않는 `touch_registry()`와
  같은 원칙이되 "확인 불가"와 "확인 완료·최신"은 서로 다른 `status`로
  구분됨([flutter-mapping.html DEC-20](flutter-mapping.html#s1c), 최초엔
  이 두 경우가 똑같이 `None`이라 리뷰에서 CFL-18로 지적됐고 이후 별도로
  해소).
- `handoff_webui.py`: `AppState.update_info`(기본 `None`) +
  `update_checked`(기본 `False`) 추가. `main()`이 서버 시작 직후
  `_check_for_update_in_background()`를 데몬 스레드로 실행 — 실제 네트워크
  I/O(`gh` 호출)가 서버 부팅/창 띄우기를 지연시키지 않도록.
  `GET /api/update-check`는 이 캐시된 값을 읽기만 하므로 항상 즉시 응답.
- `webui/index.html`/`app.css`/`app.js`: 타이틀바 "업데이트" 버튼(항상
  표시) + 점(dot) 배지(업데이트 있을 때만 표시) + 팝오버(버전 정보,
  "나중에"/"릴리즈 노트 보기"). 와이어프레임이 "업데이트 있음" 상태만
  목업했으므로, 업데이트가 없을 때 버튼을 눌러도 새 팝오버 레이아웃을
  발명하지 않고 기존 토스트("최신 버전을 사용 중입니다")를 재사용.
- **PR 리뷰에서 발견된 실제 레이스 수정**: 처음엔 `update_info is None`을
  "아직 확인 안 됨"과 "확인했지만 없음" 둘 다로 취급했는데, 실제 `gh`
  네트워크 호출이 페이지 로드+첫 조회보다 오래 걸리는 경우(서버 시작
  직후 거의 항상 그렇다)가 실제로 재현 가능해 배지가 조용히 영구
  누락될 수 있었다. `update_checked` 플래그를 추가해 두 상태를 구분하고,
  `webui/app.js`가 `checked: false`인 동안 짧게(1.5초 간격, 최대 10회 =
  15초, `gh` 기본 타임아웃 10초를 넉넉히 넘김) 재조회하도록 수정.

- **후속 수정(DEC-20, CFL-18 해소)**: 위 "그 외엔 전부 `None`"이던 초기
  구현은 "확인했지만 최신"과 "확인 자체가 불가"를 구분하지 못해 `gh`가
  없는 사용자에게도 "최신 버전을 사용 중입니다"라는 사실과 다른 안내를
  보여줬다. `status: available|current|unavailable` 3분류로 다시 나누고,
  `GET /api/update-check`도 `update_available` 불리언 대신 이 `status`를
  그대로 노출하도록 수정 — `webui/app.js`는 `unavailable` 전용 토스트
  ("업데이트를 확인할 수 없습니다")를 새로 추가.

**검증**: `parse_version_tuple()`(v-prefix, 파싱 실패, 길이 다른 버전
비교), `check_for_update()`(신규 릴리즈 감지·동일 버전·과거 버전 모두
올바른 `status`로 구분, `gh` 미설치/에러/malformed JSON 전부
`status: "unavailable"`, `--repo` 플래그로 cwd에 의존하지 않음 확인),
`GET /api/update-check`(미확인·확인+최신·확인+신규·확인 불가 네 상태
구분), 백그라운드 체크가 `state.update_info`/`update_checked`를
올바르게 세팅하는지. 정확한
개수는 `python3 -m unittest discover -s tests -v`로 확인.

## Phase 7 — 프레임워크 전환 (DEC-01, 최종 목표)

**목표**: `handoff_webui.py` + `webui/`(stdlib + 선택적 pywebview)를
DEC-01이 가리키는 실제 프로덕션 스택으로 이관.

**왜 마지막인가**: Phase 1–6에서 기능·UX가 계속 바뀐다. 그 상태에서
무거운 프레임워크 전환부터 하면 같은 화면을 두 번 만드는 셈이 된다.
저비용 스택 위에서 기능을 전부 검증한 뒤, 안정된 기능 집합을 한 번만
옮기는 편이 싸다.

**해소하는 항목**: [CFL-06](flutter-mapping.html#s1b)(선택 완료, 실행만
남음), CFL-09(배포 파이프라인 재설계 — 이 단계에서
[release-process.md](../release-process.md)를 다시 써야 함. 실제로는
"zip 하나로 git 없이 실행" 모델이 끝나는 게 아니라 병행 유지되는 쪽으로
결론남 — [DEC-23](flutter-mapping.html#s1c) 참고),
[CFL-14](flutter-mapping.html#s1c) 해소(MVP를 계속 확장할지/재작성할지
질문 — 재작성 쪽으로 확정, 단 백엔드는 그대로 옮김).

**착수 전 조사** ([docs/research-phase7-framework.md](../research-phase7-framework.md)):
Tauri·Electron 둘 다 공식 문서상 이 프로젝트의 Python 백엔드를 다시 쓰지
않고 sidecar(외부 바이너리)로 그대로 유지할 수 있고, 둘 다 바닐라
JS/CSS/HTML 프론트엔드도 그대로 유지할 수 있다 — 실제 갈림길은 (1) Tauri의
공식 sidecar 지원(PyInstaller로 빌드한 Python API 서버를 문서에서 직접
예시로 듦)이 Electron의 서드파티 조합(electron-builder)보다 이 프로젝트
모양에 훨씬 잘 맞는다는 점, (2) 반대로 private repo 자동 업데이트는
Electron 쪽(electron-updater, 문서화는 됐지만 "매우 특수한 경우에만"
경고)이 Tauri(문서화된 경로 자체가 없음)보다 낫다는 점.

**설계 확정** (2026-08-05, 조사 결과를 바탕으로 킥오프 질문 4건 →
[flutter-mapping.html DEC-22](flutter-mapping.html#s1c)):
- 프레임워크 = **Tauri**(Electron 기각). sidecar 우위가 이 프로젝트
  실제 모양에 더 결정적이고, Electron의 유일한 우위(자동 업데이트)는
  아래 결정으로 애초에 필요 없어짐.
- 백엔드 = **`handoff_webui.py`를 PyInstaller sidecar로 그대로 유지**,
  Rust 재작성 안 함. 검증된 353개 테스트 커버 로직을 버릴 이유 없음.
- 자동 업데이트 = **기존 `check_for_update()`(DEC-19, `gh` CLI 재사용)
  그대로 유지**, Tauri 공식 updater 채택 안 함 — 어느 프레임워크의
  공식 updater도 private repo를 깔끔히 지원하지 않음.
- 프론트엔드 = **이번 phase는 기존 바닐라 JS `webui/`를 근접 그대로
  이식**. DEC-01의 원래 동기(네이티브 애니메이션)를 위한 프론트엔드
  프레임워크 도입은 의도적으로 후속 sub-phase로 연기.
- 코드 서명(macOS 공증·Windows 서명)은 이번 인터뷰 범위 밖 — 별도
  결정 필요, 미서명 상태로 우선 진행 가능.

**Sub-phase 분해** (하나의 거대한 PR 대신 이전 phase들과 같은 크기
규율 적용):
- **7a — Tauri 셸 + Python sidecar (기능적 동등성만, 비주얼 다듬기 없음)
  — ✅ 완료 (PR #12)**: Tauri v2 프로젝트 스캐폴딩(바닐라 JS 템플릿),
  `handoff_webui.py`를 PyInstaller로 빌드해 sidecar로 등록, 기존
  `webui/`를 sidecar의 로컬 HTTP 서버에 그대로 연결. 최소 한 OS(개발
  머신)에서 엔드투엔드 동작 확인이 목표 — 배포 파이프라인·서명·
  크로스플랫폼 빌드는 범위 밖.
- **7b — 크로스플랫폼 빌드 + 패키징 — ✅ 완료 (PR #13~#16, 2026-08-06)**:
  Windows/Linux PyInstaller 빌드, Tauri 번들 설정(설치형 산출물),
  `scripts/package_platforms.py`의 zip 방식과 병행 유지(DEC-23),
  `release-process.md` 재작성(CFL-09 해소), 7a가 남긴 sidecar 생명주기
  후속 항목까지 검증·수정. 상세는 위 "7b 마무리 요약" 참고.
- **7c — 코드 서명 — ✅ 결정 완료: 하지 않는다 (DEC-24, 2026-08-06)**:
  macOS 공증 + Windows 서명. DEC-22가 "별도 결정 게이트"로만 남겨뒀던
  것을, 실제 비용(macOS $99/년, Windows 인증서+선택적 HSM/토큰)을
  확인하고 자매 프로젝트 `file-converter`의 실제 선례(DEC-029, 같은
  갈림길에서 같은 결론)까지 참고해 **최종 결정**으로 종결 — "나중에
  다시 논의"가 아니라 "지금 규모에서는 안 한다"는 확정. 상세 근거는
  `flutter-mapping.html`의 DEC-24, 사용자 안내는
  `docs/security-model.md` 참고. 전제(운영자 자신만 실사용자인 단계)가
  바뀌기 전까지는 재논의하지 않음.
- **7d (별도 논의, 이번 범위 밖)** — 프론트엔드 프레임워크 도입(DEC-01의
  네이티브 애니메이션 목표를 실제로 달성하려면 필요할 가능성이 높지만,
  기술적으로는 독립된 결정).

**7a에서 의도적으로 미룬 것** (리뷰에서 지적, 머지 차단은 아님 —
7b/7c 진입 전에 확인할 가치가 있는 실제 후속 항목): 앱 종료 시
sidecar 프로세스가 함께 정리되는지, 재실행 시 포트 `8787`이 이미
사용 중이면 어떻게 되는지 둘 다 검증되지 않음. "sidecar 아키텍처가
동작하는가"라는 7a의 범위 자체는 이미 증명됐고, 이 두 가지는 그
아키텍처를 실제 운영 가능하게 다듬는 문제(엣지 케이스 견고화)라
7b/7c의 성격에 더 가까움. **7b M6에서 실제로 검증·수정됨** — 앞은
실제로 깨져 있었고(고아 프로세스로 확인), 뒤는 멈추진 않았지만
메시지가 부실했음. 상세는 아래 "7b M6 실제로 한 것" 참고.

**7b 계획 (착수 전 — 사용자 확인 후 시작, 2026-08-06) — ✅ 전 항목
완료 (2026-08-06, PR #13~#16)**: 7a 실제 구현 경험에서 나온 구체적
작업 목록. 순서는 의존관계 기준(빌드 인프라 → 패키징 → 배포 문서).

1. **크로스플랫폼 sidecar 빌드** — ✅ **완료 (M1, PR #13)**.
   PyInstaller/Nuitka는 크로스컴파일을 지원하지 않으므로
   (`docs/research-phase7-framework.md`) Windows·Linux 각각 실제 해당
   OS에서 빌드해야 함 — GitHub Actions `windows-latest`/`ubuntu-latest`
   러너 활용이 유력. `scripts/build_phase7a_sidecars.py`는 현재 POSIX
   전제(`--add-data` 구분자 `:`)라 Windows에서는 `;`로 바뀌어야 하고,
   `.exe` 접미사 처리도 추가해야 함 — 스크립트 이름도
   `build_sidecars.py`처럼 7a 한정이 아니게 일반화하는 편이 나음.
2. **target-triple 자동화** — ✅ **완료 (M1에 통합, PR #13)**. 지금은
   `cp binary binary-<triple>`을 손으로 했음 — 빌드 스크립트가
   `rustc -vV`의 `host:` 값(또는 CI 매트릭스에서 명시적으로 지정한
   타겟)을 읽어 자동으로 올바른 이름을 붙이도록 만들어야 함. macOS는
   Intel(`x86_64-apple-darwin`) 지원 여부도 이번에 결정 필요(현재
   Apple Silicon만 실제 빌드해봄) — **미결 상태로 남음**: M4에서
   Apple Silicon 전용이라는 제약만 명시하고 Intel 지원 여부 자체는
   아직 별도 결정되지 않음(`docs/release-process.md` 참고).
3. **`rust-build` CI를 실빌드로 확장** — ✅ **완료 (M3, PR #14, 새
   `installer-build` job)**. 지금 CI는 `cargo build` 컴파일 체크만
   함(더미 sidecar 사용). 7b에서는 각 OS 러너에서 실제 `cargo tauri
   build`로 진짜 설치형 산출물(.dmg/.msi/.AppImage 등)을 만드는 CI로
   확장 — 이번에 발견한 `needrestart`/apt-get 비대화형 설정
   (`DEBIAN_FRONTEND=noninteractive`, `NEEDRESTART_MODE=a`)과
   `timeout-minutes` 안전장치를 새 job에도 그대로 적용해 같은 함정을
   또 밟지 않도록.
4. **`release-process.md` 재작성** — ✅ **완료 (M4, PR #15, DEC-23)**.
   현재 문서는 "zip 하나, git 불필요" 모델을 전제로 함(CFL-09). 새
   모델은 "OS별 설치형 산출물 + 자동 업데이트는 여전히 기존 `gh` 기반
   방식"으로 다시 쓰고, `scripts/package_platforms.py`의 위치(완전
   대체 vs 유지)도 결정.
5. **코드 서명은 7c로 분리 유지** — ✅ **항목 자체가 그대로 이행됨,
   2026-08-06 사용자가 "코드 서명 일단은 제외해줘"로 재확인**. 7b는
   미서명 설치형 산출물까지만. 서명은 비용(Apple $99/년+)·계정 준비가
   필요해 별도 게이트라는 DEC-22의 결정 그대로 — 이 항목은 "하지
   않는다"가 곧 이행이라, 별도 M 번호 없이 이 확인 자체로 완결.
6. **7a가 남긴 후속 항목도 이번에 같이 확인** — ✅ **완료 (M6, PR #16)**.
   위에서 언급한 sidecar 종료 정리, 포트 `8787` 충돌 처리 — 실제
   설치형 배포판을 여러 OS에서 테스트하는 이번 단계가 이 두 가지를
   자연스럽게 검증할 기회.

**7b 마무리 요약** (2026-08-06): 6개 계획 항목 전부 이행 완료 —
M1(PR #13)·M3(PR #14)·M4(PR #15, DEC-23)·M6(PR #16), M2는 M1에 통합,
M5는 "하지 않는다"는 결정 자체의 재확인으로 완결. `docs/release-notes.md`
Unreleased 섹션에 Phase 7b 항목 추가해 사용자 대상 변경 이력에도 반영.
**7b 범위에서 여전히 열려 있는, 의도적으로 남겨둔 항목**: (a) macOS
Intel(`x86_64-apple-darwin`) 지원 여부 — 결정된 적 없음, 필요해지면
별도로 다뤄야 함; (b) 설치형 산출물 트랙(`installer-build`) 자체가
실제 태그된 릴리즈로 한 번도 실행된 적 없음; (c) M6에서 고친 심화
로직(트리 kill, graceful 타이밍)의 Windows/Linux 실기기 검증 —
이 macOS 개발 환경에서는 애초에 불가능. 다음 단계는 7c(코드 서명,
현재 명시적으로 제외)이거나, 위 (a)~(c) 중 하나를 실제로 겪을 때
(예: 첫 실제 릴리즈, Windows 접근 가능해짐)까지 대기.

**7b M1 실제로 한 것** (2026-08-06, 위 계획의 항목 1+2): 사용자가
"계획만 준비"를 선택한 뒤, 이어서 "M1부터 실제 진행"을 명시적으로
요청해 착수.
- `scripts/build_phase7a_sidecars.py` → `scripts/build_sidecars.py`로
  이름 변경 + 일반화 — 더 이상 7a 한정 스크립트가 아니므로.
  `handoff_bridge.py`의 `INSTALL_FILES`, `scripts/validate_handoff.py`의
  `REQUIRED_FILES`/`PYTHON_FILES` 세 곳 모두 새 이름으로 갱신(옛 이름을
  추적하던 목록이 갱신 안 되면 `check`가 삭제된 파일을 계속 찾게 됨).
- `--add-data` 구분자를 하드코딩된 `:` 대신 `os.pathsep`으로 — Windows는
  `;`, 그 외는 `:`라는 PyInstaller 자체 규칙과 정확히 일치.
- `rename_for_tauri()`: 이전엔 손으로 `cp binary binary-<triple>`을
  네 번 반복했던 걸 자동화. target triple은 `--target-triple` 인자로
  명시하거나, 생략 시 `rustc -vV`의 `host:` 줄에서 자동 추론. Windows는
  `.exe` 접미사가 실행파일 이름과 target-triple 접미사 이름 둘 다에
  붙어야 함(`agent-handoff-bridge-server.exe` →
  `agent-handoff-bridge-server-x86_64-pc-windows-msvc.exe`)을 확인하고
  반영.
- **`.github/workflows/ci.yml`에 `sidecar-build` job 신설**: `macos-latest`
  · `windows-latest` · `ubuntu-latest` 3-way 매트릭스로 각 OS에서 실제
  `scripts/build_sidecars.py`를 돌려 sidecar 4개(× target-triple 접미사
  버전까지 총 8개 파일)가 만들어지는지 검증하고 `actions/upload-artifact`로
  보관(7일). 매트릭스의 target triple은 `rustc -vV`를 부르지 않고
  GitHub 호스팅 러너별로 이미 알려진 값을 명시적으로 지정 —
  이 job은 Python/PyInstaller 패키징 작업이라 Rust 툴체인 설치 자체가
  불필요.
  **로컬에서 재현 불가능한 채 CI에서만 검증 가능한 두 플랫폼(Windows·
  Linux)이 처음 생기는 지점**이라 다음 두 가지를 미리 반영: (a)
  `python3`이 아니라 `python`으로 호출 — `actions/setup-python`이
  Windows에는 `python.exe`만 PATH에 놓고 `python3` alias를 보장하지
  않는다는 잘 알려진 함정을 실제로 걸리기 전에 미리 회피; (b)
  `rust-build` job에서 이미 겪은 apt-get/`needrestart` 무한 대기 교훈과
  동일하게 `timeout-minutes: 10`을 기본으로 둠. 로컬 macOS에서는
  스크립트 자체(자동 추론 경로 + `--target-triple` 수동 지정 경로 둘
  다)와 `cargo build`까지 직접 재현해 확인했지만, Windows/Linux
  러너에서의 실제 성공 여부는 CI 실행 결과로만 최종 확인 가능.

**7b M3 실제로 한 것** (2026-08-06, 위 계획의 항목 3): "다음 작업
진행해줘"로 착수. 실제 설치형 산출물 빌드(`cargo tauri build`)는
GitHub 비공개 저장소 Actions 분당 과금이 macOS ×10 / Windows ×2라 매
PR/push마다 도는 기존 job들과 같은 트리거로 두면 비용이 크다는 점을
사용자에게 확인 — **`workflow_dispatch` 수동 트리거만**으로 결정
(다른 두 옵션은 "main push에만"/"기존 job과 동일하게 매번"이었음).
- `.github/workflows/ci.yml`에 `installer-build` job 신설(`if:
  github.event_name == 'workflow_dispatch'`). `sidecar-build`와 동일한
  3-way 매트릭스 + runner OS/arch 가드를 재사용하되, 실제 산출물이
  필요하므로 `rust-build`의 더미 sidecar 대신 `scripts/
  build_sidecars.py`로 진짜 sidecar를 먼저 빌드한 뒤 `cargo tauri
  build`로 실제 번들(.dmg+.app / .msi+nsis .exe / .deb+.AppImage+.rpm)을
  만든다. 서명은 하지 않음(7c/DEC-22가 그대로 적용 — 이번 산출물은
  전부 미서명). 포맷별 산출 파일 존재를 OS별로 개별 검증(파일명만
  보고 끝내지 않도록, `sidecar-build`의 review-bot 지적과 같은 원칙
  재사용)한 뒤 `actions/upload-artifact`로 보관.
- 로컬 macOS에서 CI와 동일한 순서(진짜 sidecar 빌드 → `cargo tauri
  build`)로 실제 재현: `.app`/`.dmg` 둘 다 정상 생성, 검증 스텝의
  `find` 조건도 실제 산출물 경로에 대해 그대로 통과 확인. Windows/
  Linux는 로컬 재현이 불가능한 플랫폼이라 (M1 때와 마찬가지로) 실제
  CI 실행 결과로만 최종 확인 가능 — 특히 Windows의 NSIS/WiX 자동
  다운로드, Linux의 AppImage/rpm 번들러 동작은 이번이 처음 실제로
  실행되는 지점.
- **PR 오픈 전 self-review에서 잡힌 실수들**: 처음 작성했던 apt
  패키지 목록이 틀렸음 — Tauri의 rpm 번들러는 순수 Rust `rpm` crate로
  동작해 시스템 `rpm`/`rpmbuild`가 전혀 필요 없는데(소스 직접 확인),
  실제로는 필요 없는 `rpm` 패키지를 추가했었음 — 제거. 대신 `rust-build`/
  `sidecar-build` 둘 다 실제 Linux 번들링 경로를 한 번도 타본 적이
  없어서 놓쳤던 진짜 필요 패키지(Tauri 공식 예제 CI가 쓰는
  `patchelf`/`xdg-utils`)를 추가. 또한 AppImage 번들러(`linuxdeploy`)가
  `ubuntu-latest`에서 실패하는 현재 열려 있는 업스트림 버그
  (tauri-apps/tauri#14796)를 미리 알게 되어, 흔한 원인인 `libfuse2`
  누락에 대한 선제 조치로 apt 목록에 추가 — 그래도 재발하면 실제 CI
  로그로 진단 필요. `cargo install tauri-cli --version "^2"`는 버전이
  고정되지 않아 이 job을 나중에 다시 수동 실행할 때마다 번들러 동작이
  조용히 달라질 수 있어, 이번에 실제로 로컬 검증에 쓴 정확한 버전
  (`2.11.4`)으로 고정.
- **PR #14 실제 리뷰 라운드에서 잡힌 것 (머지 전 수정 완료, 재검증까지
  통과 — 위험 하/0건으로 머지됨)**:
  (1) `libfuse2` 고정 설치가 위험 중 지적됨 — `ubuntu-latest`가
  24.04 계열이면 Ubuntu의 64-bit time_t 전환으로 패키지명이
  `libfuse2t64`로 바뀌었을 수 있어, apt install 자체가 번들러 단계에
  도달하기도 전에 실패할 수 있음. `libfuse2` 설치를 별도 단계로
  분리하고 `libfuse2t64` 폴백을 추가(둘 다 실패해도 나머지 단계는
  계속 진행 — AppImage leg만 나중에 실패하도록). (2) `workflow_dispatch`를
  workflow 전체 트리거로 추가했더니 기존 `validate`/`rust-build`/
  `sidecar-build`에는 조건이 없어 수동 실행 시 이들도 같이 돌아
  비용 절감이라는 애초 목적을 일부 훼손함(위험 하로 지적) —
  세 job 모두에 `if: github.event_name != 'workflow_dispatch'`를
  추가해 수동 트리거 시 `installer-build`만 돌도록 격리.

**7b M4 실제로 한 것** (2026-08-06, 위 계획의 항목 4, CFL-09 해소): "계속
진행해줘"로 착수. 항목 4가 명시적으로 요구한 "`scripts/
package_platforms.py`의 위치(완전 대체 vs 유지) 결정"을 먼저 사용자에게
확인 — **둘 다 유지(권장안)** 로 결정, **DEC-23**으로 기록(CFL-09 해소,
`flutter-mapping.html#s1c`).
- `docs/release-process.md` 전면 재작성: "release는 이제 병행 트랙
  둘"(1) 기존 소스 zip(`scripts/package_platforms.py`, 터미널/CLI 전용,
  git 불필요하지만 사용자 자신의 Python 3 필요) — 그대로 유지, (2) 신규
  Tauri 설치형 산출물(`cargo tauri build`, Python 번들링돼 있어 사용자
  Python 설치 불필요, 데스크톱 GUI 전용, 현재 미서명 — 7c/DEC-22
  그대로) — 로 구조를 다시 씀. 버전 범프 단계에 `src-tauri/
  tauri.conf.json`의 `version` 필드를 `BRIDGE_VERSION`과 수동 동기화하는
  단계 추가(자동 동기화 메커니즘은 없음). 설치형 산출물 빌드 단계는
  `installer-build`가 `workflow_dispatch` 전용이라는 걸 그대로 반영해
  `gh workflow run` → `gh run watch` → `gh run download`로 실제
  트리거·대기·다운로드하는 명령을 문서에 명시. GitHub Release 발행
  단계에 zip과 함께 OS당 대표 설치형 파일 하나씩(`.dmg`/nsis
  `.exe`/`.AppImage`)만 첨부하도록(나머지 포맷은 요청 시 개별
  업로드) 정리 — 산출물이 6종(포맷 3개 × macOS 제외 시 아니고, 실제로는
  macOS 2 + Windows 2 + Linux 3 = 7종)이라 전부 붙이면 릴리즈 페이지가
  지저분해짐.
- 연쇄 갱신: `docs/index.md`(Release Process 한 줄 설명을 두 트랙
  기준으로), `docs/cli-reference.md`("Platform Packages" 절에 설치형
  트랙은 별도 CI job이라는 안내 추가), `docs/platform-setup.md`("Build
  Zip Packages" 절 끝에 이 문서는 터미널/CLI 경로만 다룬다는 점과
  GUI 경로 포인터 추가), `docs/security-model.md`("Tauri Shell
  Boundaries" 절에 설치형 산출물이 현재 미서명이라는 사실 — Gatekeeper/
  SmartScreen 경고가 뜨는 게 정상 동작임 — 을 새 항목으로 명시. 이전엔
  이 문서 어디에도 서명 상태 언급이 아예 없었음). `README.md`의
  Download 절은 건드리지 않음 — 아직 실제 태그된 릴리즈에 설치형
  산출물이 첨부된 적이 없어서, 미리 광고하면 과장이 됨(자체 판단, 별도
  확인 없이 보수적으로 결정).
- `flutter-mapping.html`: CFL-09를 Conflict List(§2)에서 제거하고
  Decision Log(§1c)에 **DEC-23**으로 추가. DEC-01 행의 "CFL-09 신규
  파생" 링크와 §1a의 프레임워크 비교 표(당초 "Python 단일 zip 배포가
  더 이상 성립하지 않음"이라던 우려)도 DEC-23으로 해소됐음을 반영해
  갱신 — 죽은 앵커 링크(`#s2`의 CFL-09 행)가 안 남도록.
- 이 섹션 바로 아래 Phase 7 요약 표의 CFL-09 상태도 "7b에서 해소
  예정" → 해소로 갱신(아래 표 참고).
- **PR 오픈 전 self-review에서 잡힌 실제 버그**: 처음 쓴 초안은 "5.
  설치형 산출물 빌드"를 "6. 커밋·태그·푸시"보다 **앞에** 배치했음 —
  `installer-build`는 `workflow_dispatch` 시점에 지정된 ref(기본
  `main`)를 그대로 빌드하는데, 버전 범프 커밋이 아직 push되지 않은
  상태라 설치형 산출물이 새 버전이 아니라 **직전 버전**으로 만들어지는
  실제 순서 버그였음 — 두 트랙이 같은 릴리즈에서 버전이 어긋나는 결과.
  순서를 뒤바꿔 커밋·태그·푸시를 먼저 하고, `gh workflow run` 대상도
  `--ref main`이 아니라 방금 만든 **태그**(`--ref vX.Y.Z`)로 지정하도록
  수정 — 태그된 커밋 그대로 빌드된다는 보장이 더 명확해짐. 또한
  `actions/upload-artifact@v4`가 여러 glob 패턴의 공통 상위 경로 아래
  구조를 그대로 보존한다는 실제 동작을 놓치고 있었음(`gh release
  create`의 asset 경로가 평평한 디렉터리를 가정했지만 실제로는
  `installers-<triple>/<포맷>/<파일>`처럼 한 단계 더 들어감) — CI의
  실제 `--add-data`/`path:` 정의와 대조해 경로 수정. `gh run list`도
  `--event workflow_dispatch` 필터와 태그 이름 매칭이 없어 동시에 다른
  트리거(예: push)가 돌면 엉뚱한 run을 집을 수 있었음 — 필터·태그
  매칭·짧은 폴링 루프 추가. 이 설치형 트랙 런북 자체는 실제 태그된
  릴리즈가 한 번도 없어서(`gh release list` 확인) 아직 end-to-end로
  실행해본 적이 없다는 점을 문서에 그대로 명시.

**7b M6 실제로 한 것** (2026-08-06, 위 계획의 항목 6, 7a가 남긴 두 후속
항목 검증): "다음으로 진행해줘"로 착수. M5(코드 서명)는 7c로 분리된
별도 게이트라 건너뛰고, 순서상 다음인 M6부터 — 실제로 앱을 빌드·실행해
두 가지를 직접 검증(`src-tauri/src/lib.rs`, 코드 변경 포함).
- **sidecar 종료 정리: 실제로 깨져 있었음, 수정함.** 검증 도중 실제
  증거를 우연히 발견 — 포트 충돌 테스트를 준비하며 `lsof -i :8787`을
  돌렸더니 이미 다른 프로세스가 포트를 쥐고 있었고, 확인해보니 그
  프로세스의 부모가 `launchd`(PID 1)였다. 즉 이 세션 초반(7a 작업
  당시로 추정) 실행했던 Tauri 앱은 이미 오래 전에 종료됐는데, 그 앱이
  띄운 sidecar(`agent-handoff-bridge-server`)는 몇 시간째 고아
  프로세스로 계속 살아 포트를 쥐고 있었음. 원인: `sidecar.spawn()`이
  돌려주는 `CommandChild`를 `_child`로 즉시 버렸던 게 문제 —
  `tauri-plugin-shell`의 `CommandChild`는 drop돼도 프로세스를 죽이지
  않는다. 고친 방법: `CommandChild`를 Tauri managed state
  (`Arc<Mutex<Option<CommandChild>>>`)에 보관해두고, 앱 종료 시점에
  꺼내 죽이도록 훅을 추가. 이 훅을 어디 걸어야 하는지도 실제로
  틀렸다가 고쳤음 — 처음엔 `RunEvent::ExitRequested`에 걸었는데,
  실제 `.app`을 빌드해 `osascript -e 'tell application ... to quit'`
  (Accessibility 권한이 필요한 UI 스크립팅이 아니라 Apple Event 기반
  quit이라 이 개발 환경의 접근성 권한 제약을 피해갈 수 있었음)로
  진짜 종료시키면서 발생하는 모든 `RunEvent`를 로그로 찍어보니
  macOS에서는 `ExitRequested`가 전혀 발생하지 않고 곧바로 `Exit`만
  발생함을 확인 — 훅을 `RunEvent::Exit`으로 교체. 그 다음 문제:
  `CommandChild::kill()`(Rust 표준 `Child::kill()`, 즉 SIGKILL)을
  걸어도 sidecar가 여전히 고아로 남았음 — PyInstaller onefile
  바이너리는 겉보기엔 프로세스 하나지만 실제로는 바깥쪽
  부트로더(Tauri가 직접 잡고 있는 PID)가 안쪽에 압축 해제된 실제
  인터프리터를 별도 자식 프로세스로 재실행하는 2단 구조라(`ps`로
  ppid 체인 확인), SIGKILL은 바깥쪽만 즉사시키고 안쪽은 아무 신호도
  못 받은 채 다시 고아가 됨. 흥미로운 대조 실험: 같은 바깥쪽 PID에
  `kill`(기본 SIGTERM)을 손으로 보냈을 땐 안쪽까지 같이 죽는 걸
  확인했지만("정상적인" 부트로더의 신호 전달에 의존하는 셈이라
  안전하지 않다고 판단), 그래서 최종적으로는 명시적 트리 kill로
  구현: Unix는 `pkill -P <pid>`, Windows는 `taskkill /T /F /PID
  <pid>`(자식을 부모보다 먼저 죽여야 함 — 부모가 먼저 죽으면 자식의
  ppid가 launchd/init으로 바뀌어 `-P` 매칭이 깨짐). macOS에서 종료→
  프로세스 확인을 두 번 반복해 재현성 확인, 매번 고아 없이 완전히
  정리됨(`ps`/`lsof -i :8787` 둘 다 깨끗) — **단, 이건 유휴 상태(sidecar
  1개만 떠 있는 경우)에 한한 결과였음. 아래 self-review 항목 참고.**
- **포트 8787 충돌 처리: 이미 멈추지는 않았지만(제너릭 에러로 뜨긴
  함), 메시지를 구체화함.** `handoff_webui.py`의
  `ThreadingHTTPServer(...)` 생성 호출엔 try/except가 없어서, 포트가
  이미 사용 중이면 처리되지 않은 `OSError: [Errno 48] Address already
  in use` 트레이스백이 그대로 stderr로 나가고 sidecar가 exit code
  1로 죽는다 — 실제로 포트를 미리 점유시켜놓고 두 번째 인스턴스를
  띄워 재현 확인. 이 경우 Rust 쪽은 이미 `CommandEvent::Terminated`
  분기에서 `fatal_startup_error()`로 대화상자를 띄우고 종료하고
  있었으므로 **무한 대기/조용한 실패는 원래도 아니었음** — 다만
  메시지가 "The app's local server exited before it was ready"라는
  범용 문구뿐이라 사용자가 원인(포트 충돌인지 다른 문제인지)을 알 수
  없었음. `CommandEvent::Stderr` 라인에서 `"Address already in
  use"` 문자열을 감지하는 플래그를 추가해, 감지되면 "다른 인스턴스가
  이미 실행 중일 수 있습니다" 문구로 구체화된 메시지를 보여주도록
  수정 — 실제 재현 중 화면에 뜬 대화상자 내용을 사용자가 그대로
  확인해줌.
- 두 문제가 실전에서는 서로 얽혀 있었다는 점도 기록: #1(정리 안 됨)이
  고쳐지지 않았다면, 앱을 재실행할 때마다 이전 인스턴스의 orphan이
  포트를 쥐고 있어 #2(충돌 메시지)가 사실상 매번 발생했을 것 — #1을
  고친 게 #2의 실질적 발생 빈도도 크게 낮춘다.
- 로컬 macOS에서 `cargo tauri build --debug`로 실제 `.app` 빌드 →
  실행 → 종료를 반복 재현해 검증(위 두 항목 모두, 유휴 상태 기준).
  `cargo build` 컴파일 체크와 `python3 -m unittest discover`(365개)·
  `handoff_bridge.py check`·`scan_secrets.py` 모두 통과.

**PR 오픈 전 self-review에서 잡힌 것 (코드로는 수정, 라이브 재검증은
안 함 — 로컬 리소스 사용량 우려로 반복 빌드/앱 실행 테스트를 일시
동결하기로 사용자와 합의한 뒤 진행)**:
- **[위험 높음] `pkill -P`는 1단계만 도달, 실행 중인 provider run은
  여전히 orphan 남을 수 있음.** 실제 프로세스 트리는 테스트했던 것보다
  더 깊음 — `handoff_webui.py`의 `bridge_command_prefix()`가 init/run
  시점에 **두 번째** PyInstaller sidecar(`agent-handoff-bridge-cli`)를
  shell out으로 띄우고, 그게 또 재실행(re-exec)한 뒤 실제
  `codex`/`claude`/`gemini` 서브프로세스를 띄운다 — 4세대 깊이. 이 중
  어느 것도 최초 추적 PID의 **직접** 자식이 아니라서, provider 실행
  중에 앱을 끄면 여전히 고아가 남는다(실행 중인 provider CLI까지
  포함해서). Windows의 `taskkill /T`는 재귀적이라 이 문제가 없을
  가능성이 높음(미검증) — Unix/Windows 비대칭. 수정: `pgrep -P`로
  트리 전체를 먼저 다 찾아낸 뒤(한 프로세스가 죽으면 그 자식은 더
  이상 `-P`로 못 찾으므로 먼저 다 찾아야 함), 발견 역순(자식부터)으로
  `kill -9`. `descendant_pids_unix()` 신규 함수.
- **[위험 중간] 포트 충돌 메시지 매칭이 POSIX 전용이라 Windows에서는
  죽은 코드였음.** `"Address already in use"`는 macOS/Linux
  `OSError` 텍스트고, Windows의 `WSAEADDRINUSE`는
  `"[WinError 10048] Only one usage of each socket address..."`로
  렌더링돼 이 문자열을 포함하지 않는다 — Windows에서는 개선된
  메시지가 절대 안 뜨고 항상 제너릭 메시지로 폴백. 수정:
  `"Only one usage of each socket address"`와 숫자 에러 코드
  `"10048"`(Windows 에러 *텍스트*는 시스템 언어에 따라 로컬라이즈될
  수 있지만 숫자 코드는 그렇지 않음)도 함께 매칭하도록 확장.
- 두 수정 모두 `cargo build --manifest-path src-tauri/Cargo.toml`
  컴파일 체크(더미 sidecar 사용, `rust-build` CI job과 동일 방식)만
  통과 확인 — **실제 `.app` 빌드·실행·종료 반복 재현은 이번엔 하지
  않음**(로컬 리소스 부하 우려로 동결). 특히 `descendant_pids_unix()`
  기반 트리 kill이 실제로 4세대 깊이 프로세스까지 다 잡는지, 그리고
  Windows `taskkill /T`가 실제로 재귀 동작하는지는 **여전히
  라이브로 검증된 적이 없음** — 다음 실제 빌드·설치형 릴리즈 테스트
  때 반드시 확인 필요. Windows/Linux에서 동일한 트리 종료 처리가
  실제로 동작하는지는 이 macOS 개발 환경에서 애초에 재현 불가 — CI에는
  이 시나리오를 실행하는 job이 없어(설치형 앱을 실제로 띄우고 종료하는
  건 CI에서 자동화하기 어려움) 다음 실제 Windows/Linux 설치형 릴리즈
  수동 테스트 때 확인 필요.

**7a 실제로 한 것**:
- `src-tauri/`: `cargo tauri init`으로 스캐폴딩(바닐라 JS 템플릿,
  `frontendDist`는 `../webui`를 가리키지만 실제로는 사용되지 않음 —
  아래 이유). `tauri.conf.json`의 `app.windows`는 **의도적으로
  비워둠** — 창을 정적으로 선언하면 sidecar가 실제로 준비되기 전에
  즉시 그 URL로 첫 내비게이션을 시도하고, 실패하면 다시 시도하지
  않는다(PyInstaller onefile 바이너리의 실제 시작 비용 — 압축 해제 +
  Python import — 을 실제로 빌드한 `.app`을 직접 띄워보고서야
  발견). 대신 `src-tauri/src/lib.rs`가 `agent-handoff-bridge-server`
  sidecar를 spawn하고, 그 stdout이 `handoff_webui.py main()`이 이미
  `ThreadingHTTPServer(...)` 바인딩 뒤에 찍는 준비 완료 신호 문자열을
  포함할 때만 `WebviewWindowBuilder`로 창을 **그때 처음** 만든다 —
  `http://127.0.0.1:8787/`(포트 고정, `--port` 기본값과 일치)로.
  이것도 실제로 빌드해 띄워보고서야 발견한 문제: stdout이 파이프로
  연결되면 CPython의 stdio가 줄 단위가 아니라 완전 버퍼링으로 바뀌어,
  이 준비-신호 print가 버퍼에 갇힌 채 Rust 쪽 `CommandEvent::Stdout`에
  전혀 도달하지 않을 수 있었다. sidecar spawn에 `PYTHONUNBUFFERED=1`을
  넘기는 것으로 먼저 시도했으나, 실제로 빌드한 PyInstaller onefile
  바이너리로 다시 테스트해보니 이 환경변수만으로는 안정적으로 해결되지
  않았다(bootloader의 자체 환경변수/재실행 처리 때문으로 추정) — 최종
  수정은 `handoff_webui.py`의 `main()` 맨 앞에서 직접
  `sys.stdout.reconfigure(line_buffering=True)`를 호출하는 것이었고,
  이건 실제로 리다이렉트된 파일로 즉시 확인함. `PYTHONUNBUFFERED=1`은
  해가 없어 그대로 남겨둠(다른 여러 Python 도구가 존중하는 표준
  신호이므로).
- **sidecar 4개**, 전부 PyInstaller `--onefile`: `agent-handoff-bridge-server`
  (`handoff_webui.py`), `agent-handoff-bridge-cli`(`handoff_bridge.py`),
  `agent-handoff-bridge-validate`(`scripts/validate_handoff.py`),
  `agent-handoff-bridge-scan`(`scripts/scan_secrets.py`) — CLI가
  `check` 실행 시 validate를, validate가 secret scan 시 scan을 필요로
  하는 실제 호출 사슬을 그대로 따라간 결과. `tauri.conf.json`의
  `bundle.externalBin`에 네 개 전부 등록 — server만 등록하면 Tauri가
  나머지 셋을 최종 `.app`에 아예 안 담아서, 로컬 테스트에선 되다가
  실제 패키징된 앱에선 깨지는 함정.
- **`sys.executable` + 스크립트 경로로 서로를 subprocess 호출하던
  실제 버그 4곳**(이 프로젝트에 이미 있던 패턴 — frozen 상태에선
  `sys.executable`이 진짜 Python 인터프리터가 아니라 그 바이너리
  자신이라 완전히 다르게 동작함): `handoff_webui.py`의
  `bridge_command_prefix()`(새 헬퍼, `init`/`run`이 CLI sidecar를
  올바르게 찾도록), `handoff_bridge.py`의 `check()`(validate
  sidecar), `scripts/validate_handoff.py`의 `check_secrets()`(scan
  sidecar) — 셋 다 `getattr(sys, "frozen", False)`로 분기해 frozen일
  땐 `sys.executable`과 같은 디렉터리의 형제 sidecar를 직접 실행.
  `check_tests()`(전체 개발 테스트 스위트 재실행)는 같은 방식으로
  고칠 수 없었다 — 그 테스트 스위트 자체가 `sys.executable` 기반
  서브프로세스 호출로 통합 테스트를 하기 때문에, frozen 인터프리터
  안에서 다시 실행하면 같은 문제가 재귀적으로 발생한다. 대신 frozen일
  땐 조용히 건너뛴다(실제 배포된 앱에는 재실행할 "소스 트리 테스트
  스위트" 자체가 없다는 논리 — dev/CI 전용 개념).
- `handoff_bridge.py check`가 참조하는 ~50개 파일(`INSTALL_FILES`)을
  CLI sidecar에 `--add-data`로 명시적으로 번들 — PyInstaller onefile은
  Python 코드 외 데이터 파일을 기본으로 담지 않으므로, 이게 없으면
  frozen 상태의 `init`이 새 워크스페이스에 아무것도 못 채운다.
  `check_tests()`가 동적으로 discover하는 `tests/*.py`들이 쓰는
  `unittest.mock`/`http.server` 등 표준 라이브러리 서브모듈도 정적
  분석만으론 안 잡혀 `--hidden-import`로 명시 추가.
- 새 테스트: `BridgeCommandPrefixTests`(handoff_webui.py),
  `CheckCommandTests`(handoff_bridge.py),
  `tests/test_validate_handoff.py`(신설 — 이전엔 이 스크립트에 단위
  테스트가 아예 없었음) — 전부 frozen/unfrozen 두 경로와 Windows
  `.exe` 접미사를 확인. 정확한 개수는
  `python3 -m unittest discover -s tests -v`로 확인.
- **검증**: 실제로 빌드한 `.app`을 직접 실행 — sidecar가 뜨고,
  `curl http://127.0.0.1:8787/`이 실제 프론트엔드 HTML을 반환하고,
  `POST /api/chat`으로 보낸 첫 메시지가 CLI sidecar를 통해 실제
  워크스페이스(`.handoff/current.md`/`state.json` 포함)를 만들고,
  `agent-handoff-bridge-cli check`가 전체 통과하는 것까지 전부 실제로
  확인. `.app` 자체가 macOS 프로세스 레지스트리에 `type="Foreground"`
  로 올바른 bundle ID(`com.jh3779.agenthandoffbridge`)로 등록되고
  WebKit 렌더러 프로세스가 살아있는 것도 확인. 이 개발 환경의
  Accessibility 권한 제약으로 스크린샷을 통한 직접 육안 확인은 못
  했지만(자동화 스크린샷이 계속 다른 창을 잘못 캡처함 — 시도 중
  실수로 무관한 창에 키 입력을 보낸 사고가 있어 이후 스크린샷 시도
  자체를 중단함), `tauri-plugin-log`가 always-on으로 남긴 실제 로그가
  더 강한 증거를 남겼다: 창이 뜬 뒤 `curl`이 아니라 **웹뷰 자신이**
  `GET /`·`GET /app.css`·`GET /app.js`·`GET /api/update-check`·
  `GET /api/info`를 순서대로 요청한 기록이 그대로 남음 — 이건 실제
  브라우저/웹뷰가 HTML을 파싱하고 그 안의 CSS/JS를 로드하고 앱 자신의
  초기화 API까지 호출했다는 뜻이라, 스크린샷 없이도 사실상 결정적인
  증거. 그래도 사용자가 직접 한 번 열어 확인하는 것을 권장.

---

## 상태 추적

| Phase | 상태 | 해소하는 항목 |
|---|---|---|
| 0 — 로컬 MVP | ✅ 완료 | — |
| 1 — Provider 연결(CLI) | ✅ 완료 | CFL-01, CFL-03, DEC-02/03 적용 |
| 2 — 자동 폴더 생성 | ✅ 완료 | SCR-05 구현, DEC-04~07 적용 |
| 3 — 멀티 프로젝트 히스토리 | ✅ 완료 | CFL-10/16 해소, DEC-08~12 적용 |
| 4 — API 키 모드 | ✅ 완료 | CFL-12 해소, DEC-13~16 적용 (CFL-17 후속 발견 → DEC-21로 별도 해소) |
| 5 — Gemini + provider 확장성 | ✅ 완료 | CFL-13 해소, DEC-17/18 적용 |
| 6 — 자동 업데이트 확인 | ✅ 완료 | CFL-11 해소, DEC-19 적용 (CFL-18 후속 발견 → DEC-20으로 별도 해소) |
| 7 — 프레임워크 전환 | ✅ **완료** (7a·7b, 2026-08-06) · 7c(코드 서명)는 DEC-24로 "하지 않는다" 최종 결정 | CFL-06(실행), CFL-09 해소·DEC-23 적용, CFL-14 해소·DEC-22 적용 |

이 표가 정본은 아니다 — 각 phase가 끝나면 여기 상태만 갱신하고, 실제
해소 근거는 [flutter-mapping.html Conflict List](flutter-mapping.html#s2)
쪽에서 그 항목을 지우거나 갱신한다.
