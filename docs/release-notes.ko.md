# 릴리스 노트 (한글)

이 문서는 [`docs/release-notes.md`](release-notes.md)의 한글 번역본입니다.
영어 원문이 정본(source of truth)이며, 이 문서는 `ko-operator-guide.md`와
같은 방식으로 별도 파일로 병기됩니다.

## v0.2.0 — 2026-08-06

- **웹 UI MVP**:
  - `handoff_webui.py` 추가 — `docs/design-system/`의 v0.2 채팅 재설계
    콘셉트를 위한 로컬 읽기 전용 stdlib HTTP 서버. 아직 provider는 호출하지
    않고, 워크스페이스 파일 탐색과 드래그/클릭으로 파일 첨부만 가능;
  - `webui/index.html`, `webui/app.css`, `webui/app.js` 추가;
  - 경로 탈출 공격에 안전한 `/api/tree`·`/api/file` 엔드포인트 추가
    (`safe_join()`) — `tests/test_handoff_webui.py`에 실서버 통합 테스트와
    심볼릭 링크 탈출 테스트 포함해서 커버;
  - 이 작업 중 실제로 발견한 `install`/`check` 간극 수정: `check`는
    `docs/release-process.md`를 필수 파일로 요구했지만 `install`은 이 파일을
    복사하지 않아서, 다운로드받은 사용자가 `install` 후 `check`를 돌리면
    바로 실패했음;
  - `pywebview`를 통한 선택적 네이티브 앱 창 지원 추가(`choose_ui_mode()`,
    `--browser`/`--no-browser` 플래그) — MVP를 브라우저 탭이 아니라 실제
    프로그램처럼 테스트할 수 있게 됨, `pywebview` 미설치 시 브라우저 탭으로
    자동 대체; macOS에서 실제 창이 렌더링된 스크린샷까지 찍어서
    end-to-end로 검증(코드 리뷰만으로 끝내지 않음);
  - VS Code 스타일 **Open Folder** 추가: 워크스페이스가 이제 실행 중에
    전환 가능함(`POST /api/open-folder`, `AppState`) — 시작 시 지정한
    `--workspace`에 고정되지 않음. `pywebview`의 JS-API 브리지
    (`Api.pick_folder()`)로 네이티브 OS 폴더 선택창을 띄우고, 일반 브라우저
    모드에서는 수동 절대경로 입력으로 대체;
  - 워크스페이스별 로컬 채팅 기록 추가: 메시지가
    `<workspace>/.handoff/webui/chat/YYYY-MM.jsonl`에 저장됨(`POST`/`GET
    /api/chat`) — 이미 `.handoff/current.md`가 그러듯 프로젝트 폴더와 함께
    이동함; 지난 달 기록은 자동으로 gzip 압축(`archive_old_months()`)돼서
    기록이 무한정 커지지 않음;
  - `.handoff/webui/chat/`을 `.handoff/.gitignore`에 추가 — 로컬 채팅
    초안은 기본적으로 커밋되지 않음;
  - 새 테스트 19개(채팅 저장소, 워크스페이스 후보 검증, 새 엔드포인트
    2개에 대한 독립적인 실서버 커버리지) — 총 92개.

- **웹 UI Phase 1** (provider 연결, 위의 "아직 provider는 호출하지 않음"을
  대체):
  - `POST /api/run` 추가: `handoff_bridge.py run <provider> --execute
    --auto-fallback`를 서브프로세스로 실행(인프로세스 호출이 아님 —
    `chdir_workspace()`의 cwd 상대경로는 `ThreadingHTTPServer` 요청
    스레드에서 안전하게 호출할 수 없음)하고, 호출 전후로
    `.handoff/state.json`의 `history[]`를 비교해서 새로 생긴 기록을
    읽어옴(한 번의 auto-fallback 체인이 만든 기록 전부 포함);
  - `classify_run_status()`가 `classify_handoff()`의 `(handoff_needed,
    reason)`을 `success`/`handoff`/`fail`로 매핑, 각 agent 메시지에 실제
    상태 배지로 렌더링;
  - DEC-02(브라우저 세션당 첫 전송만 확인 후 즉시 실행)와 DEC-03(펜스
    ```코드``` 블록만 모노스페이스로 렌더링, `textContent`만 사용 —
    provider 응답은 완전히 신뢰할 수 없는 입력이라 `innerHTML`은 절대 사용
    안 함) 둘 다 설계뿐 아니라 실제로 구현됨;
  - 타이틀바에 provider 선택기(`auto`/`codex`/`claude`) 추가;
  - `append_chat_message()`에 `"agent"` role 추가(`provider`/`status`/
    `reason` 필드, agent 전용) — 기존 `user`/`system`과 함께;
  - `PATH`에 가짜 `codex`/`claude` 셸 스크립트를 심어서 검증 — 결정적이고
    토큰도 안 쓰고 네트워크도 안 씀 — 실제 auto-fallback 체인(rate-limit
    걸린 codex → 성공한 claude)이 한 번의 호출로 기록 2개·agent 메시지
    2개를 만드는 것까지 포함(`RunProviderViaBridgeTests`,
    `ApiRunLiveServerTests`);
  - 병합 후 발견한 실제 CI 전용 버그 수정: 프롬프트를 `handoff_bridge.py`의
    argv 끝에 위치 인자로 붙였는데, `--instruction-type <값>` 뒤에 끼워
    넣었을 때 Python 3.11의 argparse가 이를 거부함("unrecognized
    arguments") — 3.14는 받아들였지만 3.11은 아니었음. `run_provider_via_
    bridge()`는 이제 프롬프트를 임시 파일에 써서 `--prompt-file`로 넘김 —
    이 문제와 별개의 argv 길이/프로세스 목록 노출 우려도 함께 회피;
  - 실제 간극 수정: `POST /api/chat`가 클라이언트로부터 `role: "agent"`를
    받아들여서, 실제로 provider가 실행된 적 없이 순수 POST 요청만으로 가짜
    agent 응답을 위조할 수 있었음 — "`POST /api/run`만 agent 메시지를
    쓴다"는 문서화된 계약을 위반. 이제 400으로 거부됨
    (`CLIENT_WRITABLE_CHAT_ROLES`);
  - 실제 간극 수정: 웹 UI의 600초 타임아웃이 바깥쪽 `handoff_bridge.py`
    래퍼만 죽이고 그것이 띄운 실제 codex/claude 자식 프로세스는 못
    죽였음 — `subprocess.run()`은 직계 자식에게만 신호를 보내고, 두
    프로세스 다 자기 프로세스 그룹에 속하지 않아서, 멈춘 provider가 웹
    UI가 포기한 뒤에도 계속 돌면서(토큰도 계속 쓰면서) 살아있을 수
    있었음. 이제 `--timeout-seconds`가 실제 provider 서브프로세스에까지
    전달돼서 진짜로 종료시킬 수 있음; 바깥쪽 래퍼는 더 넓은 강제종료
    안전판을 유지(`OUTER_SUBPROCESS_TIMEOUT_SECONDS = 600 * 2 + 60` —
    연속된 auto-fallback 타임아웃 2번 + `handoff_bridge.WriteLock` 경합
    시간을 고려한 값), 이 안전판이 fallback 도중 발동하면 첫 응답만
    조용히 보여주는 대신 "시간 초과" 합성 agent 메시지를 추가;
  - `--timeout-seconds`를 실제로 전달하게 만들고 나서 발견한 실제 크래시
    수정: `subprocess.TimeoutExpired.stdout`/`.stderr`는 `text=True`여도
    여전히 `bytes`일 수 있음 — CPython의 `_communicate()`가 성공 경로에서만
    디코딩하고 타임아웃 경로에서는 안 함 — 그래서 부분 JSONL 출력 도중
    타임아웃난 provider가 기록을 저장하기도 전에 `run_provider()`를
    크래시시켰음(`decode_timeout_output()`, `handoff_bridge.py`);
  - 실제 스키마 위반 수정: `run_provider_via_bridge()`의 히스토리 없음
    합성 실패 기록이, 호출자가 `"auto"`를 요청했는데 서브프로세스가 실제
    기록이 생기기도 전에 실패하면 `provider: "auto"`라는 문자열을 그대로
    영구 저장할 수 있었음 — `docs/webui-chat-storage.md`의 스키마는
    `provider`가 "절대 `auto`가 아님"이라고 명시함; 이제 이 경로에서도
    `choose_auto_provider()`로 해소됨;
  - 실제 UX 간극 수정: 첨부파일이 `POST /api/chat`(채팅 로그)까지는
    도달했지만 `POST /api/run`에는 전달되지 않았음 — provider는 사용자가
    첨부했다고 생각한 파일을 전혀 못 봤고, 첨부만 있고 텍스트가 없는
    전송(컴포저가 허용함)은 "text is required"로 완전히 실패했음.
    `build_run_prompt()`가 첨부파일 이름/내용을 실제 `--prompt-file`
    텍스트에 접어넣음; `/api/run`이 이제 텍스트-또는-첨부파일을 받아들임
    (텍스트 필수 아님);
  - 시작/도움말 문구와 `webui/app.js` 헤더 주석 갱신 — Phase 1이 실제로
    `POST /api/run`을 연결했는데도 여전히 "provider를 호출하지 않는다"고
    적혀 있었음;
  - 독립적인 적대적(adversarial) 리뷰에서 발견한 실제 경합(race) 수정: 두
    개의 동시 `POST /api/run` 호출(Enter 키 전송 경로는 이미 실행 중인지
    확인하지 않았고, 대기 중에 타이핑하면 비활성화된 전송 버튼이 다시
    활성화될 수 있었음)이 락 없이 `.handoff/state.json`의 히스토리 길이를
    비교해서, 나중에 끝난 호출이 이미 저장된 첫 호출의 기록을 두 번째
    agent 채팅 메시지로 중복시킬 수 있었음. 프로세스 전역
    `_RUN_LOCK`(`handoff_bridge.WriteLock`이 아님 — 경합은 별도 CLI
    프로세스 간이 아니라 한 프로세스 안의 HTTP 스레드 간이고, WriteLock의
    기본 10초 타임아웃은 provider 호출에 비해 너무 짧음)을 추가해서,
    동시에 들어온 두 번째 호출이 멈추거나 경합하는 대신 즉시 `409`
    (`RunAlreadyInProgressError`)로 실패하게 함; 컴포저도 실행 대기 중에는
    스스로 비활성화되므로 평소엔 이게 직접 발동할 일 없는 안전판임;
  - 이번 PR의 auto-fallback UX가 실질적으로 중요하게 만든, 원래 있던 실제
    버그 수정: `handoff_bridge.py`의 `--auto-fallback` 재귀 호출이 fallback
    provider를 부르기 전에 사용자의 실제 프롬프트를 "Continue after
    provider handoff."라는 리터럴 문자열로 바꿔치기했음 — rate-limit 걸린
    codex가 claude로 auto-fallback하면 claude는 사용자가 실제로 뭘 물었는지
    (또는 `build_run_prompt()`를 통한 첨부파일 내용까지) 전혀 못 봤음 —
    `run_provider()`가 이제 원래 `user_prompt`를 재귀 호출까지 그대로
    전달함;
  - 위 전부에 대한 테스트 추가 — 정확한 통과/실패 개수는 여기 박아두지
    말고 `python3 -m unittest discover -s tests -v`를 직접 돌려서 확인할
    것(테스트가 추가될 때마다 숫자가 바뀜 — 이전 리뷰에서 바로 이 문구에
    대해 지적받은 적 있음).

- **웹 UI Phase 2** (`--workspace`가 선택 사항이 됨, SCR-05):
  - 구현 전 설계 인터뷰로 코드 변경 전에 DEC-04~07을 확정
    (`docs/design-system/flutter-mapping.html#s1c`) — 리뷰 도중 실제로
    한 번 방향을 수정: DEC-04의 첫 버전("cwd가 유효하지 않을 때만 '워크스페이스
    없음'")은 사실상 거의 발동하지 않았을 것임(실행 중인 프로세스의 cwd는
    거의 항상 존재하므로) — "cwd에 아직 `.handoff/` 마커가 없을 때"로
    수정;
  - `AppState.workspace`가 이제 `Path | None`. `--workspace`를 생략하면
    cwd가 이미 초기화된 handoff 워크스페이스일 때만
    (`has_handoff_marker()`) 직접 열림; 아니면 서버가 워크스페이스 미선택
    상태로 시작함 — 임의의 cwd(예: 런처를 더블클릭한 위치)를 의도한
    프로젝트라고 함부로 가정하지 않음. 명시적으로 지정한 `--workspace`가
    존재하지 않으면 여전히 예전처럼 바로 실패함 —
    `resolve_startup_workspace()`;
  - `workspace is None`일 때 모든 GET 엔드포인트가 크래시 대신 우아하게
    저하됨: `/api/info`는 `{workspace: null}` 반환, `/api/tree`와
    `/api/chat`는 빈 결과 반환, `/api/file`과 `/api/run`은 명확한 400
    반환;
  - 워크스페이스 미선택 상태에서 첫 메시지를 보내면(첨부만 있는 전송
    포함) `~/Documents/Agent Handoff Bridge/<날짜>-<슬러그>/`를 자동
    생성하고 수동으로 고른 폴더와 똑같이 세팅됨 —
    `create_workspace_for_first_message()`가 (`run_provider_via_bridge()`가
    이미 그러듯) chdir 안전성 문제로 `handoff_bridge.py init`을
    서브프로세스로 호출(표준 파일도 함께 설치됨);
  - `slugify_for_folder_name()`은 로컬 전용(폴더 이름 짓는 데 provider를
    호출하지 않음)이고 유니코드 인식(`\w`)이라 한글 텍스트가 온전히
    남음(ASCII 전용 슬러그 라이브러리처럼 잘려나가지 않음); 이름 충돌 시
    숫자 접미사가 붙고, 기존 폴더를 재사용하는 일은 없음;
  - "새 폴더 자동 생성" 버튼은 실제로 아무것도 만들지 않음 — 생성은 처음
    보내는 메시지 시점까지 완전히 미뤄져서, 버튼-먼저와 메시지-먼저 UI
    경로가 별도 코드 없이 한 트리거로 수렴함;
  - 새 테스트 28개로 검증(해석/슬러그/명명 로직에 대한 순수 함수 테스트,
    `AUTO_WORKSPACE_BASE_DIR`을 임시 디렉터리로 패치해 실제 디렉터리 생성을
    검증하는 스위트, `AppState(None)`로 부팅한 실서버 스위트) + 실제
    end-to-end 실행: `$HOME`을 임시 디렉터리로 바꿔서 수동 스모크 테스트가
    실제 `~/Documents/`를 건드리지 못하게 한 뒤, 한글 첫 메시지가 정확한
    이름의 완전히 세팅된 워크스페이스를 만드는지 확인;
  - 독립적인 적대적 리뷰에서 발견하고 **재현까지 한** 실제 경합 수정:
    `POST /api/chat`의 확인-후-생성 로직(`if state.workspace is None: ...
    state.workspace = create_workspace_for_first_message(...)`)에 락이
    없었음 — `/api/run`의 `_RUN_LOCK`과 달리. 거의 동시에 온 첫 메시지
    두 개(더블클릭한 전송, 같은 서버를 향한 브라우저 탭 두 개)가 둘 다
    `None`을 관찰하고 둘 다 실제 폴더를 디스크에 만들 수 있었음 —
    수정 전에 실제 서버에 동시 스레드로 요청을 보내는 스크립트로
    확인함(이론적 우려가 아님). 획득 후 `state.workspace`를 다시 확인하는
    이중 확인 락(`_WORKSPACE_CREATE_LOCK`)으로 수정 — 경합에서 진 요청은
    그냥 승자가 이미 만든 워크스페이스를 사용함;
  - 같은 경합이 드러낸 관련 간극 수정: `create_workspace_for_first_
    message()`가 `handoff_bridge.py init` 서브프로세스의 결과를 전혀
    검사하지 않았음 — 실패(권한 문제, 디스크 가득 참)나 30초 넘는
    타임아웃이 조용히 계속 진행되거나(타임아웃의 경우엔 잡히지 않고
    크래시), 그 후 `append_chat_message()`가 `.handoff/state.json`조차
    없을 수 있는 폴더에 기록할 수 있었음. 이제 결과를 검사해서 명확한
    `WorkspaceError`로 드러내고, 절반만 생성된 디렉터리는 고아로 남기지
    않고 정리함;
  - 위에 대한 테스트 7개 추가(실서버 대상 실제 동시 요청 테스트, 수정 전엔
    실패하는 것 확인) — 총 175개;
  - 리뷰를 통해 `create_workspace_for_first_message()`를 더 강화: "성공"
    (exit 0)한 `init`도 이제 `.handoff/state.json`**과**
    `.handoff/current.md`를 실제로 만들었는지 확인한 후에야 워크스페이스로
    인정함 — exit code만 믿지 않음(`init_handoff()`는 성공 시 둘 다
    무조건 쓰므로, exit 0인데 둘 중 하나라도 없다면 뭔가 어긋난 것이라
    조용히 진짜 워크스페이스로 취급하면 안 됨);
  - 실제 간극 수정: 첨부만 있는 첫 메시지(컴포저는 텍스트 없이 전송을
    허용함)는 의미 있는 폴더 이름(첨부파일 이름으로 대체)을 받았지만,
    `.handoff/state.json`에 기록되는(그리고 이후 모든 프롬프트의 "## Task"
    섹션에 들어가는) *task*는 여전히 범용 "Continue the current handoff
    task." 플레이스홀더로 대체됐음 — 두 대체 로직이 서로 다른 코드를 쓰고
    있었기 때문. `resolve_task_for_first_message()`가 이제 폴더 이름과
    같은 요약 소스를 재사용함;
  - 문서 불일치 수정: `docs/design-system/roadmap.md`가 "사전 인터뷰 8건 →
    DEC-04~07"이라고 적어서 8 대 4가 안 맞는 것처럼 읽혔음 — 8은 3라운드에
    걸친 인터뷰 *질문* 수이고 4개의 *결정*(DEC-04~07)으로 정리됐다는 걸
    명확히 하도록 재작성;
  - 테스트 6개 추가 — 총 181개;
  - 네 번째 리뷰 라운드에서 실제 간극 2개 더 발견해 수정:
    `AUTO_WORKSPACE_BASE_DIR.mkdir()`/`new_workspace.mkdir()`이
    `create_workspace_for_first_message()`의 `try` 블록 밖에 있었음 — 여기서
    발생한 `OSError`(base 디렉터리가 실제로는 *파일*이거나, 권한 문제,
    디스크 가득 참)가 다른 모든 실패 경로가 만드는 깔끔한 `WorkspaceError`
    → 400 JSON 대신 그대로 전파됐음; 또한 `task`가 `handoff_bridge.py
    init`에 `--` 옵션 종료 구분자 없이 전달돼서, 첫 메시지가 우연히
    `init`의 실제 플래그 철자(예: `--no-install`)와 똑같으면 argparse가
    이를 위치 인자가 아니라 그 옵션으로 소비해버려 세팅 자체가 완전히
    실패할 수 있었음 — 수정 전후로 CLI에서 직접 재현해 확인;
  - 테스트 2개 추가 — 총 183개.

- **웹 UI Phase 3** (여러 프로젝트를 넘나드는 히스토리 드로어, SCR-03):
  - 구현 전 설계 인터뷰로 코드 변경 전에 DEC-08~12를 확정
    (`docs/design-system/flutter-mapping.html#s1c`) — CFL-16(히스토리
    드로어 데이터 출처)과 CFL-10의 남은 부분(레지스트리 메커니즘)을 모두
    완전히 해소 — 둘 다 Conflict List에서 제거;
  - **데이터 출처** (DEC-08): 드로어는 원래 가정했던 provider 실행 기록
    (`.handoff/runs/` + `state.json`의 `history[]`)이 아니라
    `.handoff/webui/chat/` 로그를 읽음 — 와이어프레임의 문자 그대로의 사용자
    입력 텍스트는 채팅 로그에만 존재함. `pair_messages_into_turns()`가
    `user` 메시지 하나와 그 뒤에 이어진 `agent` 메시지(들)를 드로어 항목
    하나로 묶음; auto-fallback이 `agent` 응답을 여러 개 만들면 *마지막*
    응답의 provider/status가 최종값이 됨(DEC-12) — 첫 시도보다 턴이 실제로
    어떻게 끝났는지가 더 중요함;
  - **레지스트리** (DEC-09/10): `~/Documents/Agent Handoff Bridge/`(Phase
    2가 이미 앱 소유 위치로 확립한 곳, OS별 앱 데이터 경로 아님) 아래에
    작은 `registry.json`이 최근 열어본 워크스페이스를 최대 50개, LRU
    순서로 추적함 — `AppState.workspace`가 설정되는 모든 지점(`main()`의
    시작, `POST /api/open-folder`, Phase 2의 자동 생성 경로)에서 갱신됨,
    명시적 UI 동작에서만이 아님. 폴더가 더 이상 존재하지 않는 항목은
    렌더링 시점에 에러 없이 그냥 건너뜀;
  - 이번 구현 *도중* 실제로 발견해 배포되기 전에 고친 버그: 레지스트리
    파일 경로가 처음엔 모듈 레벨 상수(`REGISTRY_PATH =
    AUTO_WORKSPACE_BASE_DIR / "registry.json"`)로 임포트 시점에 한 번만
    바인딩됐음 — 테스트는 실제 `~/Documents/Agent Handoff Bridge/`를
    절대 건드리지 않으려고 `AUTO_WORKSPACE_BASE_DIR`을 임시 디렉터리로
    패치하는데, 임포트 시점에 계산된 상수는 그 패치를 못 봄 — 그래서
    레지스트리 테스트를 하나라도 작성했다면 전부 조용히 실제 경로에 썼을
    것임. 레지스트리 테스트를 작성하기 전에 미리 발견해서, 호출할 때마다
    모듈 전역을 다시 읽는 함수(`registry_path()`)로 고침;
  - **드로어 UX** (DEC-11): 현재 워크스페이스가 최근성과 무관하게 항상
    맨 위에 고정되고, 나머지는 레지스트리에서 최근 연 순서대로, 워크스페이스당
    최대 5개 턴까지. 항목을 클릭하면 새로운 "읽기 전용 세션 뷰어"가 아니라
    기존 `switchWorkspaceTo()`(Open Folder와 같은 코드 경로)를 재사용함 —
    더 단순하고, 와이어프레임의 문자 그대로의 "읽기 전용"이라는 표현은
    추가 복잡도를 들일 가치가 없다고 판단;
  - 새 `GET /api/history` 엔드포인트; `webui/index.html`/`app.js`/`app.css`에
    History 타이틀바 버튼, 슬라이드인 드로어, 스크림 추가;
  - 커밋 전 self-review에서 발견한 실제 간극 수정: `AppState.workspace`가
    설정되는 다른 두 경로(`resolve_startup_workspace()`,
    `validate_workspace_candidate()`, 둘 다 이미 `.resolve()`를 호출함)와
    달리, `create_workspace_for_first_message()`는
    `AUTO_WORKSPACE_BASE_DIR`로부터 새 워크스페이스 경로를 만들 때 이를
    resolve하지 않았음. `Path.home()` 자체는 심볼릭 링크를 resolve하지
    않음(예: iCloud Desktop & Documents 동기화 아래의 `~/Documents`) —
    같은 실제 물리 폴더라도 자동 생성 경로와 나중의 Open Folder/CLI 시작
    경로를 통해 도달하면 문자열이 다르게 나와서 레지스트리에서 하나로
    합쳐지지 않고 중복될 수 있었음. 테스트에서 실제 심볼릭 링크로
    재현했고, 수정 전엔 실패하고 수정 후엔 통과함을 확인;
  - 후속 리뷰 라운드에서 실제 간극 2개 더 발견해 수정:
    `touch_registry()`/`read_registry()`가 `OSError`(base 디렉터리가 실제로는
    파일, 권한 문제, 디스크 가득 참)를 잡지 않고 그대로 전파했음 —
    `touch_registry()`는 `POST /api/open-folder`와 `main()`에서 그것이
    붙어있는 실제 상태 변화(`AppState.workspace` 할당, 또는 서버가 막
    시작을 마치려는 시점)가 *이미 일어난 뒤*에 호출되므로, 레지스트리
    쓰기 실패가 성공한 워크스페이스 전환을 클라이언트에게 500으로
    보이게 하거나, 그냥 LRU 편의 인덱스 문제로 서버 전체 시작을 막을 수
    있었음. 이제 best-effort로 처리: 읽기 실패는 빈 목록 반환, 쓰기
    실패는 경고 로그만 남기고 리턴 — base 디렉터리가 강제로 실패하도록
    만든 상태에서도 `/api/open-folder`가 여전히 200을 반환하는 HTTP 레벨
    테스트로 검증. 별도로, `collect_recent_turns()`가 스캔한 각 달의
    메시지를 서로 독립적으로 짝지었음 — 그래서 사용자 메시지는 한 달의
    파일에, agent 응답은 다음 달의 파일에 떨어진 턴(예: UTC 월 경계에 딱
    맞춰 전송된 경우)이 provider/status 없이 표시되고 응답이 조용히
    사라졌음 — 이제 스캔한 모든 달의 메시지를 병합해 시간순으로 정렬한
    뒤 그 전체를 대상으로 짝짓기함, 재현 후 수정한 회귀 테스트로 검증;
  - `registry.json`의 스키마, 경로 정규화 계약, 50개 LRU 상한, 락킹,
    실패 격리 정책을
    [`docs/webui-chat-storage.md`](webui-chat-storage.md#recently-opened-registry-phase-3)에
    문서화 — 이 저장소에 이미 있는 실제 데이터 모델 참조 문서(새로 지어낸
    문서 아님)를 확장한 것, 문서화하지 않고 넘어가지 않음;
  - 더 많은 테스트로 검증(실패 격리를 포함한 레지스트리 CRUD, 다중
    agent 응답과 월 경계 케이스를 포함한 턴 짝짓기,
    `collect_recent_turns()`의 월별 역방향 스캔, 드로어 조립, 실제
    심볼릭 링크를 통한 경로 정규화, 레지스트리 실패 케이스를 포함한
    실서버 HTTP 통합 테스트) — 정확한 개수는 수정마다 바뀌므로 여기 박힌
    숫자보다 `python3 -m unittest discover -s tests -v`를 믿을 것(이유는
    위 Phase 1 항목 참고). 실제 end-to-end 실행도 함: `$HOME`을 임시
    디렉터리로 바꾸고, 한글 첫 메시지로 워크스페이스 하나를 자동
    생성하고, Open Folder로 두 번째를 열고, `GET /api/history`가 curl로
    확인했을 때 둘 다 올바른 순서·올바른 턴으로 보이는지 확인.

- **웹 UI 여러 phase에 걸친 견고화**: Phase 0-3이 함께 합쳐진 뒤의
  `handoff_webui.py`/`webui/*` 전체 표면을 포괄적으로 리뷰(단일 phase
  diff가 아니라 백엔드/보안, 프런트엔드/UX, 문서 정확성을 각각 담당하는
  3개의 병렬 에이전트) — 여러 phase가 동시에 살아있을 때 서로 어떻게
  상호작용하며 생기는 버그를 특별히 찾음:
  - 실제 간극 수정: `POST /api/run`이 *예전* 워크스페이스를 대상으로
    아직 진행 중일 때(최대 `OUTER_SUBPROCESS_TIMEOUT_SECONDS`, 약 21분)
    `POST /api/open-folder`가 `AppState.workspace`를 재할당하는 걸 막는
    장치가 없었음 — 실행 결과는 서버 쪽에서는 여전히 올바른(예전)
    워크스페이스의 채팅 로그에 저장되지만, 이미 화면상 스레드를 새
    워크스페이스로 전환한 클라이언트는 그 응답이 도착했을 때 엉뚱한
    프로젝트의 스레드에 이어붙이게 됨. `/api/open-folder`가 이제
    `_RUN_LOCK`을 확인해서 실행 중이면 `409`를 반환함 — `/api/run` 자체의
    동시 호출 방지 장치와 동일; `webui/app.js`의 `switchWorkspaceTo()`
    (Open Folder와 모든 History 드로어 항목 클릭이 공유)도 즉각적인
    피드백을 위해 클라이언트 쪽에 같은 `runInFlight` 가드를 얻음;
  - 같은 리뷰가 드러낸 관련 간극 수정: `POST /api/chat`의 `"user"` role은
    동등한 가드가 없어서, 다른 곳에서 실행이 진행 중인 같은
    워크스페이스에 두 번째 브라우저 탭(또는 어떤 직접 API 호출자든)이 새
    사용자 메시지를 게시할 수 있었음 — `pair_messages_into_turns()`
    (Phase 3)는 각 `agent` 응답을 채팅 로그의 추가 순서상 가장 최근에 본
    `user` 메시지에 붙이므로, 진행 중이던 실행의 응답이 두 번째 메시지
    *이후*에 로그에 도착하면 히스토리 드로어에서 엉뚱하게 그 메시지에
    귀속될 수 있었음. 이제 이것도 `409`로 거부됨(`system` role 게시는
    영향 없음 — 턴을 시작하지 않으므로);
  - `sendMessage()`의 워크스페이스 자동 생성 에러 경로에서 발견한 실제
    UX 간극 수정: 워크스페이스를 자동 생성하는 도중 `POST /api/chat`가
    실패하면, 코드가 그냥 넘어가서 대상이 없는 게 명확한데도 오래된
    `hasWorkspace` 플래그로 `POST /api/run`을 그대로 호출했음;
  - 현재 배포된 UI를 통해서는 도달할 수 없는(UI가 `model`을 절대 안
    보내므로) 잠재적 argv 간극 수정: `run_provider_via_bridge()`가
    `["--model=값"]` 대신 `["--model", 값]`을 전달했음 — 그래서 `-`로
    시작하는 모델 문자열은 argparse가 `--model`의 값이 아니라 다음
    플래그로 오인할 수 있었음 — 프롬프트(`--prompt-file`)와 `init`의
    task(`--`) argv 간극을 이전 라운드에서 막은 것과 같은 방식으로 막음;
  - 문서 정확성 점검에서 발견한 오래된 문서 주장 여러 건 수정:
    `docs/design-system/components.html`의 페이지 요약이 히스토리 드로어
    컴포넌트(§11/§13)를 "코드 없음"이라고 적어놨었음(Phase 3에서
    배포됨), `wireframes.html`의 SCR-01 태그가 여전히 "provider 연결
    제외"라고 돼있었음(Phase 1이 추가함) SCR-03(히스토리 드로어)에는
    다른 모든 배포된 화면에 있는 "실제 구현됨" 태그가 빠져 있었음,
    `cli-reference.md`의 마지막 포인터가 여전히 프로젝트 간 히스토리
    열람을 의도적으로 빠진 것으로 나열하고 있었음(Phase 3이 배포함),
    `design-system/README.md`의 페이지 색인 표가 오래된 DEC/CFL 개수와
    한 개 어긋난 화면 개수를 인용하고 있었음, CFL-14의 예시 목록이 이미
    배포된 provider 연결과 히스토리를 아직 추가될 수도 있는 것으로
    이름을 올려놨었음;
  - 위 전부에 대한 테스트 추가, 실행 중일 때 `/api/open-folder`와 두
    번째 `POST /api/chat` 둘 다 정확히 `409`를 받고 `system` role
    메시지는 안 받는다는 걸 증명하는 HTTP 레벨 테스트 포함;
  - 같은 영역에서 후속 리뷰가 발견한 실제 간극 수정: `sendMessage()`는
    `POST /api/chat`가 실패*하고* 워크스페이스가 방금 없었을 때만 `POST
    /api/run` 호출을 멈췄음 — 이미 존재하는 워크스페이스의 경우, 실패했거나
    `409`로 거부된 `/api/chat`(예: 위의 새 동시 실행 가드)는 그냥
    `/api/run`으로 넘어가버렸고, 이는 스스로도 즉시 `409`를 반환하거나
    (첫 에러 위에 더 헷갈리는 두 번째 에러가 쌓임), 최악의 경우 대응하는
    사용자 턴이 저장된 적 없는데도 agent 응답이 렌더링되고 저장되게
    할 수 있었음. 이제 자동 생성 케이스뿐 아니라 어떤 `/api/chat` 실패
    에서도 무조건 멈춤;
  - 검토했지만 의도적으로 문서화·수용하고 넘어간 것: `/api/open-folder`와
    `/api/chat`(위)의 `_RUN_LOCK.locked()` 확인은 단순 확인-후-실행이라,
    확인과 그 뒤의 상태 변화 사이의 틈에 `/api/run`이 락을 획득하는
    경우에 대해 원자적이지 않음. 이를 완전히 막으려면
    `/api/open-folder`/`/api/chat`가 `/api/run`이 쥔 것과 같은 락에서
    블로킹해야 하거나(최대 `OUTER_SUBPROCESS_TIMEOUT_SECONDS`, 약 21분 —
    이 프로젝트는 `_RUN_LOCK`이 관여하는 다른 모든 곳에서 블로킹보다
    빠른 실패 `409`를 의도적으로 선호해왔음) 세 엔드포인트 전체에 걸친
    더 무거운 공유 뮤텍스 재설계가 필요함. 단일 사용자, 단일 프로세스
    로컬 도구라는 점을 감안해, 남은 위험 구간(이번 라운드 전의 *실행
    전체 기간*에서 파이썬 바이트코드 명령 몇 개 수준으로 줄어듦)은 둘 중
    어느 쪽 트레이드오프도 감수할 가치가 없다고 판단.

- **웹 UI Phase 4** (CLI 없는 사용자를 위한 API 키 모드, SCR-06): CFL-12
  해소. `docs/research-api-key-mode.md`가 Anthropic의 Messages API도
  OpenAI의 Responses API도 CLI 경로처럼 세션 재개/파일 편집/셸 실행을
  순수 API 키 호출 뒤에 노출하지 않는다는 걸 확인한 후, 사용자와 함께
  채팅 전용 범위로 결정 — 전체 에이전트 기능 패리티는 의도적으로 미래
  phase로 연기(신규 CFL-17), 여기서는 시도하지 않음:
  - provider별 연결 패널 추가(**Diagnose** 타이틀바 버튼,
    `webui/index.html`/`app.js`/`app.css`,
    `docs/design-system/components.html` §14/wireframes.html SCR-06과
    일치) — CLI 감지됨/CLI 없음 상태를 보여주고, provider의 CLI가
    감지되지 않았을 때만 마스킹된 키(+ 선택적 모델) 입력 필드 노출;
  - `GET /api/providers`와 `POST /api/provider-key`(빈 키 = 제거) 추가;
  - `~/Documents/Agent Handoff Bridge/credentials.json` 추가(`0600` 권한,
    Phase 2/3가 이미 "앱이 소유하는 곳"으로 확립한 것과 같은 기본
    디렉터리) — `read_credentials()`/`save_credential()`는
    `read_registry()`/`touch_registry()`와 같은 실패 격리 패턴을 따름;
  - `_run_provider_via_bridge_locked()`는 provider의 CLI가 정말로
    없고(`shutil.which()`) 그 provider용으로 저장된 키가 있을 때만 새
    `run_provider_via_api_key()` 경로로 우회함 — 기존에 있던 모든 경우
    (CLI 사용 가능, 또는 CLI 없고 키도 없음)는 그대로임, 기존 테스트
    스위트 전체가 수정 없이 통과하는 것으로 확인;
  - `call_anthropic_messages_api()`/`call_openai_responses_api()`는
    `urllib`만 사용(새 의존성 없음), 작은 `_http_post_json()` 경유 —
    테스트가 실제 네트워크 호출 대신 가짜 트랜스포트로 대체 가능함, CLI
    경로가 가짜 `codex`/`claude` 스크립트로 이미 취하고 있는 것과 같은
    태도;
  - `build_api_message_history()`는 호출할 때마다 채팅 로그를 번갈아
    나오는 턴으로 재생함(최근 20개 항목으로 제한) — 두 벤더의 API 모두
    세션 기반이 아니므로 `codex exec resume`/`claude --resume`을 대신함;
  - API 키 모드 응답은 CLI 경로가 만드는 것과 완전히 같은 채팅 로그
    레코드 형태를 재사용함(그래서 `classify_run_status()`/
    `append_chat_message()`는 변경 불필요), `session_id`/`run_dir`은 항상
    `null`, `.handoff/state.json`/`current.md`는 의도적으로 건드리지
    않음 — 이 둘은 여전히 CLI-handoff 전용 영구 상태 파일로 남음;
  - 저장된 API 키는 어떤 에러 메시지/채팅 로그 텍스트/토스트에도 절대
    끼워넣지 않음 — 모든 에러 문자열은 HTTP 응답 본문이나 예외 텍스트만
    가지고 만듦, 테스트로 검증;
  - 두 provider 다 내장 기본 모델을 제공하지 않음(Claude 기본값을 잠깐
    검토/추가했다가, 아래 라운드 3에서 이게 인용 가능한 출처가 아니라 이번
    세션 자체의 내부 환경 맥락일 뿐이라는 지적을 받고 제거) — 둘 다
    추측하는 대신 연결 패널에서 명시적으로 설정해달라는 명확한 에러를
    반환함.
  - **라운드 2** (병합 전 독립적인 2차 의견 리뷰): 1차 리뷰가 놓친 실제
    간극 수정 — `build_api_message_history()`가 채팅 로그를 병합 없이
    1:1로 번갈아 나오는 턴에 매핑했는데, 하나의 CLI 턴이 연속된 `agent`
    항목 2개를 남기는 순간(`--auto-fallback` provider 체이닝) 이게
    깨졌음 — Anthropic의 Messages API는 엄격한 교대를 요구하므로 그
    워크스페이스의 다음 API 키 모드 호출이 400을 받았을 것임. 이제 연속된
    같은 role 항목을 병합함(마지막 프롬프트와의 병합 포함). 또한 수정:
    같은 함수가 *이번 달* 로그만 읽어서, 새 UTC 월의 첫 메시지(들)에서
    이전 맥락이 전부 조용히 사라졌음 — Phase 3가
    `collect_recent_turns()`에서 이미 한 번 고쳐야 했던 것과 같은 부류의
    월경계 버그 — 이제 그 함수와 같은 방식으로 달을 거슬러 스캔함. 저장된
    키에 `http.client`가 헤더 값으로 거부하는 문자(예: 임베디드
    CR/LF)가 있으면 `_http_post_json()`이 잡히지 않는 `ValueError`를 낼
    수 있었던 것도 수정 — 이제 깔끔한 에러 튜플로 변환하되,
    `http.client` 자체의 예외 텍스트(문제의 헤더 *값*, 즉 키 자체를 그대로
    담고 있음)는 절대 그대로 전달하지 않도록 주의함. 프런트엔드: 연결
    패널의 "저장" 버튼이 키 필드를 비워둔 채로 누르면(예: 모델만
    고치려고 패널을 다시 열었을 때) provider의 저장된 키를 지워버렸음
    (저장된 키는 필드에 다시 echo되지 않으므로) — 이제 아무 동작도 안
    하고, 키를 실제로 지우는 유일한 방법은 별도의 "연결 해제" 버튼임;
    패널의 새로고침에 요청 세대 가드도 추가해서, 겹치는 재렌더링(저장
    자체의 새로고침이 방금 다시 연 것과 경합)이 최신 응답 위에 오래된
    응답의 행을 렌더링하지 못하게 함. 새 회귀 테스트 5개(정확한
    개수/이름은 이 파일의 평소 관행대로 `python3 -m unittest discover -s
    tests -v`로 확인).
  - **라운드 3** (병합 전, 붙여넣기 형태의 리뷰 2건 더): 라운드 2 자체가
    만든 실제 순서 버그 1건, 실제 문서 간 일관성 간극 1건, 정당한 견고화
    요청 1건, 문서 작성 중 발견한(리뷰가 아니라) 자격 증명 쓰기 간극
    1건, 반복적으로 제기됐지만 검증 후 거짓으로 판명돼 기각한 주장
    1건:
    - 자체 유발 버그 수정: 라운드 2의 `_http_post_json()` 안 헤더 주입
      케이스용 `except ValueError:` 가드가 너무 넓어서 형식이 잘못됐지만
      200인 응답 본문에서 나오는 `json.JSONDecodeError`(`ValueError`의
      하위 클래스)까지 삼켜버렸음 — 이걸 "헤더가 거부됨"으로 잘못
      분류해서, 라운드 2에서 추가했던 호출자 쪽 `except
      json.JSONDecodeError` 처리가 도달 불가능한 죽은 코드가 됐음. 형식
      오류 본문 케이스를 성공 경로 안에서 구체적으로 먼저 잡도록
      재구성 — 더 넓은 헤더 거부 핸들러가 오분류할 기회를 갖기 전에;
    - `_http_post_json()`에 429/5xx/네트워크 일시 장애용 소규모 제한
      재시도 추가(`API_KEY_MODE_MAX_RETRIES = 2`), 숫자형 `Retry-After`
      헤더가 있으면 존중함 — `docs/research-api-key-mode.md`가 이미 공식
      SDK들은 이걸 해준다고, 직접 만든 `urllib` 클라이언트는 공짜로
      못 받는다고 적어놨었음; 리뷰가 그 조사 결과와 실제 구현 사이의
      간극을 정확히 지적함;
    - 하드코딩된 Claude 기본 모델 제거(`API_KEY_MODE_DEFAULT_MODELS`가
      이제 두 provider 다 비어있음) — 리뷰가 그 근거가 이 세션 자체의
      내부 환경 맥락일 뿐, 이 프로젝트의 다른 모델/API 주장들처럼
      외부에서 인용 가능한 날짜 있는 출처가 아니라고 정확히 지적함;
      이제 두 provider 다 추측 없이 명시적 모델을 요구함;
    - (붙여넣기 리뷰 2건이 아니라 자격 증명 저장소를 설명하는 문서를
      쓰던 중 발견) `save_credential()`의 쓰기 실패가 어디서도 잡히지
      않았음 — `touch_registry()`의 의도적인 best-effort/로그만 남기는
      태도와 달리, 저장은 `POST /api/provider-key`의 존재 이유 자체이므로
      이제 그 실패가 일반적인 `WorkspaceError` → `400`으로 드러남,
      그 요청 스레드를 응답 없이 죽이는 잡히지 않은 예외가 아니라;
    - `docs/security-model.md`의 자격 증명 경계 절과
      `docs/architecture.md`의 상태 경계 절을 확장해 API 키 모드
      예외를 명시적으로 설명함(평문 저장 트레이드오프, 저장 위치, 우선순위)
      — 둘 다 이전엔 CLI 전용 `handoff_bridge.py` 태도만 설명했는데,
      Phase 4가 그걸 반박하는 건 아니지만 그 옆에 실제로 문서화된 예외를
      추가함;
    - **검증 후 기각**: 두 붙여넣기 리뷰 다 이 변경을
      `docs/local-data-model.md`와 `docs/adr/0010-*`/`0014-*`/`0015-*`와
      맞춰달라고 요청함. 모든 브랜치에 걸쳐 `git log --all`을 확인 — 이
      저장소 역사 어느 시점에도 둘 다 존재한 적이 없고, 이 정확히 같은
      주장이 이전 PR(커밋 `e6c74c1`, "리뷰의 제안이 이 프로젝트가 쓰지
      않는 관례를 가정했음" — `docs/webui-chat-storage.md` 자체의 첫 단락이
      ADR 디렉터리를 쓰지 않기로 한 결정을 문서화함)에서 이미 한 번
      제기되고 해소된 적 있음. 이 프로젝트가 두 번이나 의도적으로
      채택하지 않은 관례를 만족시키려고 ADR 시스템이나
      `local-data-model.md`를 새로 지어내지 않음;
    - **검토했지만 바꾸지 않음**: `.handoff/current.md`의 Phase 4 항목이
      이 PR의 번호를 언급하고 "아직 병합 안 됨"이라고 적음 — 작성
      시점에는 정확했고, 다음 세션이 다시 갱신하기 전까지는 여느
      마지막-갱신 패킷처럼 병합 후엔 오래된 것처럼 읽힘(이 저장소 자체의
      `CLAUDE.md` 규약: "멈추기 전에 갱신" 이지 "항상 최신 상태 유지"가
      아님). 특정 PR 번호를 피하려고 다시 쓰는 건 한 시점의 스냅샷을
      실질적 이득 없는 더 모호한 것으로 바꾸는 것일 뿐이라 그대로 둠.
    - 이번 라운드에서 새 회귀 테스트 9개 추가.

- **웹 UI Phase 5** (세 번째 provider로 Gemini CLI 추가 + 일반화된
  fallback **대상 선택** — 완전한 N-way 재시도-소진까지-체이닝이 아님,
  auto-fallback은 여전히 정확히 한 번의 홉, 원래 2-provider 설계에서
  바뀌지 않음; "N-way fallback"이라는 표현이 왜 이렇게 명확히 정정돼야
  했는지는 아래 라운드 2 항목 참고): CFL-13 해소.
  `docs/research-gemini-cli.md`를 먼저 작성(Codex/Claude에 대한
  `docs/research.md`와 같은 규율) — Gemini가 기존 서브프로세스
  아키텍처에 잘 맞지만 무료 인증 상태 확인 명령이 없고, JSON 출력에
  세션 ID가 없고, JSONL 스트림 대신 실행당 JSON 객체 하나를 반환한다는
  걸 발견함. 이 중 둘은 기계적 확장이 아니라 실제 구현 전 결정이
  필요했음:
  - `handoff_bridge.py`: `PROVIDERS`가 `("codex", "claude", "gemini")`로
    확장됨. `other_provider()`의 하드코딩된 이진 토글이 `PROVIDERS`를
    순서대로 순회하며 한 바퀴 도는 `next_provider(current, tried)`로
    교체됨 — 세 호출 지점(`init_handoff()`/`choose_auto_provider()`/
    `run_provider()`의 auto-fallback) 전부 이제 이걸 사용함; auto-fallback은
    여전히 정확히 한 번의 홉이고, 일반화된 건 오직 *어느* provider에
    착지하느냐뿐;
  - `provider_command()`에 `gemini` 분기 추가(다른 둘처럼 stdin으로
    프롬프트 전달, 이 워크스페이스에 이전에 깨끗하게 끝난 실행이 기록돼
    있으면 `--resume latest`), `summarize_gemini()` 추가 —
    `parse_jsonl()`을 거치지 않고 실행 종료 시점의 JSON 객체 하나를
    직접 파싱함;
  - Gemini의 `session_id`는 항상 리터럴 sentinel `"latest"`, 절대 실제
    ID가 아님 — Gemini의 JSON 응답엔 ID가 없음 — 실행이 `error` 필드
    없이 깨끗하게 끝났을 때만 설정됨, 이게 바로 `provider_command()`의
    다음 호출이 `--resume latest`를 안전하게 붙일 수 있는 시점임
    (DEC-17: Gemini 세션이 전역이 아니라 워크스페이스 디렉터리별로
    범위가 정해진다는 걸 확인한 뒤 선택 — "엉뚱한 대화를 재개할 수
    있다"는 위험을 상당히 좁혀서 이걸 그냥 당연한 게 아니라 실제 결정으로
    만듦);
  - `diagnose()`가 기존 `PROVIDERS` 루프를 통해 공짜로 `gemini` 행을
    얻고, 거기에 명시적인 "gemini auth: not checked" 문구 추가(DEC-18)
    — Gemini엔 무료 인증 상태 서브커맨드가 없고, 실제 확인은
    `diagnose`를 돌릴 때마다 토큰을 쓰게 되므로, `diagnose`가 때때로만
    무료가 되지 않도록 의도적으로 확인하지 않음;
  - `handoff_webui.py`: `API_KEY_MODE_PROVIDERS = ("codex", "claude")`가
    독자적인 튜플로 추가됨, 이제 3개짜리인 `PROVIDERS` 임포트에서
    의도적으로 파생시키지 않음 — Phase 4의 API 키 모드 범위(DEC-15)는
    Gemini를 포함하도록 재검토된 적이 없으므로, 공유 CLI 디스패치
    튜플이 커졌다고 해서 조용히 새 항목을 물려받으면 안 됨. `/api/run`이
    이제 `gemini`를 받아들임; `/api/providers`가 새 `api_key_mode_
    supported` 필드(Gemini에 대해선 계속 `false`)를 통해 Gemini의 실제
    CLI 감지 배지를 보여줌(SCR-06이 원래 "미확인" 플레이스홀더로 배포한
    걸 드디어 해소) — 그래서 연결 패널은 키 필드를 제공하지 않고
    상태만 보여줌;
  - `webui/index.html`/`app.js`: provider 선택기에 `gemini` 추가; 연결
    패널이 키/모델 입력을 렌더링하기 전에 `api_key_mode_supported`를
    확인함;
  - `docs/provider-extensibility.md`의 "현재 코드는 정확히 두 provider를
    가정한다" 절을 계획에서 실제로 무엇이 바뀌었는지의 기록으로
    재작성(`classify_handoff()` 자체는 예상대로 변경 불필요;
    `ERROR_PATTERNS`는 작은 추가 하나 필요, 아래 라운드 2에서 수정);
  - `tests/test_handoff_bridge.py`에 새/수정 테스트 17개
    (`next_provider()`의 순서/한바퀴돌기/시도한건 건너뛰기,
    `provider_command()`의 gemini 분기, `summarize_gemini()`의 성공/실패/
    형식 오류 입력, 가짜 `gemini` 바이너리를 쓰는 실제 서브프로세스
    통합 테스트) + `API_KEY_MODE_PROVIDERS` 분리에 대한
    `tests/test_handoff_webui.py` 갱신. 정확한 개수는 `python3 -m
    unittest discover -s tests -v`로 확인.
  - **라운드 2** (병합 전 독립적인 적대적 리뷰): N-way 리팩터가 놓친 실제
    버그 1건과 재개 sentinel의 실제 간극 1건 발견, 둘 다 수정:
    - `handoff_webui.py`의 바깥쪽 서브프로세스 타임아웃 핸들러가 전체가
      죽었을 때 어느 provider가 아직 돌고 있었는지 추측하는 자체
      하드코딩된 `"claude" if ... == "codex" else "codex"` 이진 추측을
      따로 갖고 있었음(`handoff_bridge.py`의 `other_provider()` 교체가
      webui 로컬의 같은 패턴 복사본은 건드리지 못했음) — 원래
      provider가 `"claude"`였을 때 항상 틀렸음(handoff가 필요한 claude
      실행은 `"codex"`가 아니라 `gemini`로 재귀함) — 시간 초과된
      provider를 영구 저장된 채팅 로그에 잘못 귀속시켰을 것임.
      추측을 재구현하는 대신 `next_provider()`를 직접 재사용하도록
      수정;
    - `summarize_gemini()`가 `"latest"` 재개 sentinel을 표시하기 전에
      JSON 응답 본문의 `error` 필드만 확인하고 `exit_code`는 절대
      확인하지 않았음 — Gemini 자체 문서가 실패 시 exit code/JSON 본문
      상관관계를 완전히 문서화하지 않으므로, 겉보기엔 깨끗한 본문을
      가진 0이 아닌 exit이 실패한 실행을 재개 가능하다고 표시했을 수
      있음. 이제 `exit_code == 0`까지 요구함.
  - **라운드 2** (이번엔 붙여넣은 게 아니라 PR 자체에 올라온 실제
    자동 리뷰): 실제 버그 2건과 실제 문서 모순 1건 발견, 전부 수정:
    - Gemini의 `AuthError`/exit-41 인증 실패가 `auth`가 아니라
      `unknown`으로 분류됐음 — `ERROR_PATTERNS`의 인증 정규식이
      Codex/Claude 자체 에러 어휘(`not logged in`/
      `authentication_failed`/`unauthorized`/`forbidden`)만 매칭했고,
      Gemini의 실제 확인된 에러 문자열은 그중 어느 것도 포함하지
      않았음. 패턴에 `AuthError`/`FatalAuthenticationError` 추가 —
      `docs/research-gemini-cli.md`에서 나온 정확하고 출처 있는 문자열,
      미검증 텍스트에 대한 추측이 아님;
    - 단일 홉 auto-fallback이 설치되어 작동하는 provider를 완전히
      건너뛸 수 있었음: `next_provider()`(그리고
      `choose_auto_provider()`의 handoff 필요 분기)가 실제 설치 여부는
      전혀 고려하지 않고 `PROVIDERS` 순서상 다음 provider를 골랐음 —
      provider가 정확히 둘일 땐 이게 전혀 문제가 안 됐지만(건너뛸 세
      번째 선택지 자체가 없었으므로), 셋이 되니 codex 실패가 미설치된
      claude에 떨어지면 gemini는 바로 거기 있고 작동하는데도 절대
      도달하지 못했음. `shutil.which()`를 인식하는 래퍼
      `next_available_provider()`를 추가해서 fallback 대상이 실제로
      *선택*되는 모든 곳에 사용함(`init_handoff()`의 순수 정보성
      메시지는 제외); `handoff_webui.py`의 타임아웃 추측(라운드 1에서
      수정)도 이제 이걸 호출해서 실제 서브프로세스가 하는 것과 같은
      걸 추측하게 함;
    - `docs/provider-extensibility.md`의 도입부가 "...(Phase 5에서
      해소됨)"이라는 제목의 절 바로 위에서 여전히 "여기 설명된 건 아직
      아무것도 구현되지 않았다"고 적혀 있었음 — 여전히 미래지향적인
      부분(가상의 네 번째 provider, API 키 모드 확장)과 이제는 역사가
      된 Gemini 기록을 같은 문서 안에서 둘 다 설명하도록 재작성함.
    - 이번 라운드에 새 테스트 7개 추가(+ `shutil.which`에 모킹된 기존
      `ChooseAutoProviderTests` 3개도 함께, `next_available_provider()`가
      이 테스트들의 결과를 스위트를 돌리는 기계에 실제로 뭐가 설치돼
      있는지에 의존하게 만들었으므로), 가짜 `codex`/`gemini` 바이너리와
      `PATH`에 `claude`가 전혀 없는 상태로 미설치 provider 건너뛰기
      시나리오를 정확히 재현하는 실제 서브프로세스 통합 테스트 포함.
  - **라운드 3** (같은 PR, 라운드 2의 수정이 반영됐는지 확인하는 후속
    자동 리뷰, 그 후 2가지 더 지적):
    - 이번 라운드 자체의 테스트가 로컬에서는 못 잡은 실제 CI 실패:
      `tests/test_handoff_webui.py`의 바깥쪽 타임아웃 추측 테스트가
      `shutil.which`를 모킹하지 않아서, 실제 `codex`/`claude`가 설치된
      개발 머신에서는 통과했지만 CI의 깨끗한 환경(아무것도 설치 안 됨)에서는
      실패했음 — `next_available_provider()`의 추측이 거기서 정확히
      `"codex"`로 폴백됐는데, 테스트의 "claude가 시간 초과됐다"는
      전제 자체가 애초에 claude가 실행 가능했어야 함을 암묵적으로
      요구하기 때문. 그 테스트에 `shutil.which`를 고정해서 전제를
      호스트의 실제 설치 상태에 의존하는 대신 구체적으로 만듦;
    - **동작 변경이 아니라 명명 정확도 문제**: "N-way fallback"이 실제로
      배포된 것보다 과장됐다는 지적 — auto-fallback은 설계상 처음부터
      끝까지 정확히 한 번의 홉이었음(원래 2-provider 시스템에서 바뀐 것
      없음, fallback도 늦게 실패할 경우 토큰 소모를 제한하기 위함).
      Phase 5가 실제로 일반화한 건 그 한 번의 홉이 *어느* provider에
      착지할 수 있느냐임. 위와 `docs/research-gemini-cli.md`의 구현 계획의
      헤드라인 표현을 "N-way fallback"이 아니라 "fallback 대상 선택"으로
      정정 — 한 번-홉 제약을 이미 설명하는 본문이 섹션 *제목* 자체가
      실제보다 더 많은 걸 암시하는 걸 막지 못하고 있었으므로;
    - 이 파일 자체의 Phase 5 항목이 `classify_handoff()`/
      `ERROR_PATTERNS`가 "변경 불필요"라고 적어놓고 바로 한 문단 아래
      라운드 2 항목에서는 `ERROR_PATTERNS`가 *실제로* 추가가 필요했다고
      문서화하고 있었음 — 각각에 실제로 맞는 내용을 적도록 정정
      (`classify_handoff()` 자체: 변경 없음; `ERROR_PATTERNS`: 추가 1건).

- **웹 UI Phase 6** (자동 업데이트 확인, SCR-07): CFL-11 해소. 이
  저장소는 비공개라서 익명 요청으로는 GitHub Releases를 조회할 수 없음
  — 새 공개 인프라를 세우는 대신 사용자 자신의 로컬 `gh` CLI 인증을
  재사용하는 것으로 해소(DEC-19) — `docs/release-process.md`가 릴리스를
  만들 때 이미 전제하고 있는 것과 같은 도구:
  - `handoff_bridge.py`: `GITHUB_REPO` 상수, `parse_version_tuple()`
    (`"v0.2.0"` → `(0, 2, 0)`, 파싱 안 되면 `None`), `check_for_update()` —
    기존 `short_run()` 헬퍼(이미 `gh`가 없거나 시간 초과됐을 때 예외
    대신 깔끔한 exit code로 바꿔줌)를 통해 `gh release view --repo
    <repo> --json tagName,url`을 실행, `BRIDGE_VERSION`과 비교해서
    진짜로 더 새 릴리스가 있을 때만 `{latest_version, current_version,
    url}`을 반환함 — 절대 예외를 던지지 않음, 이 프로젝트 다른 곳의
    `touch_registry()`와 같은 실패 시 조용히 넘어가는 태도, 아무도 명시적으로
    돌려달라고 한 적 없는 백그라운드 편의 확인이므로;
  - `handoff_webui.py`: `AppState.update_info`(평범한 속성, 락 없음 —
    한 번 쓰고 여러 번 읽는 패턴이고 하나의 백그라운드 스레드에서만
    쓰므로, 자격 증명/레지스트리처럼 경합하는 읽기-수정-쓰기가 아님).
    `main()`이 `AppState`를 만든 직후 `_check_for_update_in_background()`를
    데몬 스레드로 시작해서, 실제 `gh` 서브프로세스 호출(네트워크 I/O,
    몇 초 걸릴 수 있음)이 서버 시작이나 브라우저/네이티브 창 열기를
    절대 지연시키지 않음. `GET /api/update-check`는 캐시된 결과만
    읽으므로 네트워크 상황과 무관하게 항상 빠름;
  - `webui/index.html`/`app.css`/`app.js`: 항상 보이는 타이틀바
    "업데이트" 버튼(components.html §15의 "평소엔 아이콘만"과 일치),
    업데이트가 있을 때만 나타나는 작은 점 배지, 클릭하면 버전과
    "릴리즈 노트 보기" 링크가 있는 팝오버가 열림. 와이어프레임은
    "업데이트 있음" 상태만 목업했음 — 이미 최신이거나(또는 확인이
    실패했거나 아직 안 됐을 때) 버튼을 클릭하면 이 상태를 위해 별도
    팝오버 레이아웃을 새로 만드는 대신 기존 토스트 메커니즘("최신
    버전을 사용 중입니다")을 재사용함;
  - 새 테스트 17개: `parse_version_tuple()`(v 접두사, 파싱 불가 입력,
    길이가 다른 버전 비교), `check_for_update()`(더 새 릴리스 감지,
    같거나 더 오래된 버전은 업데이트로 보고 안 함, `gh`가 없거나
    에러나거나 형식이 잘못된 JSON을 반환하면 전부 `None`으로 귀결,
    호출이 `cwd`에 의존하지 않고 `--repo`를 고정함), 백그라운드 확인이
    `AppState.update_info`를 채우는 것, `GET /api/update-check`가 빈
    캐시 상태와 채워진 캐시 상태 둘 다 실제 HTTP 서버로 반영하는 것.
    정확한 개수는 `python3 -m unittest discover -s tests -v`로 확인.
  - **라운드 2** (실제 자동 리뷰, 진짜 정합성 버그 1건 발견):
    `state.update_info is None`이 "백그라운드 확인이 아직 안 끝남"과
    "끝났고 더 새 게 없음" 둘 다를 의미해서, 같은
    `{"update_available": false}` 응답으로 뭉개졌음. 실제 `gh`
    서브프로세스 호출은 네트워크 I/O라 페이지의 첫 `GET
    /api/update-check`가 도착할 때(특히 서버 시작 직후) 아직 돌고 있는
    경우가 흔했고, `webui/app.js`는 부팅 시 재시도 없이 딱 한 번만
    물어봤음 — 그래서 정상적인 서버 시작이 실제로 업데이트가 있는데도
    조용히 영구적으로 배지를 놓칠 수 있었음, 드문 엣지 케이스가 아니라.
    `AppState.update_checked`를 추가해서 "대기 중"과 "확인했고 없음"을
    구분하고, 프런트엔드가 이제 `checked`가 false인 동안 폴링함(1.5초
    간격, 최대 10번 — `short_run()`의 기본 10초 타임아웃을 넉넉히
    넘김) — 정확히 한 번만 묻는 대신. 새/수정 테스트가 대기 중 대
    확인됨 구분을 구체적으로 커버함 — 정확한 개수는 `python3 -m
    unittest discover -s tests -v`로 확인, 이 파일의 평소 오차 방지
    관행대로(여기 박힌 정확한 숫자가 실제 diff와 안 맞는다고 리뷰가
    지적해서 한 번 정정된 적 있음).
  - **라운드 3** (라운드 2의 수정을 확인하는 후속 리뷰, 그 후 낮은
    심각도의 비차단 지적 1건 더): 라운드 2의 수정이 만든 약 15초
    폴링 창 동안 업데이트 버튼을 클릭하면 아무것도 실제로 확인된 게
    없는데도 진짜로 최신임이 확인된 것과 같은 "최신 버전을 사용
    중입니다" 토스트를 보여줬음 — 실제로 업데이트가 있었고 아직 폴링에
    안 잡혔을 뿐이라면 작은 거짓 안심. 프런트엔드에 `updateCheckPending`
    플래그 추가(`true`로 시작, 응답이 실제로 `checked: true`를
    보고해야만 `false`로 바뀜)해서 그 창 동안엔 버튼이 대신 "업데이트
    확인 중입니다…"를 보여줌.
  - **라운드 4** (병합 전 외부 리뷰를 더 기다리는 대신 명시적으로 요청한
    독립적인 self-review): 라운드 2 수정 자체의 주석을 믿는 대신
    실제 바이트코드 수준의 읽기/쓰기 순서를 추적, 라운드 2가 고친
    바로 그 경합의 실제(다만 좁은) 읽는 쪽 대응 사례를 발견:
    - `GET /api/update-check`의 핸들러가 `update_checked` *전에*
      `update_info`를 읽었음 — 백그라운드 스레드가 그 둘을 쓰는 순서와
      반대(`update_info`를 쓰고 그다음 `update_checked`를 씀, 그래서
      `update_checked`를 나중에 확인하는 리더는 그게 True일 때 항상
      `update_info`가 진짜로 채워진 뒤라는 걸 보장받음). 두 쓰기가
      핸들러 자체의 두 읽기 사이 틈에 일어나면, 오래된(쓰기 전) `info`와
      신선한 `checked = True`를 함께 관찰할 수 있었음 — 진짜 업데이트
      발견과 경합한 요청에 대해 "확인함, 업데이트 없음"이라고 보고하는
      것 — 라운드 2의 버그를 반대편에서 조용히 재현한 것. `checked`를
      먼저 읽는 걸로 수정: 유일하게 가능한 오래된 읽기는
      `checked = False`가 되고, 이건 그냥 폴링 클라이언트가 다시
      묻게 만드는 것(틀려도 안전한 방향) — `checked`가 이미 `True`로
      관찰된 게 아니면 `update_info`의 값 자체를 아예 참조하지 않음;
    - `webui/app.js`의 폴링 `catch` 블록이 재시도가 다 소진된 뒤가
      아니라 단 한 번의 fetch 예외에도 영구적으로 포기했음 — 일시적인
      한 번의 문제(예: 서버가 시작 직후 아직 연결을 안 받는 순간)만
      있어도 라운드 2 수정의 전제(재시도) 자체를 무너뜨림. 이제
      확인이 안 끝난 것과 같은 방식으로 제한적으로 재시도됨;
    - off-by-one 오류로 "`UPDATE_CHECK_MAX_POLLS = 10`" 상한을 통과해서
      11번 fetch가 나갈 수 있었음(0부터 시작하는 `attempt`에
      `attempt < 10`은 0부터 10까지 시도를 허용함); `attempt + 1 < 10`으로
      수정;
    - 실제 대기 → 확인됨 *전이*(정적인 두 종단 상태를 `state.*` 할당으로
      직접 검증하는 게 아니라, `threading.Event`로 게이팅되는 진짜
      백그라운드 스레드)를 실행하는 실서버 테스트 추가 — 이걸 위한
      테스트 클래스는 이미 `ThreadingHTTPServer` 장치를 갖고 있어서
      저렴하게 메울 수 있었는데도 이 간극이 존재했음;
    - 위 라운드 2 항목 자체의 부풀려진 테스트 개수를 정정.
  - **라운드 5** (CFL-18 수정, DEC-20 — 라운드 3/4이 명시적으로 "심각도
    낮음, 머지 차단 아님"으로 남겨뒀던 바로 그 지적): `check_for_
    update()`가 진짜로 다른 두 상황 — "확인 성공, 더 새 것 없음"과
    "아예 확인 못 함"(`gh` 없음/미인증/오프라인/응답 파싱 불가) — 에
    대해 `None`을 반환해서 호출자가 둘을 구분할 수 없었음, 그래서 `gh`가
    세팅 안 된 사용자가 실제로 최신임이 확인된 사용자와 같은 "최신
    버전을 사용 중입니다" 토스트를 봤음. `check_for_update()`가 이제
    항상(절대 `None` 아님) `status` 필드가 있는 dict를 반환함:
    `"available"`(모양은 그대로에 필드만 추가), `"current"`, 또는
    `"unavailable"`(읽을 수 없는 응답 케이스 전부를 하나로 뭉침 —
    이 프로젝트의 백그라운드 편의 확인에 대한 기존 실패 시 조용히
    넘어가는 태도와 일치, 다만 이제 "current"와 구분 가능해짐, 이전엔
    구분 불가능했음). `GET /api/update-check`가 기존 `update_available`
    불리언을 없애고 `status`를 직접 노출함. `webui/app.js`는 예전의
    대기 전용 불리언 대신 `latestUpdateStatus`(`"pending"|"available"|
    "current"|"unavailable"`)를 추적하고, 업데이트 버튼이 이제
    unavailable 케이스에 대해 "current" 문구를 조용히 빌려쓰는 대신
    네 번째로 구분되는 토스트 — "업데이트를 확인할 수 없습니다." —
    를 보여줌. 새 계약에 맞춰 세 `CheckForUpdate*` 클래스 전부의
    테스트가 갱신됐고, 실서버 전체에 걸쳐 `unavailable`을
    end-to-end로 커버하는 새 케이스도 추가됨. 정확한 개수는
    `python3 -m unittest discover -s tests -v`로 확인.

- **CFL-17 후속** (API 키 모드의 전체 에이전트 기능 패리티, DEC-21로
  해소): API 키 모드는 채팅 전용으로 시작했음(DEC-13, Phase 4); 이번에
  Phase 4가 명시적으로 미뤘던 CLI 모드와의 파일 편집/셸 실행 패리티를
  추가함. 설계 인터뷰로 미결 갈림길 2건을 확정: 파일 도구와 셸 도구를
  한 번에 함께 구현(더 크고 위험한 선택지 — 더 보수적인 파일 도구만
  먼저라는 권장안이 아님), 이번에 추가하는 모든 도구 호출에 더 강한
  호출당 확인을 요구하는 대신 DEC-02(세션당 첫 전송만 확인)를 재사용.
  - `handoff_webui.py`: 도구 4개(`read_file`, `write_file`, `edit_file`,
    `run_shell`)를 `_TOOL_SPECS`에 한 번만 선언하고 각 벤더 자체 스키마
    모양으로 렌더링함(`anthropic_tool_definitions()`/
    `openai_tool_definitions()`) — 둘이 조용히 어긋나지 않도록.
    `execute_tool_call()`이 맞는 실행기로 디스패치하고 절대 예외를
    던지지 않음 — 모르는 도구 이름이나 실행기 내부의 버그는 대화
    중간에 크래시하는 대신 모델이 볼 수 있는 에러 문자열로 저하됨.
    파일 도구는 워크스페이스 격리를 위해 기존
    `safe_join()`/`read_file_preview()` 기본 요소와 기존 크기 상한을
    재사용함; `run_shell`은 타임아웃과 함께
    `subprocess.run(..., shell=True, cwd=workspace)`를 실행함
    (`TOOL_EXEC_TIMEOUT_SECONDS`, `API_KEY_MODE_TIMEOUT_SECONDS`의 값
    재사용)과 출력 길이 상한(`TOOL_OUTPUT_MAX_CHARS`, 명시적인 안내와
    함께 잘림, 절대 조용히 안 함) — 인터뷰 자체의 선택으로 명령
    allowlist는 없음, 워크스페이스를 시작 `cwd`로 하는 브릿지가 제어하는
    셸 도구가(샌드박스가 아님 — 절대경로나 `..`는 여전히 OS 사용자
    계정이 닿을 수 있는 곳 어디든 도달함, 실제 터미널이나 CLI 모드
    자체의 `codex`/`claude` 서브프로세스와 정확히 마찬가지로) CLI
    모드가 실제로 실행될 때 이미 갖고 있는 신뢰 수준을 넘어서는 새
    등급이 아니라는 논리로.
  - `call_anthropic_messages_api()`/`call_openai_responses_api()`가 새
    형제 함수를 도입하는 대신 그 자리에서 실제 턴 루프를 얻음 — 도구
    호출 블록이 없는 응답은 여전히 첫 HTTP 호출에서 그대로 반환됨, 이번
    변경 전 이 두 함수가 갖고 있던 정확히 같은 동작이라 순수 채팅 턴은
    영향받지 않음. Anthropic의 루프는
    `tool_choice.disable_parallel_tool_use: true`를 설정함(턴당 도구
    호출 하나, 로그와 추론이 더 단순해짐); OpenAI의 Responses API는
    문서화된 동등 기능이 없어서, 하나 이상의 `function_call` 항목을
    담은 응답은 전부 실행되고 전부에 대한 결과가 반환됨. 둘 다 한 턴을
    `MAX_TOOL_ITERATIONS = 15`번의 도구 호출로 제한, 도달하면 그때까지
    있는 텍스트에 안내를 덧붙여 반환함 — 혼란에 빠진 모델이 API
    비용을 무한정 태우며 루프 돌 수 없도록. 도구 호출 활동(도구 이름,
    인자, 결과)은 `final_text`에 펜스 코드 블록으로 접혀 들어감 —
    DEC-03의 기존 코드 블록 렌더링, 새 메시지 스키마나 프런트엔드
    변경이 아님 — 그래서 DEC-02의 단일 확인 게이트가 호출마다 물어보는
    걸 가로막지 않는데도 무엇이 실행됐는지 영구 저장된 채팅 로그에서
    보임.
  - 두 벤더의 도구 사용 JSON 모양(Anthropic의 `tool_use`/`tool_result`
    콘텐츠 블록, OpenAI의 `function_call`/`function_call_output` 항목)을
    구현 전에 각 벤더의 최신 공식 문서와 대조해서 확인함, 오래되거나
    일반적인 지식으로 가정하지 않음 — 각 함수의 docstring에 근거 기록.
  - 새 테스트: 두 벤더 모양 사이의 도구 스키마 일관성, 각 도구
    실행기를 직접(`safe_join()`을 통한 경로 탈출 거부, `edit_file`의
    정확히 한 번 일치 요구사항, `run_shell`의 타임아웃과 출력 잘림
    처리, 실행기 예외가 전파되지 않고 잡히는 것), `execute_tool_call()`을
    모킹한 턴 루프 자체(도구 호출 왕복, `MAX_TOOL_ITERATIONS` 상한,
    `tool_use_id`/`call_id`가 정확히 되짚어 전달되는 것, OpenAI의 한
    출력에 여러 function call이 있는 케이스, 형식이 잘못된 `arguments`
    JSON이 루프를 크래시시키지 않는 것). 도구 루프 이전의 단일 호출
    동작을 다루던 기존 테스트는 `workspace` 인자 하나만 추가하면 됐음 —
    도구 호출 블록이 없는 응답이 정확히 그 테스트들의 픽스처 모양이라,
    루프의 0회 반복 케이스가 예전 동작을 그대로 재현함. 정확한 개수는
    `python3 -m unittest discover -s tests -v`로 확인.
  - **라운드 2** (PR을 열기 전 독립적인 self-review, 그 다음 GitHub의
    실제 자동 리뷰 — 둘 다 진짜로 유용했음, 둘을 합쳐 실제 발견 3건,
    이번 라운드엔 오래되거나 거짓인 주장 없음): self-review가
    `MAX_TOOL_ITERATIONS`가 실제 도구 실행이 아니라 HTTP 왕복을 세고
    있다는 걸 발견함 — 어느 벤더든 응답 하나가 정당하게 도구 호출을
    여러 개 담을 수 있으므로, 모델이 많은 걸 응답 하나에 몰아넣으면
    의도한 상한을 훌쩍 넘겨 실행할 수 있었음; 이제 두 루프 다 실행된
    횟수를 실제로 추적함. 같은 리뷰가 Anthropic 루프가 항상
    `tool_use_blocks[0]`만 실행하고, API가
    `disable_parallel_tool_use`(힌트일 뿐 보장이 아님)를 지키지 않을
    경우 나머지를 조용히 버린다는 것도 발견함 — 이제 모든 블록을
    실행함, OpenAI 루프의 기존 다중 호출 응답에 대한 방어적 처리와
    일치하도록. 그 다음 GitHub 리뷰가 2건 더 발견, 둘 다 수정: 턴
    중간의 API 실패(네트워크 에러, 200 아님)가 *이미* 실행된 도구의
    기록을 지워버렸음 — `write_file`/`edit_file`/`run_shell`이 다음
    호출이 실패하기 *전에* 이미 실제 효과를 냈다면, 그 기록이 에러와
    함께 사라져서 사후에도 활동이 계속 보인다는 DEC-21 자체의 전제
    (그래서 호출별 확인을 생략해도 안전하다는 것)를 무너뜨렸음
    (`_error_with_transcript()`가 이제 누적된 기록이 있으면 실패 메시지
    앞에 붙임); 그리고 `read_file`이 `run_shell`의 출력이 이미 그러던
    것과 달리 `MAX_FILE_BYTES`(약 256KB, 디스크에서 읽는 양)로만
    제한되고 `TOOL_OUTPUT_MAX_CHARS`(4000자, *다음* API 호출에 들어가는
    양)로는 제한되지 않은 `read_file_preview()`의 내용을 그대로
    반환했음 — 한 턴에서 큰 파일을 몇 번만 읽어도 그 상수가 제한하려던
    맥락/비용 예산을 여전히 훌쩍 넘길 수 있었음. 리뷰 자체가 선택
    사항, 머지 차단 아님으로 표시했던 제안 하나(도구 출력/인자가
    자체적으로 ` ``` `를 포함하면 펜스 코드 블록 감사 기록이 깨질 수
    있는 문제도 다루라는 것)도 함께 처리함 — 다른 두 수정이 지키는 것과
    같은 사후 가시성 보장을 직접 뒷받침하므로(`_escape_fence()`, 도구
    이름/인자와 결과 둘 다에 기록에 접혀 들어가기 전에 적용됨). 새
    회귀 테스트가 이 전부를 직접 커버함.
  - **라운드 3** (라운드 2 수정 커밋에 대한 새 자동 리뷰, 이번엔 "머지
    차단 항목 없음" — 진짜지만 심각도가 낮은 발견 2건 더, 둘 다
    처리함): 라운드 2가 결과 쪽은 제한했는데도 기록의 *인자* 쪽엔
    길이 제한이 없었음 — `write_file`의 `content`/`edit_file`의
    `new_string`이 `json.dumps(tool_input)`을 통해 여전히 임의로 길게
    기록에 그대로 들어가서, 완전히 정상적인 대용량 파일 쓰기 하나가
    이후 모든 호출의 맥락을 부풀릴 수 있었음(파일 자체는 어느 쪽이든
    디스크에 전체가 남음 — 기록은 쓰기가 일어났다는 것만 보여주면
    됨). `_truncate_for_transcript()`가 이제 인자에도 같은
    `TOOL_OUTPUT_MAX_CHARS` 제한을 적용함. 별도로, `run_shell`을
    "cwd-confined"나 "고정"이라고 설명하는 이 프로젝트 자체의 주석/문서가
    `cwd=workspace`가 실제로 제공하는 것보다 더 강한 격리를 주장하는
    것처럼 읽힐 수 있었음 — 이건 *시작* 디렉터리만 설정할 뿐 샌드박스가
    아님; 절대경로나 `..`는 여전히 OS 사용자 계정이 닿을 수 있는 곳
    어디든 도달함, 실제 터미널이나 CLI 모드 자체의 `codex`/`claude`
    서브프로세스와 마찬가지로. 이 프로젝트가 `run_shell`의 격리를
    설명하는 모든 곳(`handoff_webui.py`, 이 파일, `webui-chat-storage.md`,
    `flutter-mapping.html`의 DEC-21)에서 DEC-21이 실제로 결정한 것보다
    더 많은 걸 암시하지 않고 명시적으로 그렇게 말하도록 다시 표현함.
  - **라운드 4** (라운드 3 수정 커밋에 대한 새 리뷰 — 다시 "머지 차단
    항목 없음", 심각도 낮은 선택 항목 2건 더): `subprocess.run(...,
    timeout=...)`의 `TimeoutExpired` 처리는 즉시 자식 프로세스를 죽이는
    것만 보장하고, 백그라운드로 돌거나 fork된 명령이 만들 수도 있는
    프로세스 트리 전체는 아님 — 진짜 문제지만, 크로스플랫폼
    프로세스 그룹 정리(POSIX의 `os.killpg`, Windows의 job object)는
    이번 라운드 범위보다 의미 있게 많은 코드라, 구현하는 대신 알려진
    채로 수용한 간극으로 문서화함(`handoff_webui.py`,
    `webui-chat-storage.md`) — DEC-21이 `run_shell`에 명령 allowlist가
    없다는 것에 대해 이미 취하고 있는 것과 같은 태도. PR 설명 자체가
    라운드 3이 모든 영구 문서/주석을 고친 뒤에도 여전히 "cwd-confined"라고
    적혀 있었음 — 거기도 정정함.

- **Phase 7a** (프레임워크 전환 착수, DEC-22 — CFL-14 해소): 이 저장소
  최초의 진짜 비-Python 코드. 설계 인터뷰로 아키텍처 갈림길 4건 확정
  (Electron 대신 Tauri, Rust 재작성 대신 Python 백엔드를 PyInstaller
  sidecar로 유지, 각 프레임워크 자체 업데이터 대신 기존 `gh` 기반
  업데이트 확인 유지, 이번 phase는 `webui/`를 거의 그대로 이식) — DEC-22
  참고. 가장 작은 sub-phase(7a) 추가: sidecar 아키텍처가 한 OS에서
  실제로 end-to-end로 작동하는지 증명, 아직 패키징/서명/크로스플랫폼
  빌드는 없음(7b/7c).
  - `src-tauri/`: Tauri v2 프로젝트(`cargo tauri init`, 바닐라 JS
    템플릿). `tauri.conf.json`의 `app.windows`는 의도적으로 비워둠 —
    정적으로 선언된 창은 생성되는 즉시 URL로 내비게이션하는데, 이게
    PyInstaller onefile 바이너리의 실제 시작 비용(자가 압축 해제 + 전체
    Python import)과 경합함 — 실제로 빌드한 `.app`을 띄워보고 영구히
    빈 창이 뜨는 걸 발견함. `src-tauri/src/lib.rs`가 대신
    `agent-handoff-bridge-server` sidecar를 spawn하고, 그 stdout에
    `handoff_webui.py`의 `main()`이 `ThreadingHTTPServer(...)`가 바인딩된
    직후 이미 찍는 준비 완료 줄이 담긴 뒤에야 창을 만듦.
  - 그 첫 번째 문제 위에 버퍼링 버그가 2개 더 쌓임, 둘 다 유닛 테스트가
    아니라 실제로 빌드한 `.app`을 테스트해봐야만 발견됨: 파이프로
    연결된(tty가 아닌) stdout은 CPython을 완전 버퍼링으로 전환시켜서,
    위의 준비 완료 출력이 Rust의 `CommandEvent::Stdout`에 도달하지
    못하고 Python 자체 버퍼에 무한정 머물 수 있었음 — 위의 수정이
    있어도 창 생성이 영원히 멈춤. 먼저 sidecar spawn에
    `PYTHONUNBUFFERED=1`을 시도했으나, 실제 PyInstaller onefile
    바이너리를 대상으로 테스트해보니 이것만으로는 확실히 안 고쳐짐
    (부트로더 자체의 환경변수/재실행 처리가 그 변수가 내장된
    인터프리터에 도달하는 걸 보장하지 않음). 진짜 수정:
    `handoff_webui.py`의 `main()`이 이제 직접
    `sys.stdout.reconfigure(line_buffering=True)`를 호출함, 원시
    바이너리의 stdout을 파일로 리다이렉트해서 준비 완료 줄이 즉시
    나타나는 걸 확인해 검증. `PYTHONUNBUFFERED=1`은 무해한 추가로 spawn에
    그대로 남겨둠.
  - 실제 호출 체인을 따르는 PyInstaller `--onefile` sidecar 4개:
    `agent-handoff-bridge-server`(`handoff_webui.py`),
    `agent-handoff-bridge-cli`(`handoff_bridge.py`, `init`/`run`을 위해
    서버가 호출), `agent-handoff-bridge-validate`(`scripts/
    validate_handoff.py`, CLI의 `check`가 호출),
    `agent-handoff-bridge-scan`(`scripts/scan_secrets.py`, validate의
    비밀 스캔 단계가 호출) — 넷 다 `tauri.conf.json`의
    `bundle.externalBin`에 선언됨(마지막 셋 중 하나라도 빠지면 임시
    로컬 테스트에선 동작하지만 실제 패키징된 `.app`에는 조용히
    번들되지 않음). `scripts/build_sidecars.py`가 넷 모두를 위한 실제
    실행 가능한 빌드 스크립트임(네 번의 PyInstaller 호출이 처음엔
    대화형 셸 히스토리로만 존재했던 걸 리뷰 라운드에서 추가) — CLI
    sidecar의 `--add-data` 플래그를 위해 두 번째 어긋날 수 있는 목록
    사본을 유지하는 대신 `handoff_bridge.INSTALL_FILES`를 직접
    임포트함.
  - 이 프로젝트에 이미 있던 패턴(`[sys.executable, script_path, ...]`로
    형제 스크립트를 서브프로세스 호출) 중 얼려지면(freezing) 깨지는
    실제 사례 4건 수정 — 얼려지면 `sys.executable`은 얼려진 바이너리
    자체이지 Python 인터프리터가 아님. `handoff_webui.py`가
    `bridge_command_prefix()`를 얻음; `handoff_bridge.py`의 `check()`와
    `scripts/validate_handoff.py`의 `check_secrets()`가 같은
    `getattr(sys, "frozen", False)` 분기를 얻어서 형제 sidecar 바이너리를
    대신 직접 호출함. `check_tests()`(이 프로젝트 자체의 개발용 유닛
    테스트 스위트를 재실행하는 것)는 같은 방식으로 고칠 수 없었음 —
    그 스위트 자체의 통합 테스트가 새 `sys.executable` 서브프로세스를
    spawn함, 정확히 우회하려는 그 가정이라, 이미 얼려진 인터프리터
    안에서 재실행하면 같은 문제에 재귀적으로 부딪힘. 대신 얼려졌을 때는
    깔끔하게 건너뜀, 어차피 배포된 앱은 대상으로 테스트할 개발용
    체크아웃이 없으므로.
  - `handoff_bridge.py`의 `install`/`init`이 새 워크스페이스에 복사하는
    약 50개 파일(`INSTALL_FILES`)을 `--add-data`로 CLI sidecar에 번들해야
    했음 — PyInstaller onefile은 기본적으로 Python이 아닌 데이터 파일을
    포함하지 않으므로, 안 그러면 얼려진 `init`이 조용히 불완전한
    워크스페이스를 만들었을 것임. 동적으로 `unittest.discover()`되는
    테스트 모듈의 stdlib 임포트(`unittest.mock`, `http.server` 등)도
    같은 이유로 명시적인 `--hidden-import` 플래그가 필요했음(진입점
    스크립트만 보는 PyInstaller의 정적 분석엔 안 보이므로).
  - 새 테스트: `BridgeCommandPrefixTests`(`handoff_webui.py`),
    `CheckCommandTests`(`handoff_bridge.py`), 새
    `tests/test_validate_handoff.py`(이 스크립트엔 이전에 유닛 테스트가
    없었음) — 전부 얼려진/얼려지지 않은 분기와 Windows `.exe` 접미사까지
    커버. 정확한 개수는 `python3 -m unittest discover -s tests -v`로
    확인.
  - 유닛 테스트뿐 아니라 실제로 빌드한 `.app`을 대상으로 검증: sidecar가
    시작되고, 실제 HTTP API를 통한 첫 채팅 메시지가 CLI sidecar를 거쳐
    진짜 워크스페이스(`.handoff/current.md`/`state.json`)를 만들고,
    `agent-handoff-bridge-cli check`가 깨끗하게 통과함. macOS는 이 앱을
    올바른 번들 ID와 살아있는 WebKit 렌더러 프로세스로
    `type="Foreground"`로 등록함. 이 개발 환경에서는 렌더링된 창을
    직접 시각적으로(스크린샷) 확인하는 게 불가능했음(접근성 권한
    제약으로 화면 자동화가 계속 엉뚱한 창을 대상으로 함 — 한 번은
    키 입력이 관계없는 앱으로 잘못 들어가서, 그 뒤로는 다시 위험을
    감수하는 대신 추가 스크린샷 시도를 중단함). 대신
    `tauri-plugin-log`의 영구 로그 파일(항상 켜짐, 디버그 빌드에서만이
    아님 — 아래 리뷰 라운드 참고)이 창이 생성된 직후 `curl`이 아니라
    *웹뷰 자체*가 `GET /`, `GET /app.css`, `GET /app.js`, `GET
    /api/update-check`, `GET /api/info`를 순서대로 요청하는 걸 보여줌 —
    정확히 실제 브라우저 엔진이 HTML을 파싱하고 실제 프런트엔드를
    실행하는 요청 패턴, 순수 HTTP 클라이언트로는 만들 수 없는 것.
    스크린샷 없이도 거의 결정적이지만, 직접 시각적으로 확인하는 걸
    여전히 권장함.
  - **리뷰 라운드** (PR을 열기 전 독립적인 self-review, 발견 5건, 전부
    처리): 준비 완료 마커를 찍기도 전에 죽거나 에러가 난 sidecar(잘못된
    빌드, 포트 충돌, import 에러)는 창도 없고 다이얼로그도 없이 앱이
    계속 실행되는 상태로 남겼음 — 로깅이 디버그 빌드에만 걸려 있어서
    릴리스 빌드에선 진단 흔적이 아예 없었음. 수정: 로깅이 이제 항상
    켜짐(빌드 타입과 무관하게 `tauri-plugin-log`의 평소 플랫폼별 로그
    파일에 씀), `tauri-plugin-dialog`는 오직 치명적 시작 에러 경로만을
    위해 추가됨 — sidecar가 창이 생기기 전에 종료되거나 에러나면
    영원히 보이지 않게 있는 대신 블로킹 네이티브 다이얼로그와 깔끔한
    종료. 또한 발견: 새 `tests/test_validate_handoff.py`가
    `scripts/validate_handoff.py` 자체의 `REQUIRED_FILES`/`PYTHON_FILES`
    (다른 모든 `tests/test_*.py`는 등록돼 있었음)나
    `handoff_bridge.py`의 `INSTALL_FILES`(그래서 평범한 얼려지지 않은
    `install`/`check`가 자신의 새 테스트 파일을 조용히 설치도 추적도
    안 했을 것임)에 등록되지 않았음 — 셋 다 등록함. `docs/security-model.md`에
    새 Tauri/sidecar 아키텍처에 대한 절이 전혀 없었음; 하나 추가함,
    `capabilities/default.json`의 `shell:allow-execute` 권한과
    `tauri.conf.json`의 `"csp": null`이 둘 다 현재 아무 일도 안 한다는
    발견 포함(창은 항상 sidecar의 진짜 외부 `http://127.0.0.1:8787/`
    URL만 로드하고, 이 프로젝트의 Rust 코드는 권한이 가로챌 IPC를
    거치지 않고 셸 플러그인을 직접 호출함) — 앞으로 프런트엔드에서
    도달 가능한 실제 네이티브 명령 표면을 추가하는 미래 sub-phase가
    있을 때 둘 중 어느 것도 이미 실질적인 일을 하고 있다고 오인하거나
    그 가정 아래 더 느슨하게 풀지 않도록 문서화함. 그리고: 네 번의
    PyInstaller 빌드 호출을 캡처한 커밋된 스크립트가 없었음, 위의
    `scripts/build_sidecars.py`로 처리함.

- **Phase 7b** (크로스플랫폼 빌드, 실제 설치형 산출물, sidecar 생명주기
  수정 — PR #13-#16): 7a의 개념 증명을 "개발 머신 한 대에서 돌아간다"에서
  "macOS/Windows/Linux에서 실제로 빌드되고 배포된다"로 끌어올림.
  - **크로스플랫폼 sidecar 빌드**: `scripts/build_sidecars.py`(7a
    전용이던 `build_phase7a_sidecars.py`에서 이름을 바꾸고 일반화)가
    이제 macOS/Windows/Linux 어디서든 빌드됨 — `--add-data` 구분자가
    하드코딩된 `:` 대신 `os.pathsep`을 사용함(Windows의 `;` 대
    나머지의 `:`, PyInstaller 자체 규칙과 정확히 일치), `rename_for_
    tauri()`가 Tauri의 `<name>-<target-triple>[.exe]` sidecar 파일명
    생성을 자동화함(이전엔 바이너리당 한 번씩 손으로 했음). 새 CI
    `sidecar-build` job: `macos-latest`/`windows-latest`/`ubuntu-latest`
    매트릭스가 OS별로 이 스크립트를 실제로 돌리고 각 빌드된 sidecar를
    스모크 테스트함(`--version`/`--help`) — 단순히 파일명 존재만
    확인하는 게 아니라. 이 프로젝트 최초의 Windows에서의 실제 CI 실행.
  - **OS별 실제 설치형 산출물**: 새 `installer-build` CI job이 `cargo
    tauri build`로 진짜 미서명 설치형 산출물(macOS는
    `.dmg`/`.app`, Windows는 `.msi`+nsis `.exe`, Linux는
    `.deb`/`.AppImage`/`.rpm`)을 만듦, 기존 컴파일 체크 job이 쓰는
    더미 파일 대신 위의 진짜 sidecar를 사용함. 나머지 CI처럼 모든
    PR/push마다가 아니라 의도적으로 수동 트리거(`workflow_dispatch`)로만
    제한함 — GitHub이 비공개 저장소 Actions 분을 macOS 러너는 10배,
    Windows는 2배로 과금하고, 실제 번들 빌드는 상대적으로 비싸므로.
    여전히 미서명(코드 서명은 별도의 "Phase 7c" 결정 게이트로 남음,
    DEC-22/DEC-23 — 새 비용, Apple Developer Program 연 $99+ — 지금은
    시작하지 않기로 명시적으로 결정).
  - **`docs/release-process.md` 재작성**으로 하나가 아니라 두 병행
    패키징 트랙으로: 원래의 git 불필요 소스 zip
    (`scripts/package_platforms.py`, 변경 없음, 터미널/CLI 전용 사용)이
    새 설치형 산출물(데스크톱 GUI 사용)에 밀려나는 게 아니라 그 옆에
    나란히 유지됨 — 추측이 아니라 저장소 소유자와 명시적으로 확인,
    **DEC-23**으로 기록(오래 열려 있던 **CFL-09** 해소 — 원래는
    프레임워크 전환이 일어나면 zip 모델이 완전히 끝날 거라고 가정했었음).
  - **실제로 빌드하고 종료시켜서 검증한 sidecar 생명주기, 코드만 읽어서가
    아님**: 7a가 미뤘던 두 질문이 알고 보니 검증뿐 아니라 실제 수정이
    필요했음. 앱 종료 시 sidecar 정리는 진짜로 깨져 있었음 — 실제로
    남아있던 고아 프로세스(`launchd`가 부모, 앱이 종료된 지 몇 시간
    지나서도 포트 8787을 쥐고 있었음)로 발견 — spawn된 sidecar의
    `CommandChild`가 정리 훅 없이 버려졌던 게 원인. 수정: 이제 자식이
    Tauri managed state에 보관되고 `RunEvent::Exit`에서 죽여짐
    (`ExitRequested`가 아님 — 여기선 실제 종료 시 절대 발동하지 않음 —
    실제 종료가 만드는 모든 이벤트를 로깅해서 확인함). *깔끔한* kill을
    얻는 데 두 라운드가 더 걸림: 단일 PID `kill()`은 바깥쪽
    PyInstaller 부트로더에만 도달해서 그 재실행된 내부 프로세스를 또
    다시 고아로 만들었고, 단일 홉 `pkill -P`는 첫 프로세스 세대에만
    도달함 — 실행 중인 provider 실행의 실제 트리는 3~4세대 깊이임(실행
    도중 spawn되는 두 번째 sidecar, 그것의 재실행된 인터프리터, 그리고
    실제 `codex`/`claude`/`gemini` 서브프로세스). 아무것도 죽이기 전에
    전체 자손 트리를 순회하는 걸로 수정함. 그다음: 그 전체 트리를
    무조건 강제 종료하는 것 자체가 provider가 파일을 쓰는 도중이라면
    실제 위험이었음 — 이제 먼저 `SIGTERM`/강제 아닌 `taskkill`을 보내고,
    잠깐 기다린 뒤, 여전히 살아있는 것만 강제 종료함. 포트 8787 충돌
    처리는 이미 멈추진 않았지만(기존 에러 다이얼로그가 이미 잡고
    있었음) 제너릭한 메시지를 보여줬음; `handoff_webui.py`가 이제
    바인딩 실패를 다시 던지기 전에 안정적인 마커(`AHB_PORT_CONFLICT`)를
    찍고, Rust 쪽은 OS/로케일마다 다른 `OSError` 텍스트를 추측하는 대신
    그 마커로 판단함.
  - **알려졌고 의도적으로 수용한 채로 남긴 간극, 추측으로 넘기지
    않음**: 위의 더 깊은 트리 kill과 graceful 종료 로직은 컴파일
    체크로만 검증됐고, 반복적인 실제 빌드-실행-종료 사이클로는 검증되지
    않음(테스트 도중 로컬 시스템 리소스 부하 우려가 나온 뒤의 의도적인
    트레이드오프). 같은 graceful-then-force kill의 Windows 버전에는
    여전히 알려졌고 안 고친 엣지 케이스가 있음(이미 죽은 root PID를
    다시 대상으로 하면 살아남은 자손을 놓칠 수 있음) — 이 코드베이스의
    Windows 전용 코드는 이 개발 환경에서 검증 경로가 전혀 없어서(CI
    컴파일 체크조차 없음 — `rust-build`는 Linux에서만 돎) 그대로 둠.
    이 기능 전체에 대한 실제 Windows/Linux 테스트는 여전히 남은 과제임.

## v0.1.0 — 2026-08-03

첫 태그된 릴리스. GitHub Releases에서
`agent-handoff-bridge-macos.zip`/`agent-handoff-bridge-windows.zip`으로
다운로드하거나 `git clone`으로 사용 가능. `python3 handoff_bridge.py
--version`과 `python3 handoff_bridge.py check`로 다운로드를 검증함 — 둘
다 provider 토큰을 쓰지 않고 git 저장소도 필요 없이 실행됨.

- **릴리스 패스**:
  - `handoff_bridge.py`에 `BRIDGE_VERSION`/`--version` 추가;
  - `scripts/package_platforms.py`가 이제 품질 게이트 스크립트와
    `tests/`를 릴리스 zip에 함께 담고 `START_HERE_*.txt`에 버전을 새김;
  - 릴리스를 만드는 방법을 문서화한 `docs/release-process.md` 추가.

- **품질 게이트 패스**:
  - 강제되는 모든 규칙을 정리한 `docs/quality-gates.md` 추가;
  - 브랜치 명명 규칙(`type/short-description`)과
    `scripts/check_branch_name.py` 추가;
  - `scripts/scan_secrets.py` 추가하고 `handoff_bridge.py check`와
    `.githooks/pre-commit`에 연결;
  - `handoff_bridge.py`의 실패 분류가 `tool_failure`를 인식하도록
    수정하고 `docs/shared-agent-contract.md`와 동기화;
  - `.handoff/state.json`과 `.handoff/current.md`에 원자적이고
    프로세스 간 락이 걸린 쓰기(`atomic_write_text`, `WriteLock`) 추가;
  - 분류, provider fallback, 공유 쓰기 로직을 커버하는
    `tests/test_handoff_bridge.py`(stdlib `unittest`) 추가,
    `handoff_bridge.py check`가 실행;
  - `.githooks/pre-commit`, `.githooks/pre-push`,
    `scripts/install_git_hooks.sh`, `.github/workflows/ci.yml` 추가.

- **플랫폼 패스**:
  - 크로스플랫폼 데스크톱 컨트롤러 추가;
  - macOS `.command`와 설치 런처 추가;
  - Windows `.cmd`와 PowerShell 런처 추가;
  - macOS/Windows zip 패키지 빌더 추가;
  - 플랫폼 설정 문서 추가.

- **문서 패스**:
  - 문서 색인 추가;
  - 아키텍처 가이드 추가;
  - CLI 레퍼런스 추가;
  - 워크플로 가이드 추가;
  - 한국어 운영자 가이드 추가;
  - 보안 모델 추가;
  - README와 검증에서 새 문서로 링크 연결.

## Initial

- Codex/Claude handoff 브릿지 추가.
- 워크스페이스 컨트롤러 추가.
- 모바일 앱 원격 가이드 추가.
- 사전 준비 설정과 provider/모델 타겟팅 프로토콜 추가.
- 공유 계약과 검증 플레이북 추가.
- 선택적 훅 예시와 커스텀 HTTP 원격 스크립트 추가.

